# RV32 port and Hazard3 notes

The Raspberry Pi Pico 2's RISC-V cores (Hazard3) are **RV32**; the host test
build under `qemu-riscv64` is RV64. The same source tree builds both:

```sh
make            # RV64 -> build/filter,   run with qemu-riscv64
make rv32       # RV32 -> build32/filter, run with qemu-riscv32
make testall    # the full test suite against both
```

`src/xlen.inc` is the whole width abstraction. Assembling with
`--defsym XLEN=32 -march=rv32im -mabi=ilp32` selects the 32-bit side.

## What differs between the two builds

| Thing | RV64 | RV32 |
|---|---|---|
| Saving/restoring `s0`-`s11` in `filter_core` | `sd` / `ld`, 96-byte frame | `sw` / `lw`, 48-byte frame (`REG_S` / `REG_L`) |
| Filter-record name pointer | `.dword` | `.word name, 0` (always 8 bytes, so the record layout and every `F_*` offset are identical at both widths) |
| Reading `argc` / `argv` | 8-byte slots | 4-byte slots (`PTRSIZE`, `PSHIFT`) |
| `W*H` overflow | cannot wrap (each is < 2^32) | can wrap, so `mulhu` checks the high half |
| Header numbers | saturate at 2^32-1 | same (the parser saturates instead of wrapping, so "absurdly large" is reported identically at both widths) |

Everything else (`lhu`, `sh`, `lw`, `mul`, `div`, branches, shifts) is the
same instruction at both widths. The filter arithmetic never needs more than
32 bits: the largest intermediate value is about 1 million (16 x 65535 for
`sobel` on 16-bit data).

`kernel.s` is the part that goes onto the Pico. `filter.s` (the Linux
harness: file I/O, syscalls) stays a host-only test vehicle; on the Pico a C
program calls `filter_core` and `window_level` instead.

## Hazard3 facts from the RP2350 datasheet

(Datasheet section numbers are those of the Feb 2025 edition on file.)

- **ISA** (3.8): RV32I + M + A + C, plus Zba, Zbb, Zbs, Zbkb, Zcb, Zcmp,
  Zicsr, Zifencei. 3-stage in-order pipeline.
- **Compiler flags** (3.8.1.2): `-march=rv32ima_zicsr_zifencei_zba_zbb_zbs_zbkb_zca_zcb_zcmp`
  (GCC 14+), or `rv32imac_zicsr_zifencei_zba_zbb_zbs_zbkb` for GCC 13 and
  older. The test build uses plain `rv32im`, a subset that runs on the real
  core.
- **Cycle counts** (3.8.7.2): `mul`, `mulh`, `mulhu` take **1 cycle**;
  `div`/`rem` take **18-19 cycles** (`divu`/`remu`: 18).

## Phase 2b: no divide instruction, optional Zbb clamping

Hazard3's `div` costs 18-19 cycles against 1 for `mul`, so the per-pixel
divide by the filter's divisor `d` is replaced by a multiply.

**The identity.** Let `R = ceil(2^32 / d)` and `e = R*d - 2^32` (so
`0 <= e < d`). Then for any integer `n >= 0`

    n * R / 2^32  =  n/d + n*e / (d * 2^32)

The fractional part of `n/d` is at most `(d-1)/d`, so the extra term cannot
push the value over the next integer as long as `n*e / (d*2^32) < 1/d`, i.e.

    n * e < 2^32      ==>      floor(n / d) == (n * R) >> 32

Here `n = |sum| + d/2` is at most `16 * 65535 + d/2 < 2^21` (16 is the
largest total weight, `sobel`'s two kernels together), and `e < d`, so the
condition holds for every divisor up to **2048**. The table uses 1, 2, 4, 9
and 16. `d = 1` skips the multiply, and `R` is computed once per call
(`R = floor((2^32-1)/d) + 1`, one `divu`), not per pixel. On RV32 the
product's high half is `mulhu`; on RV64 it is `mul` then `srli 32`.

**Rounding and sign.** "Round to nearest, halves away from zero" is done on
the magnitude: take `|v|` with a sign mask (`srai`, `xor`, `sub`), add
`d/2`, divide by the reciprocal, restore the sign with the same two
instructions. That is identical to the old "add `d/2` (subtract it if
negative), then truncating divide".

