# kernel.s - the portable filter core: 3x3 kernel convolution on 16-bit
# samples. No syscalls, no file I/O: it works on buffers the caller owns,
# so the same code can be driven by the Linux test harness (filter.s) or,
# after an RV32 port, by a microcontroller program.
#
# void *filter_core(a0 = src,      // W*H unsigned 16-bit samples, row-major
#                   a1 = scratch,  // W*H*2 bytes, overwritten
#                   a2 = W, a3 = H,
#                   a4 = filter record (see records.inc),
#                   a5 = passes,
#                   a6 = maxval)   // samples are 0..maxval, 1..65535
#   returns a0 = pointer to whichever of the two buffers holds the result
#   (src if passes is 0 or even, else scratch). Preserves s0-s11.
#
# Per pixel:  v = sum(kernel[i] * tap[i]);  if abs: v = |v|;
#             (if the record has a second kernel: v = |v| + |sum2|);
#             v = v / divisor (rounded to nearest, halves away from zero);
#             v += bias * (maxval+1) / 256;  clamp to 0..maxval.
#
# The divide is done without a divide instruction (Hazard3's takes 18-19
# cycles, a multiply takes 1): with R = ceil(2^32 / d), floor(n / d) equals
# (n * R) >> 32 exactly whenever n * (R*d - 2^32) < 2^32. Here n is at most
# 16*65535 + d/2 < 2^21 and R*d - 2^32 < d, so this holds for every divisor
# up to 2048 - the table uses 1, 4, 9 and 16. tests/run_tests.py checks the
# identity exhaustively and rejects a table divisor above that bound.
# Divisor 1 skips the multiply. R is computed once per call, below.
#
# Optional: assemble with --defsym ZBB=1 -march=...zbb to use the Zbb
# min/max instructions for clamping (the RP2350's Hazard3 has them).
# Edge taps clamp to the nearest valid pixel.
.include "records.inc"
.include "xlen.inc"

# CLAMP_0_MAX reg [, maxreg]: clamp reg to 0..maxval (default register s4).
.macro CLAMP_0_MAX reg, maxreg=s4
.ifdef ZBB
    max \reg, \reg, zero
    min \reg, \reg, \maxreg
.else
    bgez \reg, 7f
    li \reg, 0
7:  ble \reg, \maxreg, 8f
    mv \reg, \maxreg
8:
.endif
.endm

.section .rodata
n_identity: .asciz "identity"
n_blur:     .asciz "blur"
n_gaussian: .asciz "gaussian"
n_sharpen:  .asciz "sharpen"
n_edge:     .asciz "edge"
n_emboss:   .asciz "emboss"
n_softemboss: .asciz "softemboss"
n_sobel:    .asciz "sobel"
n_sobelx:   .asciz "sobelx"
n_sobely:   .asciz "sobely"

# name, 9 kernel weights, divisor, bias, abs flag, default passes,
# second-kernel offset (0 = none)
.balign 8
.global filter_table
.global f_blur
filter_table:
f_identity: NAMEPTR n_identity
            .word 0,0,0, 0,1,0, 0,0,0
            .word 1, 0, 0, 1, 0
f_blur:     NAMEPTR n_blur
            .word 1,1,1, 1,1,1, 1,1,1
            .word 9, 0, 0, 8, 0
f_gaussian: NAMEPTR n_gaussian
            .word 1,2,1, 2,4,2, 1,2,1
            .word 16, 0, 0, 4, 0
f_sharpen:  NAMEPTR n_sharpen
            .word 0,-1,0, -1,5,-1, 0,-1,0
            .word 1, 0, 0, 1, 0
f_edge:     NAMEPTR n_edge                       # abs: shows both sides of an edge
            .word -1,-1,-1, -1,8,-1, -1,-1,-1
            .word 1, 0, 1, 1, 0
f_emboss:   NAMEPTR n_emboss                      # zero-sum: flat areas -> mid-gray
            .word -2,-1,0, -1,0,1, 0,1,2
            .word 1, 128, 0, 1, 0
f_softemboss: NAMEPTR n_softemboss               # emboss at half strength: negative sums
            .word -2,-1,0, -1,0,1, 0,1,2         # with a divisor > 1 (d = 2: R = 2^31)
            .word 2, 128, 0, 1, 0
f_sobel:    NAMEPTR n_sobel                       # |gx| + |gy|
            .word -1,0,1, -2,0,2, -1,0,1
            .word 4, 0, 1, 1, f_sobely - f_sobel
f_sobelx:   NAMEPTR n_sobelx
            .word -1,0,1, -2,0,2, -1,0,1
            .word 4, 0, 1, 1, 0
f_sobely:   NAMEPTR n_sobely
            .word -1,-2,-1, 0,0,0, 1,2,1
            .word 4, 0, 1, 1, 0
            NAMEPTR 0               # table terminator (null name)

