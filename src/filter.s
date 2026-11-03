# Linux test harness for the filter core (kernel.s): command line, PGM file
# I/O and sample conversion around filter_core. Reads images/input.pgm,
# writes images/output.pgm. Supports P5 PGM with maxval 1..65535: 1 byte per
# sample if maxval < 256, else 2 bytes big-endian (as the PGM format says).
#
#   filter [name] [passes] [window CENTER WIDTH]
#       name     filter (default: blur)
#       passes   0..255 (default: the filter's own default)
#       window   after filtering, map to an 8-bit image with the DICOM linear
#                window function (CENTER 0..65535, WIDTH 1..65535); the
#                output is then maxval 255 regardless of input depth.
#       passes and "window C W" may come in either order after the name.
#
# Uses Linux syscalls for file I/O:
# openat=56, close=57, read=63, write=64, exit=93.
.include "records.inc"
.include "xlen.inc"

.equ MAX_PIXELS, 16777216   # up to 4096x4096
.equ MAX_PASSES, 255

.section .data
s_window: .asciz "window"
in_path:  .asciz "images/input.pgm"
out_path: .asciz "images/output.pgm"
err_too_big: .ascii "error: image too large (max 16777216 pixels)\n"
.equ err_too_big_len, . - err_too_big
err_open_in: .ascii "error: cannot open images/input.pgm\n"
.equ err_open_in_len, . - err_open_in
err_open_out: .ascii "error: cannot open images/output.pgm for writing\n"
.equ err_open_out_len, . - err_open_out
err_header: .ascii "error: bad or truncated PGM header\n"
.equ err_header_len, . - err_header
err_trunc: .ascii "error: truncated pixel data\n"
.equ err_trunc_len, . - err_trunc
err_maxval: .ascii "error: unsupported maxval (only 1..65535)\n"
.equ err_maxval_len, . - err_maxval
err_sample: .ascii "error: pixel value exceeds maxval\n"
.equ err_sample_len, . - err_sample
err_dims: .ascii "error: width and height must be non-zero\n"
.equ err_dims_len, . - err_dims
err_write: .ascii "error: failed writing images/output.pgm\n"
.equ err_write_len, . - err_write
err_usage: .ascii "usage: filter [name] [passes] [window CENTER WIDTH]\nfilters: identity blur gaussian sharpen edge emboss softemboss sobel sobelx sobely\npasses: 0..255   window: CENTER 0..65535, WIDTH 1..65535\n"
.equ err_usage_len, . - err_usage

.section .bss
.align 3
win_on:     .skip 4             # window/level requested?
win_center: .skip 4
win_width:  .skip 4
char_buf:   .skip 8             # 1 byte for read_uint, 2 for the magic number
header_buf: .skip 48
.balign 8
buf_a:      .skip MAX_PIXELS*2  # 16-bit samples, row-major. Raw file bytes
buf_b:      .skip MAX_PIXELS*2  # are staged here; filter_core ping-pongs
                                # between the two.

.section .text
.global _start

# Register use in _start (all survive the calls below):
#   s0 fd_in   s1 fd_out   s2 W   s3 H   s4 pixel count   s5/s6 scratch
#   s7 maxval  s8 filter record   s9 passes   s10 buffer   s11 bytes/sample
_start:
    # Initialize gp. There is no libc startup code to do it, and the linker
    # relaxes `la` of nearby symbols into gp-relative addressing, which
    # would silently read/write near address 0 if gp were left as 0.
.option push
.option norelax
    la gp, __global_pointer$
.option pop

    # ---- parse command line ----
    # Linux process entry: sp -> argc, argv[0], argv[1], ... (8 bytes each)
    REG_L s6, 0                     # argc
    la s8, f_blur                    # default filter
    li t1, 2
    blt s6, t1, .arg_names_done        # no name given: blur
    REG_L s9, 2*PTRSIZE                 # argv[1] (s9 temporarily = name)
    la s8, filter_table
.find_filter:
    REG_L t2, F_NAME, s8
    beqz t2, .usage                    # hit terminator: unknown filter
    mv a0, s9
    mv a1, t2
    jal ra, strcmp
    beqz a0, .arg_names_done
    addi s8, s8, F_SIZE
    j .find_filter
