# xlen.inc - register-width abstraction so the same source assembles for
# RV64 (the default, used for host testing under qemu-riscv64) and RV32 (the
# Pico 2's Hazard3 cores; assemble with --defsym XLEN=32 -march=rv32im).
#
# Only the doubleword instructions (ld/sd) and pointer-sized data differ
# between the two; every other instruction used here is identical.
.ifndef XLEN
.set XLEN, 64
.endif

.if XLEN == 64
.equ PTRSIZE, 8             # bytes in a pointer / saved register
.equ PSHIFT,  3             # log2(PTRSIZE)
.macro REG_L rd, off, base=sp
    ld \rd, \off(\base)
.endm
.macro REG_S rs, off, base=sp
    sd \rs, \off(\base)
.endm
.macro NAMEPTR sym
    .dword \sym
.endm
# RECIP_MUL rd, rn, rr:  rd = (rn * rr) >> 32, for rn, rr < 2^32 and a product
# that fits the register (RV64 needs rn * rr < 2^64; always true here).
.macro RECIP_MUL rd, rn, rr
    mul \rd, \rn, \rr
    srli \rd, \rd, 32
.endm
.else
.equ PTRSIZE, 4
.equ PSHIFT,  2
.macro REG_L rd, off, base=sp
    lw \rd, \off(\base)
.endm
.macro REG_S rs, off, base=sp
    sw \rs, \off(\base)
.endm
.macro NAMEPTR sym
    .word \sym, 0           # the name field is always 8 bytes, so a filter
.endm                       # record has the same layout at both widths
.macro RECIP_MUL rd, rn, rr
    mulhu \rd, \rn, \rr         # RV32: the high half of the product directly
.endm
.endif