**What checks it.** `tests/run_tests.py` verifies the identity exhaustively
(every `n` from 0 to the largest possible, for each divisor in
`kernel.s`'s table), checks `n*e < 2^32` for each, and fails if a table
divisor changes without the test's `FILTERS` table agreeing. The 16-bit
extreme-value tests (all-max, all-zero, checkerboards, stripes for every
filter) push the largest sums through the real code, and `softemboss`
covers negative sums with a divisor above 1 and the `d = 2` case where
`R = 2^31`.

**Zbb (optional).** `make rv32zbb` assembles with `-march=rv32im_zbb
--defsym ZBB=1`, replacing the branchy clamps (to `0..maxval`, and the
left/right column edges) with `min`/`max`. It runs under
`qemu-riscv32 -cpu rv32,zbb=true`. The plain `rv32` build stays the
portable baseline.

### Measured effect (estimated Hazard3 cycles, `scripts/estimate_cycles.py`)

32x32 image, 1 pass, cycles per pixel inside `filter_core`:

| filter | `div` per pixel | reciprocal | reciprocal + Zbb |
|---|---:|---:|---:|
| blur / gaussian / sharpen / identity | ~116 | ~98 (1.18x) | ~94 (1.23x) |
| edge | ~117 | ~99 | ~95 |
| sobel (two kernels, one divide) | ~187 | ~169 | ~165 (1.13x) |

QEMU does not model cycles, so these come from replaying QEMU's instruction
trace through the datasheet's cycle table (section 3.8.7: ALU and multiply 1,
divide 18-19, taken branch 2, load 2 when pessimistic). Treat them as a
comparison between versions, not a prediction of the real chip: the real
pipeline and RAM behaviour are not modelled.

## Phase 2c, step 1: the nine weights live in registers

`filter_core` is now a dispatcher. A filter with one kernel takes
`filter_core_single`; a filter with a second kernel (`sobel`, 18 weights)
takes `filter_core_generic`, which is the previous code unchanged.

**Why registers.** In the old tap loop each of the nine taps did
`add, lhu, lw (the weight), mul, add`. The weights never change within a call,
so the `lw` was redundant work, and a load whose result is used by the very
next instruction costs an extra cycle (datasheet 3.8.7.1).

**Can it fit?** The per-pixel loop already needs the three row pointers, the
destination pointer, three column offsets, the sum, temporaries, and the
call constants (reciprocal, scaled bias, `d/2`, `maxval`, abs flag, last
column). Counting them, nine weights plus all that is about 25 of the 28
registers that may be used, so it only fits if the outer-loop state (row
index, pass count, `H`, the two buffer pointers) lives on the stack; that
state is touched once per row, not per pixel, so the cost is negligible.
`sobel` needs 18 weights and cannot fit, which is why it keeps the generic
path.

**Load scheduling.** The nine taps are software-pipelined with four rotating
temporaries: tap *i* does `add; lhu`, then tap *i+1*'s `add; lhu` runs while
the load completes, and only then tap *i*'s `mul`, then its accumulate one
step later. A load is never used by the immediately following instruction.

**Measured** (`scripts/estimate_cycles.py`, Zbb build, 32x32, estimated Hazard3
cycles per pixel in `filter_core`):

| filter | before | after |
|---|---:|---:|
| identity, blur, gaussian, sharpen, emboss, softemboss | 93.7 | **68.9** (1.36x) |
| edge, sobelx, sobely | 94.7 | **69.9** (1.35x) |
| sobel (two kernels, generic path) | 164.7 | 164.7 (unchanged) |

Instructions per pixel fall from about 70 to 57. The mutation checks (a swapped
weight, a wrong temporary, unclamped top/bottom/left/right edges, no buffer
swap between passes, a stale row index, the dispatcher inverted, clamping to
the wrong register, and others) all fail the suites, each in the builds it
should.

## Phase 2c, step 2: the interior fast path

**The idea.** For a pixel that is not in the first or last column, its three
columns of neighbours are always at -1, 0 and +1. If a pointer sits on the
pixel's own column in each of the three rows, every one of the nine loads is at
a *fixed* offset from a pointer (`lhu t, -2(a0)`, `0(a0)`, `2(a0)`, and the
same for `a1` and `a2`), so there is no per-tap address `add` and no
per-pixel clamping. Moving on one pixel is four `addi`s (three row pointers and
the destination).

**The edges become constants too.** At the first column "the left neighbour is
the pixel itself", so its left offset is `0`; at the last column the right
offset is `0`. So a row is three pieces, none of which needs run-time clamping:

| piece | left | right | how often |
|---|---:|---:|---|
| first pixel | 0 | +2 | once per row |
| interior pixels | -2 | +2 | a loop, W-2 times |
| last pixel | -2 | 0 | once per row |
| (a one-pixel-wide image) | 0 | 0 | the whole row |

A two-pixel-wide row has no interior pixels. The pixel body (nine taps,
rounding divide, clamp, store) is one assembler macro, `PIXEL Lc, Rc`,
expanded four times with different offsets. The interior loop ends on a single
pointer comparison against an end pointer computed once per row.

**Measured** (`scripts/estimate_cycles.py`, Zbb build, 32x32, estimated
Hazard3 cycles per pixel in `filter_core`):

| filter | before | after |
|---|---:|---:|
| identity, blur, gaussian, sharpen, emboss, softemboss | 68.9 | **56.6** (1.22x) |
| edge, sobelx, sobely | 69.9 | **57.7** (1.21x) |
| sobel (two kernels, generic path) | 164.7 | 164.7 (unchanged) |

Instructions per pixel fall from about 57 to about 45.