# TAP rowptr, colreg, kernel_offset [, record]: sum += rowptr[colreg] * weight
# from the given filter record (default s8). colreg is a BYTE offset into
# the row (2 per sample). Clobbers t2, t3; accumulates into s0.
.macro TAP rowptr, colreg, koff, rec=s8
    add t2, \rowptr, \colreg
    lhu t2, 0(t2)
    lw t3, \koff(\rec)
    mul t2, t2, t3
    add s0, s0, t2
.endm

.section .text
.global filter_core
.global window_level

# ---------------------------------------------------------------------------
# filter_core is a dispatcher. A filter with ONE kernel takes the fast path
# below (filter_core_single), which keeps all nine weights in registers so the
# tap loop never loads a weight. A filter with a second kernel (sobel: 18
# weights, which cannot fit in registers) takes filter_core_generic.
filter_core:
    la t0, f_sobel                      # the three Sobel records have a dedicated path
    beq a4, t0, filter_core_sobel       # (see "Sobel fast path" below)
    la t0, f_sobelx
    beq a4, t0, filter_core_sobel
    la t0, f_sobely
    beq a4, t0, filter_core_sobel
    lw t0, F_KERNEL2(a4)
    bnez t0, filter_core_generic
    j filter_core_single                # tail call: a0-a6 are untouched

# Frame: s0-s11 saved, then five 'locals' for the outer-loop state, which is
# only touched once per row (not per pixel) and so can live on the stack.
.equ FRAME_S, (17*PTRSIZE + 15) & -16
.equ L_SRC,  12*PTRSIZE
.equ L_DST,  13*PTRSIZE
.equ L_ROW,  14*PTRSIZE
.equ L_PASS, 15*PTRSIZE
.equ L_H,    16*PTRSIZE

# PIXEL Lc, Rc: filter ONE output pixel. a0/a1/a2 point at this pixel's column
# in the rows above / current / below, and a3 at its destination. Lc and Rc are
# the byte offsets of the left and right neighbours relative to the pixel:
#   interior pixel   Lc = -2, Rc = +2
#   first column     Lc =  0, Rc = +2   (the left edge replicates the pixel)
#   last column      Lc = -2, Rc =  0   (the right edge replicates the pixel)
#   one-column image Lc =  0, Rc =  0
# so the nine loads need no address arithmetic and no run-time clamping.
# Clobbers a5, a6, a7, t5, t6. Leaves t0 (the loop end pointer) alone.
.macro PIXEL Lc, Rc
    # Nine taps, software-pipelined: tap i is "lhu", its "mul" one step later,
    # its accumulate one step after that, so no load feeds the next instruction.
    lhu a6, \Lc(a0)
    lhu a7, 0(a0)
    mul a6, a6, s0
    lhu t5, \Rc(a0)
    mul a7, a7, s1
    mv a5, a6
    lhu t6, \Lc(a1)
    mul t5, t5, s2
    add a5, a5, a7
    lhu a6, 0(a1)
    mul t6, t6, s3
    add a5, a5, t5
    lhu a7, \Rc(a1)
    mul a6, a6, s4
    add a5, a5, t6
    lhu t5, \Lc(a2)
    mul a7, a7, s5
    add a5, a5, a6
    lhu t6, 0(a2)
    mul t5, t5, s6
    add a5, a5, a7
    lhu a6, \Rc(a2)
    mul t6, t6, s7
    add a5, a5, t5
    mul a6, a6, s8
    add a5, a5, t6
    add a5, a5, a6

    beqz t4, 5f                         # optional |sum|
    bgez a5, 5f
    neg a5, a5
5:  srai a6, a5, XLEN-1                 # divide with rounding on |v| (see the
    xor a5, a5, a6                      # header): sign mask, |v|, + d/2,
    sub a5, a5, a6
    add a5, a5, s11
    beqz s9, 6f
    RECIP_MUL a5, a5, s9                # floor(n/d) as (n*R) >> 32
6:  xor a5, a5, a6                      # restore the sign
    sub a5, a5, a6
    add a5, a5, s10                     # + scaled bias
    CLAMP_0_MAX a5, t3                  # clamp to 0..maxval
    sh a5, 0(a3)                        # store the filtered value
.endm

# PIXEL_NEXT: advance all three source rows and the destination one pixel.
.macro PIXEL_NEXT
    addi a0, a0, 2
    addi a1, a1, 2
    addi a2, a2, 2
    addi a3, a3, 2
.endm

