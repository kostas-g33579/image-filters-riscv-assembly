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

## What that suggests for later (not done yet)

- The per-pixel `div` (18-19 cycles) is a sizeable fraction of each pixel's
  cost, since a 3x3 tap costs only a few single-cycle instructions. Replacing
  it with a multiply by a reciprocal plus a shift (1 cycle for the multiply)
  is the obvious optimisation, but it needs a proof that it is exact over
  our value range, so it is a separate step from the port.
- Zbb provides `min`/`max` (and `neg`-style helpers), which would replace
  the branchy clamp to `0..maxval` with two instructions.
