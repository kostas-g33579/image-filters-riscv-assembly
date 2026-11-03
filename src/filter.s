# Generic 3x3 kernel convolution filter on a real PGM (P5, raw grayscale)
# image file, up to 256x256. The filter is chosen by name on the command
# line (see the filter table below); each filter is a 3x3 kernel plus a
# divisor, bias, an optional absolute-value step and a default pass count.
#
#   filter [name [passes]]       (default: blur, filter's default passes)
#
# Per pixel:  v = sum(kernel[i] * tap[i]);  if abs: v = |v|;
#             (if the record has a second kernel: v = |v| + |sum2|);
#             v = v / divisor (rounded to nearest, halves away from zero);
#             v += bias;  clamp to 0..255.
# Edge taps clamp to the nearest valid pixel.
#
# Uses Linux syscalls for file I/O:
# openat=56, close=57, read=63, write=64, exit=93.
.equ MAX_PIXELS, 65536      # up to 256x256
.equ MAX_PASSES, 255

# Filter record layout (72 bytes each, see filter_table):
.equ F_NAME,   0            # .dword pointer to NUL-terminated name
.equ F_KERNEL, 8            # 9 x .word signed weights, row-major
.equ F_DIV,    44           # .word divisor (non-zero)
.equ F_BIAS,   48           # .word added after division
.equ F_ABS,    52           # .word nonzero: take |sum| before dividing
.equ F_PASSES, 56           # .word default number of passes
                            # (offset 60: .word padding)
.equ F_KERNEL2, 64          # .dword pointer to a second filter record, or 0.
                            # If set, the pixel value is |sum with this
                            # kernel| + |sum with that record's kernel|
                            # (gradient magnitude, used by `sobel`).
.equ F_SIZE,   72

.section .data
in_path:  .asciz "images/input.pgm"
out_path: .asciz "images/output.pgm"
err_too_big: .ascii "error: image exceeds MAX_PIXELS (65536)\n"
.equ err_too_big_len, . - err_too_big
err_open_in: .ascii "error: cannot open images/input.pgm\n"
.equ err_open_in_len, . - err_open_in
err_open_out: .ascii "error: cannot open images/output.pgm for writing\n"
.equ err_open_out_len, . - err_open_out
err_header: .ascii "error: bad or truncated PGM header\n"
.equ err_header_len, . - err_header
err_trunc: .ascii "error: truncated pixel data\n"
.equ err_trunc_len, . - err_trunc
err_maxval: .ascii "error: unsupported maxval (only 1..255, 8-bit PGM)\n"
.equ err_maxval_len, . - err_maxval
err_dims: .ascii "error: width and height must be non-zero\n"
.equ err_dims_len, . - err_dims
err_write: .ascii "error: failed writing images/output.pgm\n"
.equ err_write_len, . - err_write
err_usage: .ascii "usage: filter [name [passes]]\nfilters: identity blur gaussian sharpen edge emboss sobel sobelx sobely\npasses: 0..255\n"
.equ err_usage_len, . - err_usage

n_identity: .asciz "identity"
n_blur:     .asciz "blur"
n_gaussian: .asciz "gaussian"
n_sharpen:  .asciz "sharpen"
n_edge:     .asciz "edge"
n_emboss:   .asciz "emboss"
n_sobel:    .asciz "sobel"
n_sobelx:   .asciz "sobelx"
n_sobely:   .asciz "sobely"

# name, 9 kernel weights, divisor, bias, abs flag, default passes, pad,
# second-kernel record pointer
.balign 8
filter_table:
f_identity: .dword n_identity
            .word 0,0,0, 0,1,0, 0,0,0
            .word 1, 0, 0, 1, 0
            .dword 0
f_blur:     .dword n_blur
            .word 1,1,1, 1,1,1, 1,1,1
            .word 9, 0, 0, 8, 0
            .dword 0
