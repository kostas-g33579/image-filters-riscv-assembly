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
# Edge taps clamp to the nearest valid pixel.
.include "records.inc"

.section .rodata
n_identity: .asciz "identity"
n_blur:     .asciz "blur"
n_gaussian: .asciz "gaussian"
n_sharpen:  .asciz "sharpen"
n_edge:     .asciz "edge"
n_emboss:   .asciz "emboss"
n_sobel:    .asciz "sobel"
n_sobelx:   .asciz "sobelx"
n_sobely:   .asciz "sobely"

# name, 9 kernel weights, divisor, bias, abs flag, default passes,
# second-kernel offset (0 = none)
.balign 8
.global filter_table
.global f_blur
filter_table:
f_identity: .dword n_identity
            .word 0,0,0, 0,1,0, 0,0,0
            .word 1, 0, 0, 1, 0
f_blur:     .dword n_blur
            .word 1,1,1, 1,1,1, 1,1,1
            .word 9, 0, 0, 8, 0
f_gaussian: .dword n_gaussian
            .word 1,2,1, 2,4,2, 1,2,1
            .word 16, 0, 0, 4, 0
f_sharpen:  .dword n_sharpen
            .word 0,-1,0, -1,5,-1, 0,-1,0
            .word 1, 0, 0, 1, 0
f_edge:     .dword n_edge                       # abs: shows both sides of an edge
            .word -1,-1,-1, -1,8,-1, -1,-1,-1
            .word 1, 0, 1, 1, 0
f_emboss:   .dword n_emboss                      # zero-sum: flat areas -> mid-gray
            .word -2,-1,0, -1,0,1, 0,1,2
            .word 1, 128, 0, 1, 0
f_sobel:    .dword n_sobel                       # |gx| + |gy|
            .word -1,0,1, -2,0,2, -1,0,1
            .word 4, 0, 1, 1, f_sobely - f_sobel
f_sobelx:   .dword n_sobelx
            .word -1,0,1, -2,0,2, -1,0,1
            .word 4, 0, 1, 1, 0
f_sobely:   .dword n_sobely
            .word -1,-2,-1, 0,0,0, 1,2,1
            .word 4, 0, 1, 1, 0
            .dword 0                # table terminator (null name)

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

# Register use inside the loops:
#   s0 sum   s1 first sum (sobel)   s2 W   s3 H   s4 maxval
#   s5 row   s6 column byte offset  s7 pass index
#   s8 record  s9 passes  s10 src  s11 dst   a7 row stride in bytes (2*W)
#   a1 scaled bias   a2/a3/a4 source row pointers (above/current/below)
#   a5 destination row pointer   a6 second record   t0/t1 left/right column
filter_core:
    addi sp, sp, -96
    sd s0, 0(sp)
    sd s1, 8(sp)
    sd s2, 16(sp)
    sd s3, 24(sp)
    sd s4, 32(sp)
    sd s5, 40(sp)
    sd s6, 48(sp)
    sd s7, 56(sp)
    sd s8, 64(sp)
    sd s9, 72(sp)
    sd s10, 80(sp)
    sd s11, 88(sp)

    mv s10, a0
    mv s11, a1
    mv s2, a2
    mv s3, a3
    mv s8, a4
    mv s9, a5
    mv s4, a6
    slli a7, s2, 1                  # row stride in bytes

    lw a1, F_BIAS(s8)               # scaled bias = bias * (maxval+1) / 256
    addi t0, s4, 1
    mul a1, a1, t0
    srai a1, a1, 8

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

    li s6, 0                         # column byte offset
.col_loop:
    beq s6, a7, .col_done

    addi t0, s6, -2                   # left column, clamped to 0
    bgez t0, 3f
    li t0, 0
3:  addi t1, s6, 2                     # right column, clamped to W-1
    blt t1, a7, 4f
    addi t1, a7, -2
4:
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
    beqz a6, .single_kernel
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
.single_kernel:

    lw t2, F_ABS(s8)                     # optional |sum|
    beqz t2, .no_abs
    bgez s0, .no_abs
    neg s0, s0
.no_abs:
    lw t2, F_DIV(s8)                      # divide, rounding to nearest with
    srli t3, t2, 1                         # halves away from zero: add (or,
    bltz s0, .round_neg                     # for negatives, subtract) div/2
    add s0, s0, t3                           # before the truncating divide
    j .round_done                             # (without this, repeated passes
.round_neg:                                    # steadily darken the image)
    sub s0, s0, t3
.round_done:
    div s0, s0, t2                        # signed, truncates toward zero
    add s0, s0, a1                         # + scaled bias
    bgez s0, .no_clamp_lo                  # clamp to 0..maxval
    li s0, 0
.no_clamp_lo:
    ble s0, s4, .no_clamp_hi
    mv s0, s4
.no_clamp_hi:

    add t2, a5, s6
    sh s0, 0(t2)                         # dst[row*W + col] = filtered value

    addi s6, s6, 2
    j .col_loop
.col_done:
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
    ld s0, 0(sp)
    ld s1, 8(sp)
    ld s2, 16(sp)
    ld s3, 24(sp)
    ld s4, 32(sp)
    ld s5, 40(sp)
    ld s6, 48(sp)
    ld s7, 56(sp)
    ld s8, 64(sp)
    ld s9, 72(sp)
    ld s10, 80(sp)
    ld s11, 88(sp)
    addi sp, sp, 96
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