# SINGLE_CONSTANTS recreg, maxreg, widthreg: per-call constants of the register-weight
# path. Loads the nine weights into s0-s8, the reciprocal R into s9 (0 means d == 1),
# the scaled bias into s10, d/2 into s11, and sets t2 = last column byte offset,
# t3 = maxval, t4 = abs flag. Clobbers t0, t1.
.macro SINGLE_CONSTANTS recreg, maxreg, widthreg
    lw s0, F_KERNEL+0(\recreg)               # the nine weights, loaded once per call
    lw s1, F_KERNEL+4(\recreg)
    lw s2, F_KERNEL+8(\recreg)
    lw s3, F_KERNEL+12(\recreg)
    lw s4, F_KERNEL+16(\recreg)
    lw s5, F_KERNEL+20(\recreg)
    lw s6, F_KERNEL+24(\recreg)
    lw s7, F_KERNEL+28(\recreg)
    lw s8, F_KERNEL+32(\recreg)
    lw t4, F_ABS(\recreg)
    lw s10, F_BIAS(\recreg)                  # scaled bias = bias * (maxval+1) / 256
    addi t0, \maxreg, 1
    mul s10, s10, t0
    srai s10, s10, 8
    lw t0, F_DIV(\recreg)                    # divisor d >= 1
    srli s11, t0, 1                     # d/2
    li s9, 0                            # R = ceil(2^32/d); 0 means d == 1
    li t1, 1
    beq t0, t1, 1f
    li t1, 0xFFFFFFFF
    divu s9, t1, t0
    addi s9, s9, 1
1:  mv t3, \maxreg                           # maxval
    slli t2, \widthreg, 1
    addi t2, t2, -2                     # last column byte offset
.endm

# ROW_PIXELS base, done: filter one row with the register-weight path. Expects
# a0/a1/a2 = source rows above/current/below at column 0, a3 = destination row,
# t2 = last column byte offset, and the constants of SINGLE_CONSTANTS. Ends with
# "j done". Its labels are base_interior, base_last, base_width1 (so the whole-image
# function keeps the names .single_interior etc. that docs/debugging.md refers to).
.macro ROW_PIXELS base, done
    add t0, a1, t2                      # t0 = where the interior loop ends:
                                        # this row's column W-1 in the source
    beqz t2, \base\()_width1             # a one-pixel-wide image
    PIXEL 0, 2                          # first pixel: left edge = itself
    PIXEL_NEXT
    beq a1, t0, \base\()_last            # width 2: no interior pixels
\base\()_interior:
    PIXEL -2, 2                         # interior pixels: no clamping at all
    PIXEL_NEXT
    bne a1, t0, \base\()_interior
\base\()_last:
    PIXEL -2, 0                         # last pixel: right edge = itself
    j \done
\base\()_width1:
    PIXEL 0, 0
    j \done
.endm

# GENERIC_CONSTANTS: per-call constants of the generic path. Expects s8 = record,
# s4 = maxval, a7 = row stride in bytes. Sets a1 = scaled bias, a0 = reciprocal R
# (0 means d == 1), t4 = d/2, t6 = last column byte offset. Clobbers t0, t2, t3.
.macro GENERIC_CONSTANTS
    lw a1, F_BIAS(s8)               # scaled bias = bias * (maxval+1) / 256
    addi t0, s4, 1
    mul a1, a1, t0
    srai a1, a1, 8

    lw t2, F_DIV(s8)                # divisor d >= 1
    srli t4, t2, 1                   # t4 = d/2, the rounding term
    li a0, 0                          # a0 = R = ceil(2^32/d); 0 means d == 1
    li t3, 1
    beq t2, t3, 1f
    li t3, 0xFFFFFFFF
    divu a0, t3, t2                    # floor((2^32 - 1) / d) ...
    addi a0, a0, 1                      # ... + 1 = ceil(2^32 / d), for d >= 2
1:  addi t6, a7, -2                      # byte offset of the last column
.endm

# GENERIC_COLUMNS base: filter one row with the generic path (any kernel, including
# two-kernel filters). Expects a2/a3/a4 = source rows above/current/below, a5 =
# destination row, a7 = row stride in bytes, and the constants of GENERIC_CONSTANTS.
.macro GENERIC_COLUMNS base
    li s6, 0                         # column byte offset
\base\()_col_loop:
    beq s6, a7, \base\()_col_done

    addi t0, s6, -2                   # left column, clamped to 0
    addi t1, s6, 2                     # right column, clamped to W-1
.ifdef ZBB
    max t0, t0, zero
    min t1, t1, t6
.else
    bgez t0, 3f
    li t0, 0
3:  blt t1, a7, 4f
    mv t1, t6
4:
.endif
    li s0, 0                           # weighted sum
    TAP a2, t0, F_KERNEL+0
    TAP a2, s6, F_KERNEL+4
    TAP a2, t1, F_KERNEL+8
    TAP a3, t0, F_KERNEL+12
    TAP a3, s6, F_KERNEL+16
    TAP a3, t1, F_KERNEL+20
    TAP a4, t0, F_KERNEL+24
    TAP a4, s6, F_KERNEL+28
    TAP a4, t1, F_KERNEL+32

    lw a6, F_KERNEL2(s8)                 # gradient magnitude: second kernel?
    beqz a6, \base\()_single_kernel
    add a6, a6, s8                         # offset -> record address
    mv s1, s0                               # s1 = first sum
    li s0, 0
    TAP a2, t0, F_KERNEL+0, a6
    TAP a2, s6, F_KERNEL+4, a6
    TAP a2, t1, F_KERNEL+8, a6
    TAP a3, t0, F_KERNEL+12, a6
    TAP a3, s6, F_KERNEL+16, a6
    TAP a3, t1, F_KERNEL+20, a6
    TAP a4, t0, F_KERNEL+24, a6
    TAP a4, s6, F_KERNEL+28, a6
    TAP a4, t1, F_KERNEL+32, a6
    bgez s1, 5f                            # s0 = |s0| + |s1|
    neg s1, s1
