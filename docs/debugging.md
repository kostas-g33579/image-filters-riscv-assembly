# Debugging the filter kernel on the RP2350 (OpenOCD + gdb)

Setup: a plain Pico 2 running `debugprobe` firmware is wired to the target's
SWD pads (probe GP2 -> SWCLK, GP3 -> SWDIO, GND -> GND), each board on its own
USB cable. The target (Pico 2 W) runs our firmware on its RISC-V cores.

Everything below has been run on a real Pico 2 W (attach, `load`, three
breakpoints, register and memory reads, single-stepping, detach), and the values
quoted match what the code predicts; see the worked example near the end.

## Three terminals

**Terminal 1: OpenOCD** (leave it running; it is the bridge between gdb and the probe)

```
openocd -s /usr/share/openocd/scripts -f interface/cmsis-dap.cfg -f target/rp2350-riscv.cfg -c "adapter speed 5000"
```

You should see `SWD DPIDR 0x4c013477` and both cores examined (`rp2350.rv0`,
`rp2350.rv1`, `XLEN=32`), then `Listening on port 3333 for gdb connections`.

**Terminal 2: gdb**

```
cd ~/projects/riscv-image-blur
riscv64-elf-gdb build-pico/image_filters.elf
```

```
(gdb) set pagination off
(gdb) target extended-remote :3333
(gdb) monitor reset halt
(gdb) load
(gdb) monitor reset halt
```

`load` writes the ELF into the target's flash over the debug wires, so the code on
the chip matches the ELF's symbols. (If it fails, copy
`build-pico/image_filters.uf2` onto the board with BOOTSEL instead and skip
`load`.) Then set three hardware breakpoints. Hardware breakpoints are needed
because the code runs from flash, which a software breakpoint cannot patch:

```
(gdb) hbreak *0x10000578
(gdb) hbreak *0x100005fa
(gdb) hbreak *0x100006dc
(gdb) info symbol 0x100005fa
(gdb) continue
```

Those addresses are `filter_core`, `.single_pass` and `.single_interior` in the
current ELF. `info symbol` should answer `.single_pass in section .text`. If you
rebuild, get fresh addresses with `riscv64-elf-nm -n build-pico/image_filters.elf | grep -E "filter_core$|single_pass|single_interior"`.

**Terminal 3: trigger a job.** Once `continue` is running, give the board a
couple of seconds to enumerate over USB, then:

```
cd ~/projects/riscv-image-blur
sudo python3 pico/tools/pico_filter.py images/phantom16_256.pgm out.pgm sobelx --timeout 600
```

`--timeout 600` keeps the client waiting while you inspect. `sobelx` is a good
choice because its kernel has negative weights and zeros, so the registers are
easy to recognise.

## Stop 1: the call arrives at `filter_core`

gdb prints `Thread ... hit Breakpoint 1`. This is the C code calling our
assembly with the standard RISC-V argument registers:

```
(gdb) info registers a0 a1 a2 a3 a4 a5 a6
(gdb) x/9dw $a4+8
(gdb) x/s *(char**)$a4
```

| register | holds | expect (sobelx on the 256x256 phantom) |
|---|---|---|
| a0 | source buffer (in SRAM, `0x2000....`) | an address |
| a1 | scratch buffer | an address |
| a2, a3 | width, height | 256, 256 |
| a4 | pointer to the filter record (in flash) | an address `0x1000....` |
| a5 | passes | 1 |
| a6 | maxval | 4095 |

`x/9dw $a4+8` prints the nine kernel weights from the 64-byte record in the
table: `-1 0 1 -2 0 2 -1 0 1`. `x/s ...` prints the filter's name.

## Stop 2: `.single_pass`, the weights are in registers

```
(gdb) continue
(gdb) info registers s0 s1 s2 s3 s4 s5 s6 s7 s8
(gdb) info registers s9 s10 s11 t2 t3 t4
```

| register | holds | expect |
|---|---|---|
| s0 .. s8 | the nine weights | -1 0 1 -2 0 2 -1 0 1 (gdb shows -1 as `0xffffffff`, -2 as `0xfffffffe`) |
| s9 | reciprocal R = ceil(2^32 / 4) | `0x40000000` |
| s10 | scaled bias | 0 |
| s11 | d/2, the rounding term | 2 |
| t2 | last column byte offset 2*(W-1) | 510 (`0x1fe`) |
| t3 | maxval | 4095 (`0xfff`) |
| t4 | abs flag | 1 |

This is the "weights in registers" idea, visible in the hardware.

## Stop 3: `.single_interior`, the fixed-offset taps

```
(gdb) continue
(gdb) info registers a0 a1 a2 a3 t0
(gdb) p $a1 - $a0
(gdb) x/3hx $a0-2
(gdb) x/3hx $a1-2
(gdb) x/3hx $a2-2
```

The three `x/3hx` lines print the three 16-bit samples at columns 0, 1, 2 in the
row above, the current row and the row below: the nine pixels the interior loop
is about to filter, read at the fixed offsets -2, 0, +2.

The first time you stop here you are on row 0, so `p $a1 - $a0` is **0**: the
row above row 0 does not exist, so the kernel replicates the pixel's own row (edge
clamping).

This breakpoint is on the loop's first instruction, so it fires for **every
interior pixel**: `continue` takes you to the next pixel of the same row (column
2), not to the next row. To get to row 1 (where `p $a1 - $a0` is **512**, the row
stride of 2 bytes x 256), skip the rest of row 0. A 256-wide row has 254 interior
pixels and you are already on the first, so, with `N` the breakpoint's number from
`info breakpoints`:

```
(gdb) ignore N 253
(gdb) continue
```

Step through a pixel if you like:

```
(gdb) display/i $pc
(gdb) stepi 12
(gdb) info registers a5 a6 a7 t5 t6
```

## Worked example (a real session)

The job was `sobelx` on `images/phantom16_256.pgm`. At the first stop in the
interior loop (column 1 of row 0) gdb showed:

```
a0 0x2000167a   a1 0x2000167a   a2 0x2000187a   a3 0x2002d59a   t0 0x20001876
x/3hx $a0-2:  0x0014 0x001f 0x0015        (row above:   20 31 21)
x/3hx $a1-2:  0x0014 0x001f 0x0015        (this row:    20 31 21, the same row)
x/3hx $a2-2:  0x0031 0x0019 0x001f        (row below:   49 25 31)
```

* `a0 == a1`: row 0 has no row above, so it reuses itself.
* `a2 - a1 = 0x200 = 512`: one row of 256 samples of 2 bytes.
* `a3 - (scratch buffer) = 2`: the destination is column 1 as well.
* `t0 - row start = 0x1fe = 510`: the interior loop ends at column W-1.

Weights `-1 0 1 / -2 0 2 / -1 0 1` applied to those nine pixels:
`(-20 + 0 + 21) + (-40 + 0 + 42) + (-49 + 0 + 31) = 1 + 2 - 18 = -15`; the abs
flag makes it 15; `(15 + 2) / 4 = 4` (the `+ 2` is the rounding term `d/2`, the
division is the multiply by `0x40000000` and a shift). The output file holds **4**
at pixel (1, 0).

At the next stop (column 2, row 0), `stepi 12` ran the first twelve instructions
of the pixel body and stopped on `lhu a7, 2(a1)`, the loads and multiplies being
software-pipelined. The registers then were:

| register | value | what it is |
|---|---|---|
| `a5` | -11 | the running sum so far: `(31 x -1) + (21 x 0) + (20 x 1)`, the row above |
| `a7` | 0 | the product `21 x 0` (the zero weight; not yet overwritten) |
| `t5` | 20 | the product `20 x 1` (right neighbour times weight +1) |
| `t6` | -62 | the product `31 x -2` (left neighbour of this row times weight -2), not yet added |
| `a6` | 21 | a raw pixel just loaded: the centre, waiting to be multiplied by 0 |

Three products and a partial sum are in flight at once: that is what the
pipelined schedule looks like in hardware.

## Finish

Remove the breakpoints before letting it run, or it stops again on every row:

```
(gdb) delete
(gdb) continue
```

The client in terminal 3 then prints the result. Leave gdb with `detach` then
`quit`, so the target keeps running normally. Stop OpenOCD with Ctrl-C.

## Useful gdb commands

| command | what it does |
|---|---|
| `info registers` | all general registers (`info registers pc sp` for a few) |
| `stepi` / `nexti` | one instruction (`stepi 5` for five) |
| `x/8i $pc` | disassemble the next 8 instructions |
| `x/16wx ADDR` | 16 words in hex at an address (`h` for halfwords, `b` bytes, `d` decimal) |
| `p/x $s9` | print a register in hex |
| `info breakpoints` | list breakpoints (`delete N` removes one) |
| `monitor reset halt` | reset the chip and stop at the start |
| `interrupt` | stop the running target |

## Resetting the chip from gdb can leave USB out of step

`monitor reset halt` (and `load`) restart the cores, but the PC's USB stack does
not necessarily notice: the kernel log shows no disconnect, so it still believes
the device is set up, while the restarted firmware starts a fresh USB session
and waits for a host that never re-enumerates. Symptom: the client times out and
nothing happens in gdb. Evidence seen in practice: the host says
`devnum` / `bConfigurationValue 1` unchanged for tens of minutes, the firmware's
TinyUSB state says no DTR (`_cdcd_itf[0].line_state == 0`), and its receive FIFO
is empty (no bytes ever arrived).

Fix, without unplugging anything (the debugger stays attached): make the PC
perform a real USB bus reset, then rerun the client:

```
sudo python3 pico/tools/usb_reset.py
```

(If the device was left unconfigured, e.g. by an earlier failed attempt, the
tool also sets configuration 1 so the serial port comes back; by hand that is
`echo 1 | sudo tee /sys/bus/usb/devices/1-3/bConfigurationValue`.)

It resets the Raspberry Pi device that is not a debug probe (`--serial` picks one
explicitly; `--dry-run` only shows which it would reset) and prints whether a
serial port came back. The kernel log (`journalctl -k -n 20`) should then show a
new device number and a new `ttyACM`. Do this after every reset you do from gdb,
before starting the client.

Do not use the sysfs trick `echo 0/1 > .../authorized`: it does not reset the
bus, it only re-sends SET_CONFIGURATION to the old address, which the restarted
device ignores, so it ends with `usb 1-3: can't set config #1, error -71`.

To look at the firmware's view yourself while it is running (read-only, no halt),
use OpenOCD's telnet port: `mdb 0x2000142c 2` is `line_state` (bit 0 = DTR) and
`mdh 0x20001440 2` is the receive FIFO's write and read indices. (Addresses are
for the current ELF; `riscv64-elf-gdb -batch build-pico/image_filters.elf -ex
"p &_cdcd_itf[0].line_state"` gives fresh ones.)

## If something goes wrong

* `load` reports a flash error: use BOOTSEL and copy `build-pico/image_filters.uf2`, then continue from `hbreak`.
* The client says it timed out: raise `--timeout`, or just rerun it after `continue`; the firmware resynchronises by itself.
* The board disappears from `ls /dev/serial/by-id` after a reset: wait two seconds and look again; it re-enumerates.
* Only one program may read the target's serial port at a time (no `cat` in another terminal).