f_gaussian: .dword n_gaussian
            .word 1,2,1, 2,4,2, 1,2,1
            .word 16, 0, 0, 4, 0
            .dword 0
f_sharpen:  .dword n_sharpen
            .word 0,-1,0, -1,5,-1, 0,-1,0
            .word 1, 0, 0, 1, 0
            .dword 0
f_edge:     .dword n_edge                       # abs: shows both sides of an edge
            .word -1,-1,-1, -1,8,-1, -1,-1,-1
            .word 1, 0, 1, 1, 0
            .dword 0
f_emboss:   .dword n_emboss                      # zero-sum: flat areas -> 128
            .word -2,-1,0, -1,0,1, 0,1,2
            .word 1, 128, 0, 1, 0
            .dword 0
f_sobel:    .dword n_sobel                       # |gx| + |gy|
            .word -1,0,1, -2,0,2, -1,0,1
            .word 4, 0, 1, 1, 0
            .dword f_sobely
f_sobelx:   .dword n_sobelx
            .word -1,0,1, -2,0,2, -1,0,1
            .word 4, 0, 1, 1, 0
            .dword 0
f_sobely:   .dword n_sobely
            .word -1,-2,-1, 0,0,0, 1,2,1
            .word 4, 0, 1, 1, 0
            .dword 0
            .dword 0                # table terminator (null name)

.section .bss
.align 3
char_buf:   .skip 8             # 1 byte for read_uint, 2 for the magic number
buf_a:      .skip MAX_PIXELS    # 8-bit pixels, row-major; the two buffers
buf_b:      .skip MAX_PIXELS    # ping-pong between passes
header_buf: .skip 32

# TAP rowptr, colreg, kernel_offset [, record]: sum += rowptr[colreg] * weight
# from the given filter record (default s8). Clobbers t2, t3; accumulates
# into s0.
.macro TAP rowptr, colreg, koff, rec=s8
    add t2, \rowptr, \colreg
    lbu t2, 0(t2)
    lw t3, \koff(\rec)
    mul t2, t2, t3
    add s0, s0, t2
.endm

.section .text
.global _start

_start:
    # Initialize gp. There is no libc startup code to do it, and the linker
    # relaxes `la` of nearby symbols into gp-relative addressing, which
    # would silently read/write near address 0 if gp were left as 0.
.option push
.option norelax
    la gp, __global_pointer$
.option pop

    # ---- parse command line: filter name + optional pass count ----
    # Linux process entry: sp -> argc, argv[0], argv[1], ...
    ld t0, 0(sp)                    # argc
    la s8, f_blur                    # default filter
    li t1, 2
    blt t0, t1, .args_filter_done     # no name given: blur
    ld s9, 16(sp)                      # argv[1] (s9 temporarily = name)
    la s8, filter_table
.find_filter:
    ld t2, F_NAME(s8)
    beqz t2, .usage                    # hit terminator: unknown filter
    mv a0, s9
    mv a1, t2
    jal ra, strcmp
    beqz a0, .args_filter_done
    addi s8, s8, F_SIZE
    j .find_filter
.args_filter_done:
    lw s9, F_PASSES(s8)                # default passes for this filter
    ld t0, 0(sp)
    li t1, 3
    blt t0, t1, .args_done
    ld a0, 24(sp)                       # argv[2]: passes
    jal ra, parse_uint
    bltz a0, .usage
    mv s9, a0