5:  bgez s0, 6f
    neg s0, s0
6:  add s0, s0, s1
\base\()_single_kernel:

    lw t2, F_ABS(s8)                     # optional |sum|
    beqz t2, \base\()_no_abs
    bgez s0, \base\()_no_abs
    neg s0, s0
\base\()_no_abs:
    # Divide by d, rounding to nearest with halves away from zero: work on
    # |v| (t2 = sign mask), add d/2, divide, restore the sign. (Without the
    # d/2, repeated passes steadily darken the image.)
    srai t2, s0, XLEN-1                   # t2 = -1 if v < 0 else 0
    xor s0, s0, t2
    sub s0, s0, t2                         # s0 = |v|
    add s0, s0, t4                          # + d/2
    beqz a0, \base\()_no_recip              # d == 1: nothing to divide
    RECIP_MUL s0, s0, a0                      # s0 = floor(s0 / d), exactly
\base\()_no_recip:
    xor s0, s0, t2
    sub s0, s0, t2                         # restore the sign
    add s0, s0, a1                          # + scaled bias
    CLAMP_0_MAX s0                           # clamp to 0..maxval

    add t2, a5, s6
    sh s0, 0(t2)                         # dst[row*W + col] = filtered value

    addi s6, s6, 2
    j \base\()_col_loop
\base\()_col_done:
.endm

# Register use in the fast path:
#   s0..s8  the nine weights, row-major    s9  reciprocal R (0 if d == 1)
#   s10 scaled bias    s11 d/2 (rounding term)
#   t2  last column byte offset 2*(W-1)    t3  maxval    t4  abs flag
#   a0/a1/a2 pointers to the CURRENT pixel's column in the rows above /
#   current / below (advanced one pixel at a time)   a3 destination pointer
#   a5  sum    t0  end pointer of the interior loop    a4, t1  free
#   a6 a7 t5 t6  four temporaries, rotated so each load is followed by
#                independent work before its value is used (the datasheet
#                charges an extra cycle for a dependent instruction)
filter_core_single:
    addi sp, sp, -FRAME_S
    REG_S s0, 0*PTRSIZE
    REG_S s1, 1*PTRSIZE
    REG_S s2, 2*PTRSIZE
    REG_S s3, 3*PTRSIZE
    REG_S s4, 4*PTRSIZE
    REG_S s5, 5*PTRSIZE
    REG_S s6, 6*PTRSIZE
    REG_S s7, 7*PTRSIZE
    REG_S s8, 8*PTRSIZE
    REG_S s9, 9*PTRSIZE
    REG_S s10, 10*PTRSIZE
    REG_S s11, 11*PTRSIZE

    SINGLE_CONSTANTS a4, a6, a2
    REG_S a0, L_SRC
    REG_S a1, L_DST
    REG_S a3, L_H
    REG_S a5, L_PASS

.single_pass:
    REG_L t0, L_PASS
    beqz t0, .single_done
    addi t0, t0, -1
    REG_S t0, L_PASS
    REG_S zero, L_ROW

.single_row:
    REG_L t5, L_ROW                     # y
    REG_L t6, L_H
    bge t5, t6, .single_row_done
    REG_L a6, L_SRC
    REG_L a7, L_DST
    addi t0, t2, 2                      # row stride in bytes
    mul a1, t5, t0                      # y * stride
    add a3, a1, a7                      # a3 = this destination row
    add a1, a1, a6                      # a1 = this source row
    mv a0, a1                           # a0 = row above (clamped to row 0)
    beqz t5, 1f
    sub a0, a1, t0
1:  mv a2, a1                           # a2 = row below (clamped to row H-1)
    addi t5, t5, 1
    bge t5, t6, 2f
    add a2, a1, t0
2:  REG_S t5, L_ROW                     # next row index
    ROW_PIXELS .single, .single_row

.single_row_done:
    REG_L t0, L_SRC                     # swap source and destination
    REG_L t1, L_DST
    REG_S t1, L_SRC
    REG_S t0, L_DST
    j .single_pass

.single_done:
    REG_L a0, L_SRC                     # the buffer holding the final result
    REG_L s0, 0*PTRSIZE
    REG_L s1, 1*PTRSIZE
    REG_L s2, 2*PTRSIZE
    REG_L s3, 3*PTRSIZE
    REG_L s4, 4*PTRSIZE
    REG_L s5, 5*PTRSIZE
    REG_L s6, 6*PTRSIZE
    REG_L s7, 7*PTRSIZE
    REG_L s8, 8*PTRSIZE
    REG_L s9, 9*PTRSIZE
    REG_L s10, 10*PTRSIZE
    REG_L s11, 11*PTRSIZE
    addi sp, sp, FRAME_S
    ret

