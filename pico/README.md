# Pico firmware: filters on the RP2350's RISC-V cores

The image filters from `../src/kernel.s` running on a Raspberry Pi Pico 2 /
Pico 2 W, on the **Hazard3 RISC-V cores** (not the Cortex-M33 pair). A small C
program handles USB serial and the wire protocol; the filtering is the same
assembly `filter_core` and `window_level` the host tests verify. The same firmware also
serves the **DICOM to NIfTI anonymiser** (request magic `DCM1`, see the last section).

```
host PC                                   Pico 2 (Hazard3, RV32)
pico_filter.py    --- USB serial (CDC) -> main.c -> proto.c -> kernel.s (assembly)       IMG1, STR1
pico_anonymize.py --- USB serial (CDC) -> main.c -> proto.c -> anon.c -> dicom.c, geom.c,
                                                                nifti.c, sidecar.c       DCM1
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

The client states plainly where the filtering ran. On a real board:

```
identity: 256x256, maxval 4095 -> wrote out.pgm (65551 bytes, 0.88s round trip)
filtered on: the board at /dev/ttyACM1 (usb-Raspberry_Pi_Pico_...-if00): the filter ran on the
RP2350's RISC-V core. This PC only sent the pixels and received the result.
on the core: 5646526 cycles, 4526158 instructions retired (IPC 0.80), read from the core's own mcycle/minstret counters
```

The Python only moves bytes; the filtering happens on the chip. With `--exec`
(the automated tests) it instead says `NOT hardware`.

`--verify` re-runs the same job through the Linux test harness under
`qemu-riscv32` and requires byte-identical output. The client prints the
**cycles and instructions the core actually spent in `filter_core`**, read
from the Hazard3's `mcycle` / `minstret` counters (the firmware clears
`mcountinhibit`, which the chip holds set after reset to save power).

Limits: **whole-image mode** (`IMG1`) takes at most **90,000 pixels** (two 16-bit
buffers in the 520 KB of RAM, e.g. 300x300 or 256x256). Bigger images use
**streaming mode** (`STR1`, below): the client picks it automatically, or force
it with `--stream`. Streaming sends the image row by row and the device keeps only
a few rows of each filter pass, so any height works and the only limit is
`passes * 3 * width * 2` bytes (plus one row) of its 420,000-byte working memory:

| width | most passes |
|---:|---:|
| 240 | 255 (the pass limit) |
| 512 | 136 |
| 1024 | 68 |
| 2048 | 34 (33 with a window) |
| 4096 | 17 |

See `docs/streaming.md` for how it works.

## Testing without hardware

`make test-proto` builds `build32/proto_test`: the same `proto.c` and the same
assembly kernel as a Linux program for `qemu-riscv32`, with the wire protocol
on stdin/stdout. `tests/test_proto.py` runs every filter at three bit depths,
all options, the largest image, noise before a request, back-to-back requests,
every rejection status, and a host that stops mid-message, and compares each
result byte for byte with the Linux harness. `make test-stream` does the same
for streaming (`tests/test_stream.py`): every filter and bit depth, every small
width and height, chains of up to 255 passes, 4096-wide and 5000-row images, the
memory limit to the byte, an error in the first, middle and last row, and a host
that stops mid-row. What it cannot cover is the USB layer (`main.c`) and the
hardware itself.

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
stopped sending), 9 not enough device memory (streaming only), 10 a bad separable kernel. The device skips
any bytes before a request magic (`"IMG1"` or `"STR1"`), so boot noise or a
half-sent earlier request cannot wedge it.

### Streaming (`STR1`)

Same 48-byte request header with the magic `"STR1"` (`W` up to 16384, `H` up to
2^20; the real limit is memory). The exchange is:

1. **Host -> device:** the request header.
2. **Device -> host:** the acknowledgement (`"OUT1"`, `status`; `bytes_per_sample`
   is 1 with a window else 2, `payload_bytes` = bytes per OUTPUT row, `reserved` =
   the device's working-memory size). `status != 0` ends it (9 = not enough memory).
3. **Host -> device:** the `H` input rows, one after another, `W` samples of 2
   bytes each, no framing. The host keeps sending while it receives (a sender
   thread): sending everything first and only then reading deadlocks once the data
   outgrows the pipe or USB buffers.
4. **Device -> host, interleaved with 3:** each finished row as a frame:
   `"ROW1"`, the row index (u32), then `payload_bytes` of samples. Rows arrive in
   order, a few rows behind the input (one row per pass in the pipeline).
5. **Device -> host, last:** the final `"OUT1"` header with `status` 0 and the
   measured `cycles` / `instret` (time spent in the filter only), or `status != 0`
   at any point if something went wrong. After an error the device reads and
   discards the rows still being sent so the next request starts cleanly, which is
   why the client always sends all `H` rows.

The result is bit-identical to the whole-image mode (`tests/test_stream.py`
checks this against the Linux harness).

In streaming mode the reported `cycles` / `instret` are read around the filter but the
core is also servicing USB at the same time, so they include about 1,900 cycles of
interrupt time per input row (more with the larger FIFOs, because the overlap puts more
USB work inside the filter window). Use whole-image mode for the filter's pure cost
(`docs/streaming.md`).

### A separable kernel in streaming mode (`STR1`, flags bit 1)

A Gaussian with a chosen sigma (or a box, or any symmetric non-negative kernel) in place of a named 3x3 filter, for radii up to
15 (a kernel up to 31 wide): `docs/separable_gaussian.md`. Set bit 1 of `flags` in the same 48-byte `STR1` header and send, right
after it, `u32 r` (0..15) and `r+1` signed 32-bit weights `w[0..r]` (`w[0]` the centre, `w[k]` each of the two samples at distance
k). The device checks them before it acknowledges: radius at most 15, every weight non-negative, and `w[0] + 2*sum(w[1..r])`
exactly 2^14, otherwise status **10** and nothing follows (a whole-image `IMG1` request with the flag is also status 10). The name
field is ignored; `passes` counts how many times the kernel is applied (default 1); the window option works as for any filter.
The rest of the exchange is the same as above. The host makes the weights (`scripts/gauss_weights.py`), so the device needs no
`exp()` and a sigma in millimetres is just a division by the pixel size on the host:

```sh
sudo python3 pico/tools/pico_filter.py in.pgm out.pgm gauss --sigma 2.0 --verify                       # sigma in pixels
sudo python3 pico/tools/pico_filter.py in.pgm out.pgm gauss --sigma-mm 1.5 --pixel-mm 0.94 --radius 6    # in millimetres
```

Memory: a stage keeps `2r+1` rows instead of 3, so `passes * (2r+1) * width * 2` bytes plus two rows (the staging row and the
output row) and `width * 4` bytes of accumulators (`stream_sep_memory_needed()` in `src/stream.h`; r = 15, 4 passes: width up to
1640). The result is bit-identical to the exact integer model (`tests/test_gauss_stream.py`), not to the 3x3 filters, which it does
not replace.

### USB speed

The client writes to the port in 64-byte slices and the firmware uses 4096-byte TinyUSB
FIFOs (`CFG_TUD_CDC_RX/TX_BUFSIZE` in `pico/CMakeLists.txt`); together they took a 512x512
run from 2.64 s to 0.83 s. The measurements behind this, and what is still unexplained,
are in `docs/streaming.md`. To repeat them: `make bench-pico` builds three benchmark
firmwares (`usb_bench_stdio`, `usb_bench_fifo`, `usb_bench_raw`; the `raw` one hangs
intermittently and is kept only as an experiment), flash one with
`picotool load -f build-pico/usb_bench_stdio.uf2` and run
`sudo python3 pico/tools/usb_bench.py --mb 4 --hostwrite 64`. The benchmark logic is tested
without hardware by `make test-bench`.

## The anonymiser (`DCM1`)

The same firmware image also turns a DICOM series into a NIfTI volume and a JSON file; the device is the trust boundary, so
only voxel values, a header built from constants and validated numbers, and a JSON built the same way ever leave it.

```sh
sudo python3 pico/tools/pico_anonymize.py data/dicom/SERIES_DIR --subject P001 --sequence T1c
sudo python3 pico/tools/pico_anonymize.py --batch data/dicom --out output/batch --report output/batch.csv
```

- **Protocol:** a request (`DCM1`, four labels, the files sorted along the slice normal) and a reply of framed messages
  (accepted, voxel chunks, the 352-byte header, the JSON, done or a named error). The exact format is in `src/anon.h`.
- **Memory:** the anonymiser's work area (153,640 bytes) lives inside the 420,000-byte arena the filters use, so the RAM use
  did not change (code 79 KB, up from 32 KB). It reads a file 4 KB at a time and parses the header as soon as it has ended
  (about 3 KB), so a file of any size works; core 0's stack is 4 KB (`PICO_STACK_SIZE` in `CMakeLists.txt`) because a hostile
  header of nested sequences makes the reader recurse.
- **Speed:** 35.6 s for a 192-slice series (19.45 MB), about 546 KB/s each way, which is the ceiling of the USB loop measured
  by the benchmark; see "Speed" in `../docs/dicom.md`.
- **Tests:** `make test-anon-c` runs the protocol under `qemu-riscv32` (the same code as the firmware), `make test-anonymize`
  and `make test-batch` test the client; `make anon-probe` builds a firmware that runs the anonymiser modules on a built-in
  series and `sudo python3 pico/tools/anon_probe_check.py` compares what the chip printed with the emulator's output.
- Everything about the format, the guarantees and the limits is in `../docs/dicom.md`.