.args_done:

    # ---- open input, parse header ----
    li a0, -100             # AT_FDCWD
    la a1, in_path
    li a2, 0                # O_RDONLY
    li a3, 0
    li a7, 56                # openat
    ecall
    bltz a0, .open_in_fail
    mv s0, a0                 # fd_in

    mv a0, s0                  # magic number: exactly "P5" (2 bytes). What
    la a1, char_buf            # follows (\n, \r\n, space, comment...) is
    li a2, 2                   # skipped by read_uint's whitespace/comment
    li a7, 63                  # handling before the width field.
    ecall
    li t0, 2
    blt a0, t0, .bad_header     # error or EOF before the magic number
    la t0, char_buf
    lbu t1, 0(t0)
    li t2, 0x50                  # 'P'
    bne t1, t2, .bad_header
    lbu t1, 1(t0)
    li t2, 0x35                  # '5'
    bne t1, t2, .bad_header

    mv a0, s0
    jal ra, read_uint
    mv s2, a0                   # W

    mv a0, s0
    jal ra, read_uint
    mv s3, a0                    # H

    mv a0, s0
    jal ra, read_uint            # maxval: validated, otherwise unused
    beqz a0, .bad_maxval          # 0 is invalid
    li t0, 255
    bgtu a0, t0, .bad_maxval       # >255 means 16-bit samples: unsupported

    mul s4, s2, s3                # total_pixels = W*H

    beqz s2, .bad_dims             # zero width or height is not an image
    beqz s3, .bad_dims
    li t0, MAX_PIXELS              # reject images that would overflow buffers
    bgtu s4, t0, .too_big
    li t0, 0x100000000             # W and H each < 2^32 so W*H can't wrap
    bgeu s2, t0, .too_big
    bgeu s3, t0, .too_big

    li s5, 0                        # bytes read so far; read() may return
    la s10, buf_a                   # pixels are read straight into buf_a
.px_read:                           # fewer bytes than asked, so loop
    bgeu s5, s4, .px_read_done
    mv a0, s0
    add a1, s10, s5
    sub a2, s4, s5
    li a7, 63
    ecall
    blez a0, .truncated              # error or EOF before all pixels arrived
    add s5, s5, a0
    j .px_read
.px_read_done:

    mv a0, s0
    li a7, 57                       # close(fd_in)
    ecall

    la s11, buf_b

    # ---- s9 passes of the selected 3x3 kernel, clamp-to-edge ----
    # s8=filter record, s9=passes, s10=src, s11=dst for the pass about to run.
    # No calls in the loop: edge clamping is done once per row (the three
    # source row pointers) and once per pixel (left/right column), then the
    # nine taps are straight byte loads at fixed offsets.
    li s7, 0                       # pass index
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
2:  mul a2, t0, s2                     # a2/a3/a4 = pointers to the 3 source rows
    add a2, a2, s10
    mul a3, s5, s2
    add a5, a3, s11                     # a5 = pointer to this destination row
    add a3, a3, s10
    mul a4, t1, s2
    add a4, a4, s10

    li s6, 0                         # col
.col_loop:
    beq s6, s2, .col_done

    addi t0, s6, -1                   # left column, clamped to 0
    bgez t0, 3f
    li t0, 0
3:  addi t1, s6, 1                     # right column, clamped to W-1
    blt t1, s2, 4f
    addi t1, s2, -1
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

    ld a6, F_KERNEL2(s8)                 # gradient magnitude: second kernel?
    beqz a6, .single_kernel
    mv s1, s0                             # s1 = first sum
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
    lw t2, F_BIAS(s8)
    add s0, s0, t2
    bgez s0, .no_clamp_lo                  # clamp to 0..255
    li s0, 0
.no_clamp_lo:
    li t2, 255
    ble s0, t2, .no_clamp_hi
    mv s0, t2