# ---------------------------------------------------------------------------
# The generic path: handles every filter, including two-kernel ones (sobel).
# Register use inside the generic loops:
#   s0 sum   s1 first sum (sobel)   s2 W   s3 H   s4 maxval
#   s5 row   s6 column byte offset  s7 pass index
#   s8 record  s9 passes  s10 src  s11 dst   a7 row stride in bytes (2*W)
#   a1 scaled bias   a2/a3/a4 source row pointers (above/current/below)
#   a5 destination row pointer   a6 second record   t0/t1 left/right column
filter_core_generic:
    addi sp, sp, -(12*PTRSIZE)
    REG_S s0, 0*PTRSIZE
    REG_S s1, 1*PTRSIZE
    REG_S s2, 2*PTRSIZE
    REG_S s3, 3*PTRSIZE
    REG_S s4, 4*PTRSIZE
    REG_S s5, 5*PTRSIZE
    REG_S s6, 6*PTRSIZE
    REG_S s7, 7*PTRSIZE
    REG_S s8, 8*PTRSIZE
    REG_S s9, 9*PTRSIZE
    REG_S s10, 10*PTRSIZE
    REG_S s11, 11*PTRSIZE

    mv s10, a0
    mv s11, a1
    mv s2, a2
    mv s3, a3
    mv s8, a4
    mv s9, a5
    mv s4, a6
    slli a7, s2, 1                  # row stride in bytes

    GENERIC_CONSTANTS

    li s7, 0                        # pass index
.pass_loop:
    beq s7, s9, .passes_done

    li s5, 0                        # row
.row_loop:
    beq s5, s3, .row_done

    addi t0, s5, -1                  # row above, clamped to 0
    bgez t0, 1f
    li t0, 0
1:  addi t1, s5, 1                    # row below, clamped to H-1
    blt t1, s3, 2f
    addi t1, s3, -1
2:  mul a2, t0, a7                     # a2/a3/a4 = pointers to the 3 source rows
    add a2, a2, s10
    mul a3, s5, a7
    add a5, a3, s11                     # a5 = pointer to this destination row
    add a3, a3, s10
    mul a4, t1, a7
    add a4, a4, s10

    GENERIC_COLUMNS .gen
    addi s5, s5, 1
    j .row_loop
.row_done:

    mv t5, s10                          # swap src/dst for the next pass
    mv s10, s11
    mv s11, t5

    addi s7, s7, 1
    j .pass_loop
.passes_done:

    mv a0, s10                          # s10 = buffer holding the final result
    REG_L s0, 0*PTRSIZE
    REG_L s1, 1*PTRSIZE
    REG_L s2, 2*PTRSIZE
    REG_L s3, 3*PTRSIZE
    REG_L s4, 4*PTRSIZE
    REG_L s5, 5*PTRSIZE
    REG_L s6, 6*PTRSIZE
    REG_L s7, 7*PTRSIZE
    REG_L s8, 8*PTRSIZE
    REG_L s9, 9*PTRSIZE
    REG_L s10, 10*PTRSIZE
    REG_L s11, 11*PTRSIZE
    addi sp, sp, 12*PTRSIZE
    ret

# ---------------------------------------------------------------------------
# Row-level entry points, for streaming. They filter ONE output row given the
# three source rows, so an image never has to be held whole in memory.
#
# void filter_row(a0 = dst,      // W 16-bit samples, written
#                 a1 = above,    // source rows, W samples each; any of the three may
#                 a2 = cur,      // alias another (at the top and bottom edge the
#                 a3 = below,    // caller passes the same row twice); none may alias dst
#                 a4 = W, a5 = filter record, a6 = maxval)
# One pass of the filter on one row, bit-identical to what filter_core produces for
# that row. Preserves s0-s11. The per-call setup (weights, reciprocal) costs well
# under 1% of a 240-sample row.
.global filter_row
filter_row:
    la t0, f_sobel
    beq a5, t0, filter_row_sobel_xy
    la t0, f_sobelx
    beq a5, t0, filter_row_sobel_x
    la t0, f_sobely
    beq a5, t0, filter_row_sobel_y
    lw t0, F_KERNEL2(a5)
    bnez t0, filter_row_generic
    j filter_row_single

filter_row_single:
    addi sp, sp, -(12*PTRSIZE)
    REG_S s0, 0*PTRSIZE
    REG_S s1, 1*PTRSIZE
    REG_S s2, 2*PTRSIZE
    REG_S s3, 3*PTRSIZE
    REG_S s4, 4*PTRSIZE
    REG_S s5, 5*PTRSIZE
    REG_S s6, 6*PTRSIZE
    REG_S s7, 7*PTRSIZE
    REG_S s8, 8*PTRSIZE
    REG_S s9, 9*PTRSIZE
    REG_S s10, 10*PTRSIZE
    REG_S s11, 11*PTRSIZE
    SINGLE_CONSTANTS a5, a6, a4
    mv t0, a0                           # arguments -> the register-weight layout:
    mv a0, a1                           #   a0/a1/a2 = above/current/below, a3 = dst
    mv a1, a2
    mv a2, a3
    mv a3, t0
    ROW_PIXELS .srow, .srow_done
