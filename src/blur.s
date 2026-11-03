# 3x3 box blur on a real PGM (P5, raw grayscale) image file, repeated
# PASS_COUNT times for a visible blur radius at up to 256x256. 
# Uses Linux syscalls for file I/O: 
# openat=56, close=57, read=63, write=64, exit=93.
.equ MAX_PIXELS, 65536      # up to 256x256
.equ PASS_COUNT, 8          # repeated 3x3 passes -> a visible blur radius
                             # at 256x256, where a single 3x3 pass alone
                             # would be imperceptible (see README)

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

.section .bss
.align 3
char_buf:   .skip 1
raw_in:     .skip MAX_PIXELS
raw_out:    .skip MAX_PIXELS
pixel_in:   .skip MAX_PIXELS*4
pixel_out:  .skip MAX_PIXELS*4
header_buf: .skip 32

.section .text
.global _start

_start:
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
    la a1, raw_in              # follows (\n, \r\n, space, comment...) is
    li a2, 2                   # skipped by read_uint's whitespace/comment
    li a7, 63                  # handling before the width field.
    ecall
    li t0, 2
    blt a0, t0, .bad_header     # error or EOF before the magic number
    la t0, raw_in
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
.px_read:                           # fewer bytes than asked, so loop
    bgeu s5, s4, .px_read_done
    mv a0, s0
    la a1, raw_in
    add a1, a1, s5
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

    # ---- unpack raw bytes -> word array ----
    la s10, pixel_in
    la t0, raw_in
    li t1, 0
.unpack_loop:
    beq t1, s4, .unpack_done
    add t2, t0, t1
    lbu t3, 0(t2)
    slli t4, t1, 2
    add t4, t4, s10
    sw t3, 0(t4)
    addi t1, t1, 1
    j .unpack_loop
.unpack_done:

    la s11, pixel_out

    # ---- PASS_COUNT repeated 3x3 box blur passes, clamp-to-edge ----
    # s10=src, s11=dst for the pass about to run;
    li s7, 0                       # pass index
.pass_loop:
    li t5, PASS_COUNT
    beq s7, t5, .passes_done

    li s5, 0                        # row
.row_loop:
    beq s5, s3, .row_done
    li s6, 0                         # col
.col_loop:
    beq s6, s2, .col_done

    li s0, 0                          # sum (fd_in is closed, s0 is free)

    addi a0, s5, -1
    addi a1, s6, -1
    jal ra, sample
    add s0, s0, a0

    addi a0, s5, -1
    mv   a1, s6
    jal ra, sample
    add s0, s0, a0

    addi a0, s5, -1
    addi a1, s6, 1
    jal ra, sample
    add s0, s0, a0

    mv   a0, s5
    addi a1, s6, -1
    jal ra, sample
    add s0, s0, a0

    mv   a0, s5
    mv   a1, s6
    jal ra, sample
    add s0, s0, a0

    mv   a0, s5
    addi a1, s6, 1
    jal ra, sample
    add s0, s0, a0

    addi a0, s5, 1
    addi a1, s6, -1
    jal ra, sample
    add s0, s0, a0

    addi a0, s5, 1
    mv   a1, s6
    jal ra, sample
    add s0, s0, a0

    addi a0, s5, 1
    addi a1, s6, 1
    jal ra, sample
    add s0, s0, a0

    li t0, 9
    divu s0, s0, t0                    # average

    mul t1, s5, s2                      # index = row*W + col
    add t1, t1, s6
    slli t1, t1, 2
    add t1, t1, s11
    sw s0, 0(t1)                         # dst[index] = average

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

    # ---- pack word array -> raw bytes, write, close, exit ----
    la t0, raw_out
    li t1, 0
.pack_loop:
    beq t1, s4, .pack_done
    slli t2, t1, 2
    add t2, t2, s10          # s10 = final buffer after the last pass swap
    lw t3, 0(t2)
    add t4, t0, t1
    sb t3, 0(t4)
    addi t1, t1, 1
    j .pack_loop
.pack_done:

    mv a0, s1
    la a1, raw_out
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

# clamp: clamps idx into [0, limit-1]. Leaf function, no stack needed.
# in: a0=idx, a1=limit  out: a0=clamped idx
clamp:
    bltz a0, .cl_neg
    bge a0, a1, .cl_over
    ret
.cl_neg:
    li a0, 0
    ret
.cl_over:
    addi a0, a1, -1
    ret

# sample: reads the current source buffer at (row,col), clamping both
# coordinates first, so out-of-range taps read the nearest edge pixel
# instead of garbage memory.
# in: a0=row (may be -1 or H), a1=col (may be -1 or W)  out: a0=pixel value
# needs: s2=W, s3=H, s10=&current source buffer  clobbers: t0,t1
sample:
    addi sp, sp, -16
    sd ra, 8(sp)
    sd a1, 0(sp)
    mv a1, s3               # limit = H
    jal ra, clamp
    mv t0, a0                # t0 = clamped row
    ld a0, 0(sp)               # restore col
    mv a1, s2                    # limit = W
    jal ra, clamp                 # a0 = clamped col
    mul t1, t0, s2
    add t1, t1, a0
    slli t1, t1, 2
    add t1, t1, s10
    lw a0, 0(t1)
    ld ra, 8(sp)
    addi sp, sp, 16
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