.no_clamp_hi:

    add t2, a5, s6
    sb s0, 0(t2)                         # dst[row*W + col] = filtered value

    addi s6, s6, 1
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

    # ---- open output ----
    li a0, -100
    la a1, out_path
    li a2, 577               # O_WRONLY|O_CREAT|O_TRUNC (1|64|512)
    li a3, 420                # 0644
    li a7, 56
    ecall
    bltz a0, .open_out_fail
    mv s1, a0                  # fd_out

    # ---- build "P5\n<W> <H>\n255\n" header text ----
    la s5, header_buf
    li t1, 0x50                 # 'P'
    sb t1, 0(s5)
    li t1, 0x35                  # '5'
    sb t1, 1(s5)
    li t1, 0x0A                   # '\n'
    sb t1, 2(s5)
    li s6, 3                       # header length so far

    mv a0, s2                       # W
    add a1, s5, s6
    jal ra, itoa
    add s6, s6, a0

    add t3, s5, s6
    li t1, 0x20                       # ' '
    sb t1, 0(t3)
    addi s6, s6, 1

    mv a0, s3                          # H
    add a1, s5, s6
    jal ra, itoa
    add s6, s6, a0

    add t3, s5, s6
    li t1, 0x0A
    sb t1, 0(t3)
    addi s6, s6, 1

    add t3, s5, s6                      # "255\n"
    li t1, 0x32
    sb t1, 0(t3)
    li t1, 0x35
    sb t1, 1(t3)
    li t1, 0x35
    sb t1, 2(t3)
    li t1, 0x0A
    sb t1, 3(t3)
    addi s6, s6, 4

    mv a0, s1
    mv a1, s5
    mv a2, s6
    jal ra, write_all                    # write header

    # ---- write pixels, close, exit ----
    mv a0, s1
    mv a1, s10                              # s10 = final buffer after the last swap
    mv a2, s4
    jal ra, write_all                      # write pixel bytes

    mv a0, s1
    li a7, 57                                # close(fd_out)
    ecall
    bnez a0, .write_fail                      # close can report deferred errors

    li a0, 0
    li a7, 93                                 # exit(0)
    ecall

# Error exits: print a message to stderr, exit(1).
.too_big:
    la a1, err_too_big
    li a2, err_too_big_len
    j die
.open_in_fail:
    la a1, err_open_in
    li a2, err_open_in_len
    j die
.open_out_fail:
    la a1, err_open_out
    li a2, err_open_out_len
    j die
.bad_header:
    la a1, err_header
    li a2, err_header_len
    j die
.usage:
    la a1, err_usage
    li a2, err_usage_len
    j die
.bad_maxval:
    la a1, err_maxval
    li a2, err_maxval_len
    j die
.bad_dims:
    la a1, err_dims
    li a2, err_dims_len
    j die
.write_fail:
    la a1, err_write
    li a2, err_write_len
    j die
.truncated:
    la a1, err_trunc
    li a2, err_trunc_len
die:                                # in: a1=msg, a2=len
    li a0, 2                        # stderr
    li a7, 64                        # write
    ecall
    li a0, 1
    li a7, 93                        # exit(1)
    ecall

# ---- subroutines ----

# read_uint: skips whitespace and '#'-to-end-of-line comments, then reads
# decimal ASCII digits one byte at a time from fd until a non-digit is hit. 
# in: a0=fd  out: a0=parsed value  clobbers: t0-t4
# On read error or EOF inside the header, exits via .bad_header.
read_uint:
    mv t4, a0
    li t0, 0
.ru_skip:
    mv a0, t4
    la a1, char_buf
    li a2, 1
    li a7, 63
    ecall
    blez a0, .bad_header      # read error or EOF mid-header
    lbu t1, char_buf
    li t2, 0x23             # '#' -> comment to end of line
    beq t1, t2, .ru_comment
    li t2, 0x20              # ' '
    beq t1, t2, .ru_skip
    li t2, 0x09               # '\t'
    beq t1, t2, .ru_skip
    li t2, 0x0A                # '\n'
    beq t1, t2, .ru_skip
    li t2, 0x0D                 # '\r'
    beq t1, t2, .ru_skip
    j .ru_digit                  # first non-whitespace byte: start of number
.ru_comment:
    mv a0, t4
    la a1, char_buf
    li a2, 1
    li a7, 63
    ecall
    blez a0, .bad_header      # read error or EOF mid-header
    lbu t1, char_buf
    li t2, 0x0A
    bne t1, t2, .ru_comment
    j .ru_skip
