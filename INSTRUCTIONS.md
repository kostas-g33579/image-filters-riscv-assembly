# Instructions

```sh
cd ~/projects/riscv-image-blur

make run                        # build + run blur (8 passes) on images/input.pgm
make run FILTER=sharpen         # choose a filter
make run FILTER=blur PASSES=3   # override pass count (0..255)
make run FILTER=identity WINDOW="1064 400"   # window/level -> 8-bit output
make phantom                    # make a 512x512 16-bit CT-like test image
make demo                       # run every filter -> images/output_<filter>.pgm
make test                       # regression tests (SKIP_BIG=1 skips the 16M-pixel cases)
make rv32 && make test32        # RV32 build (the Pico 2's architecture) under qemu-riscv32
make testall                    # both widths
make debug                      # step through in gdb (breaks at _start)
make clean

xdg-open images/input.pgm
xdg-open images/output.pgm
```

Filters: identity, blur, gaussian, sharpen, edge, emboss, sobel, sobelx, sobely
(see docs/filters.md for what each one does, docs/window_level.md for windowing).

`images/input.pgm` is generated (256x256 checkerboard) only if missing.
To use your own image (8-bit or 16-bit PGM, up to 16,777,216 pixels):

```sh
convert downloaded.jpg -resize 256x256 -colorspace Gray -depth 8 images/input.pgm
make run FILTER=edge
```

Manual steps (no Makefile; two source files, `-I src` finds `records.inc`):

```sh
python3 scripts/make_test_image.py images/input.pgm
riscv64-linux-gnu-as -I src -o build/filter.o src/filter.s
riscv64-linux-gnu-as -I src -o build/kernel.o src/kernel.s
riscv64-linux-gnu-ld -o build/filter build/filter.o build/kernel.o
qemu-riscv64 build/filter sharpen
qemu-riscv64 build/filter identity window 1064 400
```
