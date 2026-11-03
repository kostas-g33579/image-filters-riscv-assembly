# Pico firmware: filters on the RP2350's RISC-V cores

The image filters from `../src/kernel.s` running on a Raspberry Pi Pico 2 /
Pico 2 W, on the **Hazard3 RISC-V cores** (not the Cortex-M33 pair). A small C
program handles USB serial and the wire protocol; the filtering is the same
assembly `filter_core` and `window_level` the host tests verify.

```
host PC                                   Pico 2 (Hazard3, RV32)
pico_filter.py --- USB serial (CDC) --->  main.c  -> proto.c -> kernel.s (assembly)
```

## Build, flash, run

```sh
make pico                         # -> build-pico/image_filters.uf2
```

Flash: hold **BOOTSEL** while plugging the board in, copy `image_filters.uf2`
onto the `RP2350` drive that appears; the board reboots by itself. After the
first flash, `picotool load -f build-pico/image_filters.uf2` reboots a running
board into the bootloader without the button.

Run a job (the serial device is usually `/dev/ttyACM*`; opening it needs the
`uucp` group or `sudo`):

```sh
python3 scripts/make_phantom16.py images/phantom16_256.pgm 256    # a 256x256 16-bit test image
sudo python3 pico/tools/pico_filter.py images/phantom16_256.pgm out.pgm identity --window 1064 400 --verify
sudo python3 pico/tools/pico_filter.py images/input.pgm out.pgm gaussian 4 --port /dev/ttyACM1
```

`--verify` re-runs the same job through the Linux test harness under
`qemu-riscv32` and requires byte-identical output. The client prints the
**cycles and instructions the core actually spent in `filter_core`**, read
from the Hazard3's `mcycle` / `minstret` counters (the firmware clears
`mcountinhibit`, which the chip holds set after reset to save power).

Limits: at most **90,000 pixels** (two 16-bit buffers must fit in the 520 KB
of RAM, e.g. 300x300 or 256x256 with room to spare). Larger images are
rejected with a clear status; streaming them through line buffers is the
planned way past this.

## Wire protocol

All integers are little-endian 32-bit. Samples are always unsigned 16-bit
little-endian, whatever the image's `maxval`.

1. **Host -> device, request header (48 bytes):**
   `"IMG1"`, filter name (16 bytes, NUL-padded), then `W`, `H`, `maxval`,
   `passes` (`0xFFFFFFFF` = the filter's default), `flags` (bit 0 = window),
   `center`, `width`.
2. **Device -> host, response header (40 bytes), as an acknowledgement:**
   `"OUT1"`, `status`, `W`, `H`, `bytes_per_sample`, `maxval`, `cycles`,
   `instret`, `payload_bytes`, `reserved`. If `status != 0` the request was
   rejected and nothing else follows.
3. **Host -> device:** `W*H` samples (2 bytes each).
4. **Device -> host:** a response header again (with the measured `cycles` and
   `instret`), then `payload_bytes` of result: 16-bit samples, or 8-bit
   display values if a window was requested (`bytes_per_sample` = 1,
   `maxval` = 255).

Status: 0 ok, 2 unknown filter, 3 bad/too-large dimensions, 4 bad maxval,
5 bad pass count, 6 bad window, 7 a sample exceeds maxval, 8 timeout (the host
stopped sending). The device skips any bytes before the `"IMG1"` magic, so
boot noise or a half-sent earlier request cannot wedge it.