.arg_names_done:
    li s9, -1                          # passes: -1 = not given yet
    li s5, 2                            # remaining args start at argv[2]
.arg_loop:
    bge s5, s6, .args_done
    slli t0, s5, PSHIFT
    add t0, t0, sp
    REG_L a0, PTRSIZE, t0               # argv[s5]
    la a1, s_window
    jal ra, strcmp
    bnez a0, .arg_passes

    addi t0, s5, 2                      # "window" needs two more arguments
    bge t0, s6, .usage
    slli t0, s5, PSHIFT
    add t0, t0, sp
    REG_L a0, 2*PTRSIZE, t0              # argv[s5+1]: center
    li a1, 65535
    jal ra, parse_uint
    bltz a0, .usage
    la t1, win_center
    sw a0, 0(t1)
    slli t0, s5, PSHIFT
    add t0, t0, sp
    REG_L a0, 3*PTRSIZE, t0               # argv[s5+2]: width
    li a1, 65535
    jal ra, parse_uint
    blez a0, .usage                        # width must be >= 1
    la t1, win_width
    sw a0, 0(t1)
    li t0, 1
    la t1, win_on
    sw t0, 0(t1)
    addi s5, s5, 3
    j .arg_loop

.arg_passes:
    bgez s9, .usage                      # pass count given twice
    slli t0, s5, PSHIFT
    add t0, t0, sp
    REG_L a0, PTRSIZE, t0
    li a1, MAX_PASSES
    jal ra, parse_uint
    bltz a0, .usage
    mv s9, a0
    addi s5, s5, 1
    j .arg_loop
.args_done:
    bgez s9, 1f
    lw s9, F_PASSES(s8)                  # default passes for this filter
1:

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
    jal ra, read_uint            # maxval
    mv s7, a0
    beqz s7, .bad_maxval          # 0 is invalid
    li t0, 65535
    bgtu s7, t0, .bad_maxval       # >65535 isn't valid PGM

    li s11, 1                       # bytes per sample: 1 if maxval < 256
    li t0, 255
    bleu s7, t0, 1f
    li s11, 2
1:
    beqz s2, .bad_dims             # zero width or height is not an image
    beqz s3, .bad_dims
    mul s4, s2, s3                # pixel count = W*H  (W, H are each < 2^32:
                                  # read_uint saturates, so this can't wrap at
                                  # 64 bits ...)
.if XLEN == 32
    mulhu t0, s2, s3              # ... but at 32 bits it can: any set bit in
    bnez t0, .too_big             # the high half means the product overflowed
.endif
    li t0, MAX_PIXELS              # reject images that would overflow buffers
    bgtu s4, t0, .too_big

    # ---- read raw sample bytes into buf_b (staging) ----
    mul s6, s4, s11                 # total bytes = pixels * bytes/sample
    li s5, 0                         # bytes read so far; read() may return
    la s10, buf_b
.px_read:                            # fewer bytes than asked, so loop
    bgeu s5, s6, .px_read_done
    mv a0, s0
    add a1, s10, s5
    sub a2, s6, s5
    li a7, 63
    ecall
    blez a0, .truncated              # error or EOF before all pixels arrived
    add s5, s5, a0
    j .px_read
.px_read_done:

    mv a0, s0
    li a7, 57                       # close(fd_in)
    ecall

    # ---- widen staged bytes (buf_b) into 16-bit samples (buf_a) ----
    la t0, buf_b
    la t1, buf_a
    li t2, 0                         # sample index
    li t6, 2
    beq s11, t6, .widen16
.widen8:
    beq t2, s4, .widen_done
    add t3, t0, t2
    lbu t3, 0(t3)
    bgtu t3, s7, .bad_sample          # PGM requires sample <= maxval
    slli t4, t2, 1
    add t4, t4, t1
    sh t3, 0(t4)
    addi t2, t2, 1
    j .widen8
.widen16:
    beq t2, s4, .widen_done
    slli t3, t2, 1
    add t3, t3, t0
    lbu t4, 0(t3)                     # big-endian: high byte first
    lbu t5, 1(t3)
    slli t4, t4, 8
    or t4, t4, t5
    bgtu t4, s7, .bad_sample
    slli t5, t2, 1
    add t5, t5, t1
    sh t4, 0(t5)
    addi t2, t2, 1
    j .widen16