.srow_done:
    REG_L s0, 0*PTRSIZE
    REG_L s1, 1*PTRSIZE
    REG_L s2, 2*PTRSIZE
    REG_L s3, 3*PTRSIZE
    REG_L s4, 4*PTRSIZE
    REG_L s5, 5*PTRSIZE
    REG_L s6, 6*PTRSIZE
    REG_L s7, 7*PTRSIZE
    REG_L s8, 8*PTRSIZE
    REG_L s9, 9*PTRSIZE
    REG_L s10, 10*PTRSIZE
    REG_L s11, 11*PTRSIZE
    addi sp, sp, 12*PTRSIZE
    ret

filter_row_generic:
    addi sp, sp, -(8*PTRSIZE)
    REG_S s0, 0*PTRSIZE
    REG_S s1, 1*PTRSIZE
    REG_S s4, 2*PTRSIZE
    REG_S s6, 3*PTRSIZE
    REG_S s8, 4*PTRSIZE
    mv s8, a5                           # record
    mv s4, a6                           # maxval
    slli a7, a4, 1                      # row stride in bytes
    mv a5, a0                           # arguments -> the generic layout:
    mv a4, a3                           #   a2/a3/a4 = above/current/below, a5 = dst
    mv a3, a2
    mv a2, a1
    GENERIC_CONSTANTS
    GENERIC_COLUMNS .grow
    REG_L s0, 0*PTRSIZE
    REG_L s1, 1*PTRSIZE
    REG_L s4, 2*PTRSIZE
    REG_L s6, 3*PTRSIZE
    REG_L s8, 4*PTRSIZE
    addi sp, sp, 8*PTRSIZE
    ret

# ---------------------------------------------------------------------------
# Sobel fast path: the records f_sobel, f_sobelx and f_sobely (recognised by
# address, so a copy of a record still works, through the generic path).
#
# The table-driven paths spend 9 or 18 multiplies per pixel on weights that are
# only ever -2, -1, 0, 1, 2. Sobel factors into a vertical and a horizontal part:
#     vs(x) = above(x) + 2*cur(x) + below(x)      vd(x) = below(x) - above(x)
#     gx(x) = vs(x+1) - vs(x-1)                   gy(x) = vd(x-1) + 2*vd(x) + vd(x+1)
# so each new column costs three loads and a few adds, and a sliding window of the
# last columns gives both gradients. gy is kept as e(x-1) + e(x) with
# e(x) = vd(x) + vd(x+1). The table's divisor is 4 and bias 0, so the divide with
# rounding is exactly (|gx| + |gy| + 2) >> 2 (the reciprocal multiply for d = 4 is
# a shift), then clamp to maxval; sobelx / sobely use one gradient only. The left
# edge is clamped by starting the window with column 0 twice, the right edge by a
# last pixel that reuses its own column. tests/run_tests.py checks the output against
# the independent Python reference and that the table still says divisor 4, bias 0
# and the weights assumed here.
#
# Register state rotates through three registers (vs) and two (e, vd), so six pixels
# are unrolled and no register is ever moved. The 0-5 pixels that do not fill a group
# run first through SOBEL_STEP_MOVE (which moves registers), so the unrolled loop
# always starts with the register roles of step 0.
#   mode bit 0: the x gradient   bit 1: the y gradient   (1 = sobelx, 2 = sobely, 3 = sobel)
#   s0 s1 s2  vs(x-1), vs(x), vs(x+1)   s3 s4  e(x-1), e(x)   s5 a5  vd(x), vd(x+1)
#   a0 dst   a1 a2 a3 above/current/below   a6 maxval   a7 leftover count   t6 group counter
.equ SOBEL_SHIFT, 2

.macro SOBEL_ABS reg, tmp
.ifdef ZBB
    neg \tmp, \reg
    max \reg, \reg, \tmp
.else
    srai \tmp, \reg, XLEN-1
    xor \reg, \reg, \tmp
    sub \reg, \reg, \tmp
.endif
.endm

# SOBEL_LOADCOL mode, off, N, DN: read the column at byte offset `off` of the three rows.
#   N = above + 2*cur + below (if the x gradient is needed)   DN = below - above (y gradient)
.macro SOBEL_LOADCOL mode, off, N, DN
    lhu t0, \off(a1)
    lhu t2, \off(a3)
.if \mode & 1
    lhu t1, \off(a2)
.endif
.if \mode & 2
    sub \DN, t2, t0
.endif
.if \mode & 1
    add t0, t0, t2
    slli t1, t1, 1
    add \N, t0, t1
.endif
.endm