.ru_loop:
    mv a0, t4
    la a1, char_buf
    li a2, 1
    li a7, 63
    ecall
    blez a0, .bad_header      # read error or EOF mid-header
    lbu t1, char_buf
.ru_digit:
    li t2, 0x30
    blt t1, t2, .ru_done
    li t2, 0x39
    bgt t1, t2, .ru_done
    addi t1, t1, -0x30
    li t2, 10
    mul t0, t0, t2
    add t0, t0, t1
    j .ru_loop
.ru_done:
    mv a0, t0
    ret

# write_all: writes exactly len bytes, looping over short writes. Exits via
# .write_fail on error (or a write that makes no progress).
# in: a0=fd, a1=buf, a2=len  clobbers: a0-a2, a7, t0-t2
write_all:
    mv t0, a0
    mv t1, a1
    mv t2, a2
.wa_loop:
    beqz t2, .wa_done
    mv a0, t0
    mv a1, t1
    mv a2, t2
    li a7, 64
    ecall
    blez a0, .write_fail
    add t1, t1, a0
    sub t2, t2, a0
    j .wa_loop
.wa_done:
    ret

# strcmp: compares two NUL-terminated strings.
# in: a0, a1  out: a0 = 0 if equal, nonzero otherwise  clobbers: t0, t1
strcmp:
.sc_loop:
    lbu t0, 0(a0)
    lbu t1, 0(a1)
    bne t0, t1, .sc_ne
    beqz t0, .sc_eq
    addi a0, a0, 1
    addi a1, a1, 1
    j .sc_loop
.sc_ne:
    li a0, 1
    ret
.sc_eq:
    li a0, 0
    ret

# parse_uint: parses a NUL-terminated decimal string, value 0..MAX_PASSES.
# in: a0=string  out: a0=value, or -1 if empty, non-numeric or too large
# clobbers: t0-t2
parse_uint:
    lbu t0, 0(a0)
    beqz t0, .pu_bad                    # empty string
    li t1, 0
.pu_loop:
    lbu t0, 0(a0)
    beqz t0, .pu_ok
    addi t0, t0, -0x30
    bltz t0, .pu_bad
    li t2, 9
    bgtu t0, t2, .pu_bad
    li t2, 10
    mul t1, t1, t2
    add t1, t1, t0
    li t2, MAX_PASSES
    bgtu t1, t2, .pu_bad                 # checked every digit: no overflow
    addi a0, a0, 1
    j .pu_loop
.pu_ok:
    mv a0, t1
    ret
.pu_bad:
    li a0, -1
    ret

# itoa: unsigned value -> decimal ASCII digits 
# in: a0=value, a1=dest buffer  out: a0=number of bytes written
# clobbers: t0-t6, uses 16 bytes of stack as scratch
itoa:
    addi sp, sp, -16
    mv t0, a0
    mv t5, a1
    li t1, 0
    mv t4, sp
    bnez t0, .itoa_loop
    li t2, 0x30
    sb t2, 0(t4)
    addi t1, t1, 1
    j .itoa_reverse
.itoa_loop:
    beqz t0, .itoa_reverse
    li t3, 10
    remu t2, t0, t3
    divu t0, t0, t3
    addi t2, t2, 0x30
    add t6, t4, t1
    sb t2, 0(t6)
    addi t1, t1, 1
    j .itoa_loop
.itoa_reverse:
    li t2, 0
.itoa_copy:
    beq t2, t1, .itoa_done
    sub t3, t1, t2
    addi t3, t3, -1
    add t6, t4, t3
    lbu t3, 0(t6)
    add t6, t5, t2
    sb t3, 0(t6)
    addi t2, t2, 1
    j .itoa_copy
.itoa_done:
    mv a0, t1
    addi sp, sp, 16
    ret