.widen_done:

    # ---- run the filter core ----
    la a0, buf_a                    # src
    la a1, buf_b                    # scratch
    mv a2, s2
    mv a3, s3
    mv a4, s8
    mv a5, s9
    mv a6, s7
    jal ra, filter_core
    mv s10, a0                      # buffer holding the result

    la t0, buf_a                     # s5 = the other buffer (free: staging)
    bne s10, t0, 1f
    la s5, buf_b
    j 2f
1:  mv s5, t0
2:
    la t0, win_on                     # window/level requested?
    lw t0, 0(t0)
    beqz t0, .no_window
    mv a0, s10                         # src samples
    mv a1, s5                           # dst bytes
    mv a2, s4
    la t0, win_center
    lw a3, 0(t0)
    la t0, win_width
    lw a4, 0(t0)
    jal ra, window_level
    mv t1, s5                           # .narrow_done copies t1 -> s10 (bytes to write)
    li s7, 255                           # output is 8-bit
    li s11, 1
    j .narrow_done
.no_window:

    # ---- narrow result into the other buffer as file bytes ----
    mv t0, s10                         # t0 = result samples
    mv t1, s5                           # t1 = output bytes
    li t2, 0
    li t6, 2
    beq s11, t6, .narrow16
.narrow8:
    beq t2, s4, .narrow_done
    slli t3, t2, 1
    add t3, t3, t0
    lhu t3, 0(t3)
    add t4, t1, t2
    sb t3, 0(t4)
    addi t2, t2, 1
    j .narrow8
.narrow16:
    beq t2, s4, .narrow_done
    slli t3, t2, 1
    add t3, t3, t0
    lhu t3, 0(t3)
    srli t4, t3, 8                     # big-endian: high byte first
    slli t5, t2, 1
    add t5, t5, t1
    sb t4, 0(t5)
    sb t3, 1(t5)
    addi t2, t2, 1
    j .narrow16
.narrow_done:
    mv s10, t1                        # s10 = bytes to write

    # ---- open output ----
    li a0, -100
    la a1, out_path
    li a2, 577               # O_WRONLY|O_CREAT|O_TRUNC (1|64|512)
    li a3, 420                # 0644
    li a7, 56
    ecall
    bltz a0, .open_out_fail
    mv s1, a0                  # fd_out

    # ---- build "P5\n<W> <H>\n<maxval>\n" header text ----
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

    mv a0, s7                           # maxval
    add a1, s5, s6
    jal ra, itoa
    add s6, s6, a0

    add t3, s5, s6
    li t1, 0x0A
    sb t1, 0(t3)
    addi s6, s6, 1

    mv a0, s1
    mv a1, s5
    mv a2, s6
    jal ra, write_all                    # write header

    # ---- write pixels, close, exit ----
    mul a2, s4, s11                         # pixels * bytes/sample
    mv a0, s1
    mv a1, s10
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
.bad_sample:
    la a1, err_sample
    li a2, err_sample_len
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
# in: a0=fd  out: a0=parsed value (saturating at 2^32-1)  clobbers: t0-t4
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
    li t2, 429496729              # (2^32-1)/10: above this, x10 would not fit
    bgtu t0, t2, .ru_sat           # in 32 bits
    bne t0, t2, .ru_fits
    li t2, 5                       # at exactly that value only digits 0..5 fit
    bgtu t1, t2, .ru_sat
.ru_fits:
    li t2, 10
    mul t0, t0, t2
    add t0, t0, t1
    j .ru_loop
.ru_sat:                           # saturate at 2^32-1 instead of wrapping, so
    li t0, 0xFFFFFFFF              # an absurd size is reported as "too large"
    j .ru_loop                     # (same result at 32 and 64 bits)
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

# parse_uint: parses a NUL-terminated decimal string, value 0..limit.
# in: a0=string, a1=limit  out: a0=value, or -1 if empty, non-numeric or
# above the limit   clobbers: t0-t2
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
    bgtu t1, a1, .pu_bad                 # checked every digit: no overflow
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