# SOBEL_OUT mode, off, P, N, EP, EN, DC, DN: finish one pixel and store it at off(a0).
.macro SOBEL_OUT mode, off, P, N, EP, EN, DC, DN
.if \mode & 2
    add \EN, \DC, \DN
.endif
.if \mode == 3
    sub t3, \N, \P
    add t4, \EP, \EN
    SOBEL_ABS t3, t5
    SOBEL_ABS t4, t5
    add t3, t3, t4
.elseif \mode == 1
    sub t3, \N, \P
    SOBEL_ABS t3, t5
.else
    add t3, \EP, \EN
    SOBEL_ABS t3, t5
.endif
    addi t3, t3, 2
    srli t3, t3, SOBEL_SHIFT
.ifdef ZBB
    min t3, t3, a6
.else
    ble t3, a6, 1f
    mv t3, a6
1:
.endif
    sh t3, \off(a0)
.endm

# One pixel, then move the state along and advance the pointers by one sample.
.macro SOBEL_STEP_MOVE mode
    SOBEL_LOADCOL \mode, 0, s2, a5
    SOBEL_OUT \mode, 0, s0, s2, s3, s4, s5, a5
.if \mode & 1
    mv s0, s1
    mv s1, s2
.endif
.if \mode & 2
    mv s3, s4
    mv s5, a5
.endif
    addi a0, a0, 2
    addi a1, a1, 2
    addi a2, a2, 2
    addi a3, a3, 2
.endm

# Step k (0..5) of the unrolled group: the register roles rotate with period 6.
.macro SOBEL_STEP_K mode, k
.if \k == 0
    SOBEL_LOADCOL \mode, 2*\k, s2, a5
    SOBEL_OUT \mode, 2*\k, s0, s2, s3, s4, s5, a5
.elseif \k == 1
    SOBEL_LOADCOL \mode, 2*\k, s0, s5
    SOBEL_OUT \mode, 2*\k, s1, s0, s4, s3, a5, s5
.elseif \k == 2
    SOBEL_LOADCOL \mode, 2*\k, s1, a5
    SOBEL_OUT \mode, 2*\k, s2, s1, s3, s4, s5, a5
.elseif \k == 3
    SOBEL_LOADCOL \mode, 2*\k, s2, s5
    SOBEL_OUT \mode, 2*\k, s0, s2, s4, s3, a5, s5
.elseif \k == 4
    SOBEL_LOADCOL \mode, 2*\k, s0, a5
    SOBEL_OUT \mode, 2*\k, s1, s0, s3, s4, s5, a5
.else
    SOBEL_LOADCOL \mode, 2*\k, s1, s5
    SOBEL_OUT \mode, 2*\k, s2, s1, s4, s3, a5, s5
.endif
.endm

# SOBEL_ROW mode, name: filter_row for one Sobel record. a0 = dst, a1/a2/a3 = above /
# current / below, a4 = W, a5 = record (not needed), a6 = maxval. Preserves s0-s11.
.macro SOBEL_ROW mode, name
\name:
    addi sp, sp, -(8*PTRSIZE)
    REG_S s0, 0*PTRSIZE
    REG_S s1, 1*PTRSIZE
    REG_S s2, 2*PTRSIZE
    REG_S s3, 3*PTRSIZE
    REG_S s4, 4*PTRSIZE
    REG_S s5, 5*PTRSIZE
    SOBEL_LOADCOL \mode, 0, s1, s5          # column 0: vs(0), vd(0)
.if \mode & 1
    mv s0, s1                                # vs(-1) = vs(0): the left edge is clamped
.endif
.if \mode & 2
    slli s3, s5, 1                           # e(-1) = vd(-1) + vd(0) = 2 vd(0)
.endif
    addi a1, a1, 2                           # from here the pointers address column x+1
    addi a2, a2, 2
    addi a3, a3, 2
    addi t6, a4, -1                          # pixels that still take a new column
    li t5, 6
    remu a7, t6, t5                          # a7 = leftover pixels (0..5)
    sub t6, t6, a7                           # t6 = pixels in complete groups of 6
\name\()_rem:
    beqz a7, \name\()_rem_done
    SOBEL_STEP_MOVE \mode
    addi a7, a7, -1
    j \name\()_rem
\name\()_rem_done:
    beqz t6, \name\()_last
\name\()_group:
    SOBEL_STEP_K \mode, 0
    SOBEL_STEP_K \mode, 1
    SOBEL_STEP_K \mode, 2
    SOBEL_STEP_K \mode, 3
    SOBEL_STEP_K \mode, 4
    SOBEL_STEP_K \mode, 5
    addi a0, a0, 12
    addi a1, a1, 12
    addi a2, a2, 12
    addi a3, a3, 12
    addi t6, t6, -6
    bnez t6, \name\()_group
\name\()_last:                               # the last pixel reuses its own column
    SOBEL_OUT \mode, 0, s0, s1, s3, s4, s5, s5
    REG_L s0, 0*PTRSIZE
    REG_L s1, 1*PTRSIZE
    REG_L s2, 2*PTRSIZE
    REG_L s3, 3*PTRSIZE
    REG_L s4, 4*PTRSIZE
    REG_L s5, 5*PTRSIZE
    addi sp, sp, 8*PTRSIZE
    ret
