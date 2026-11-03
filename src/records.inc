# Filter record layout (64 bytes each), shared by kernel.s (which defines
# filter_table) and filter.s (which looks filters up by name).
.equ F_NAME,    0           # .dword pointer to NUL-terminated name
.equ F_KERNEL,  8           # 9 x .word signed weights, row-major
.equ F_DIV,     44          # .word divisor (non-zero)
.equ F_BIAS,    48          # .word added after division, in 1/256ths of
                            # (maxval+1): 128 means mid-gray at any bit depth
.equ F_ABS,     52          # .word nonzero: take |sum| before dividing
.equ F_PASSES,  56          # .word default number of passes
.equ F_KERNEL2, 60          # .word signed byte offset from THIS record to a
                            # second filter record, or 0. If set, the value
                            # is |sum with this kernel| + |sum with that
                            # record's kernel| (gradient magnitude, `sobel`).
                            # An offset (not a pointer) keeps the record
                            # layout identical on RV32 and RV64.
.equ F_SIZE,    64
