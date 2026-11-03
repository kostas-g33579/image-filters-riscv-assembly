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
condition holds for every divisor up to **2048**. The table uses 1, 4, 9 and
16. `d = 1` skips the multiply, and `R` is computed once per call
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
filter) push the largest sums through the real code. (No filter in the
table has negative sums together with a divisor above 1, so the sign handling
of the divide is not yet exercised by the tests.)

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

### Where the rest of the time goes

Every filter costs about the same, `identity` included, because the loop
always does all nine taps (each: add, load sample, load weight, multiply,
accumulate) whatever the kernel is. The nine taps are about 70 of the ~94
cycles. Larger gains would need structural changes, not micro-tweaks: keeping
the nine weights in registers, skipping zero weights, or special-casing the
image interior so the edge clamping disappears. Those are for later.