**Tests.** Splitting a row into first / loop / last is exactly where narrow
images go wrong, so the suite now runs every width from 1 to 9 (heights 1, 2
and 4) for five filters, plus 16-bit and `sobel` at widths 1 to 5. Eleven
deliberate bugs (an edge that reads its neighbour, a right edge that reads past
the row, swapped offsets, a missing width-1 or width-2 case, a wrong loop end,
a pointer that is not advanced, a wrong store offset, a loop that exits early)
are all caught, each in all three builds.

**Not done: reusing loads.** Adjacent output pixels share six of their nine
input samples. Carrying those in registers across iterations (a sliding
window) would cut the loads from nine per pixel to three, but needs a lot of
registers and an unrolled loop, so it is a separate step.

### Where the rest of the time goes (before step 1 above)

Every filter costs about the same, `identity` included, because the loop
always does all nine taps (each: add, load sample, load weight, multiply,
accumulate) whatever the kernel is. The nine taps are about 70 of the ~94
cycles. Larger gains would need structural changes, not micro-tweaks: keeping
the nine weights in registers, skipping zero weights, or special-casing the
image interior so the edge clamping disappears. Those are for later.

## Phase 2c, step 3: Sobel without multiplies

`sobel` was the slowest filter by a wide margin (148 cycles per pixel on the chip): the
generic path does all 18 taps through memory, a load of the pixel, a load of the weight and
a multiply each, for weights that are only ever -2, -1, 0, 1 and 2. `sobelx` and `sobely`
used the register-weights path (9 multiplies, about 47 cycles). All three now have a
dedicated path in `kernel.s` ("Sobel fast path"), chosen by the record's address.

**The idea.** Sobel factors into a vertical and a horizontal part. For every column keep

    vs(x) = above + 2*cur + below        vd(x) = below - above

then `gx(x) = vs(x+1) - vs(x-1)` and `gy(x) = vd(x-1) + 2*vd(x) + vd(x+1)`. A new column
costs three loads and a few adds; both gradients come from a sliding window of the last
columns. `gy` is kept as `e(x-1) + e(x)` with `e(x) = vd(x) + vd(x+1)`. The table's divisor
is 4 and its bias 0, so the divide with rounding is exactly `(|gx| + |gy| + 2) >> 2` (the
reciprocal multiply for d = 4 is a shift), then a clamp to `maxval`. About 19 instructions
per pixel for `sobel`, none of them a multiply.

**Edges without special cases in the hot loop.** The left edge is clamped by starting the
window with column 0 twice, the right edge by a last pixel that reuses its own column, so
every iteration in between simply loads the next column.

**No register moves.** The window rotates through three registers (`vs`) and two (`e`, `vd`),
so six pixels are unrolled and the register roles repeat with period 6. The 0-5 pixels that
do not fill a group go first through a simple step that does move registers, so the
unrolled loop always starts in step 0's roles. `filter_core` for these three records loops
over passes and rows and calls `filter_row`, so the fast code exists once.

**Estimated Hazard3 cycles per pixel** (`scripts/estimate_cycles.py`, Zbb build, the
pessimistic load cost; a comparison between versions, not a prediction of the chip):

| filter | before | after | |
|---|---:|---:|---|
| sobel (224 wide) | ~165 | ~23.6 | 7.0x |
| sobelx (32 wide) | ~57.7 | ~19.9 | 2.9x |
| sobely (32 wide) | ~57.7 | ~17.0 | 3.4x |

**Measured on the chip** (Pico 2 W, 240 x 240 slice, 12-bit, whole-image mode, counters read
from the core's own `mcycle` / `minstret`, every output byte-identical to the Linux harness):

| filter | cycles | instructions | IPC | cycles per pixel | before | speed-up |
|---|---:|---:|---:|---:|---:|---:|
| sobel | 1,191,446 | 1,175,112 | 0.99 | 20.7 | 148 | 7.2x |
| sobelx | 840,769 | 827,595 | 0.98 | 14.6 | ~47 | 3.2x |
| sobely | 783,103 | 713,118 | 0.91 | 13.6 | ~47 | 3.5x |

The chip beats the estimate above by 12% for sobel and by 20-27% for sobelx / sobely
(the estimator charges every load its pessimistic two cycles, and these loops leave a gap
after each load). The round trip was 0.30 s for all three: at 150 MHz sobel's 1.19 M cycles
are about 8 ms, under 3% of it, so USB is now essentially all of the time.
The record is recognised by address, so a copy of it still works, through the generic path;
`tests/run_tests.py` checks the output of all three builds against the independent nine-tap
Python reference at every width from 1 to 40 (each leftover count and several groups), all
three bit depths, extreme patterns, a single bright pixel at every position, several passes
and tall and wide shapes; that the table still holds the weights, divisor 4 and bias 0 the
fast path assumes; and, because a wrong dispatch gives correct pixels, only slower, that the
estimated cycles per pixel stay below a threshold. 38 deliberately injected bugs in the new
assembly were each caught, in both the Zbb and plain builds, except two that cannot differ in
output (swapping the operands of an addition, and swapping the operands of a subtraction
whose result goes through an absolute value).