.endm

SOBEL_ROW 3, filter_row_sobel_xy
SOBEL_ROW 1, filter_row_sobel_x
SOBEL_ROW 2, filter_row_sobel_y

# filter_core for the Sobel records: every pass, every row through filter_row (the
# per-row setup is a few dozen cycles against ~1000 for a row, so nothing is lost).
# Registers: s0 src  s1 dst  s2 W  s3 H  s4 record  s5 passes  s6 maxval  s7 pass
# s8 row  s9 row stride in bytes
filter_core_sobel:
    addi sp, sp, -(12*PTRSIZE)
    REG_S ra, 0*PTRSIZE
    REG_S s0, 1*PTRSIZE
    REG_S s1, 2*PTRSIZE
    REG_S s2, 3*PTRSIZE
    REG_S s3, 4*PTRSIZE
    REG_S s4, 5*PTRSIZE
    REG_S s5, 6*PTRSIZE
    REG_S s6, 7*PTRSIZE
    REG_S s7, 8*PTRSIZE
    REG_S s8, 9*PTRSIZE
    REG_S s9, 10*PTRSIZE
    mv s0, a0
    mv s1, a1
    mv s2, a2
    mv s3, a3
    mv s4, a4
    mv s5, a5
    mv s6, a6
    slli s9, s2, 1
    li s7, 0
.sobel_pass:
    beq s7, s5, .sobel_done
    li s8, 0
.sobel_row:
    beq s8, s3, .sobel_row_done
    addi t0, s8, -1                      # row above, clamped to 0
    bgez t0, 1f
    li t0, 0
1:  addi t1, s8, 1                        # row below, clamped to H-1
    blt t1, s3, 2f
    addi t1, s3, -1
2:  mul a1, t0, s9
    add a1, a1, s0                         # above
    mul a2, s8, s9
    add a0, a2, s1                          # destination row
    add a2, a2, s0                           # current
    mul a3, t1, s9
    add a3, a3, s0                            # below
    mv a4, s2
    mv a5, s4
    mv a6, s6
    call filter_row
    addi s8, s8, 1
    j .sobel_row
.sobel_row_done:
    mv t0, s0                              # swap src / dst for the next pass
    mv s0, s1
    mv s1, t0
    addi s7, s7, 1
    j .sobel_pass
.sobel_done:
    mv a0, s0                              # s0 = buffer holding the final result
    REG_L ra, 0*PTRSIZE
    REG_L s0, 1*PTRSIZE
    REG_L s1, 2*PTRSIZE
    REG_L s2, 3*PTRSIZE
    REG_L s3, 4*PTRSIZE
    REG_L s4, 5*PTRSIZE
    REG_L s5, 6*PTRSIZE
    REG_L s6, 7*PTRSIZE
    REG_L s7, 8*PTRSIZE
    REG_L s8, 9*PTRSIZE
    REG_L s9, 10*PTRSIZE
    addi sp, sp, 12*PTRSIZE
    ret

# void window_level(a0 = src,     // n unsigned 16-bit samples
#                   a1 = dst,     // n bytes
#                   a2 = n, a3 = center, a4 = width)   // width >= 1
# Maps samples to 8-bit display values with the DICOM linear window
# function (PS3.3 C.11.2.1.2), output range 0..255:
#     x <= c - 0.5 - (w-1)/2        ->   0
#     x >  c - 0.5 + (w-1)/2        ->   255
#     otherwise  y = ((x - (c - 0.5)) / (w - 1) + 0.5) * 255, rounded
# In integers, with N = 2x - 2c + w:  N <= 0 -> 0;  N > 2(w-1) -> 255;
# else y = (N*255 + (w-1)) / (2(w-1)).  (Width 1 is a hard threshold.)
# Leaf function: uses only caller-saved registers.
window_level:
    slli t0, a3, 1                  # 2c
    sub t0, a4, t0                   # t0 = w - 2c, so N = 2x + t0
    addi t1, a4, -1                   # t1 = w - 1 (the rounding term)
    slli t2, t1, 1                     # t2 = 2(w - 1)
    li t3, 255
.wl_loop:
    beqz a2, .wl_done
    lhu t4, 0(a0)
    slli t5, t4, 1
    add t5, t5, t0                       # t5 = N
    blez t5, .wl_black
    bgt t5, t2, .wl_white                 # also catches every N > 0 when w == 1
    mul t5, t5, t3
    add t5, t5, t1
    div t5, t5, t2                         # t2 > 0 here (0 < N <= t2)
    sb t5, 0(a1)
    j .wl_next
.wl_black:
    sb zero, 0(a1)
    j .wl_next
.wl_white:
    sb t3, 0(a1)
.wl_next:
    addi a0, a0, 2
    addi a1, a1, 1
    addi a2, a2, -1
    j .wl_loop
.wl_done:
    ret
