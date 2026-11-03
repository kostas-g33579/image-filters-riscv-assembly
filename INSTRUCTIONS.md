# Instructions

```sh
cd ~/projects/riscv-image-blur

make run                        # build + run blur (8 passes) on images/input.pgm
make run FILTER=sharpen         # choose a filter
make run FILTER=blur PASSES=3   # override pass count (0..255)
make demo                       # run every filter -> images/output_<filter>.pgm
make test                       # regression tests
make debug                      # step through in gdb (breaks at _start)
make clean

xdg-open images/input.pgm
xdg-open images/output.pgm
```

Filters: identity, blur, gaussian, sharpen, edge, emboss, sobel, sobelx, sobely
(see docs/filters.md for what each one does).

`images/input.pgm` is generated (256x256 checkerboard) only if missing.
To use your own image (must fit width*height <= 65536):

```sh
convert downloaded.jpg -resize 256x256 -colorspace Gray -depth 8 images/input.pgm
make run FILTER=edge
```

Manual steps (no Makefile):

```sh
python3 scripts/make_test_image.py images/input.pgm
riscv64-linux-gnu-as -o build/filter.o src/filter.s
riscv64-linux-gnu-ld -o build/filter build/filter.o
qemu-riscv64 build/filter sharpen
```
