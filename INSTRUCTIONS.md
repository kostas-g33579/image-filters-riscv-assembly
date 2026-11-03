# Instructions

```sh
cd ~/projects/riscv-image-blur

# generate 256x256 checkerboard test image, build, run (8 blur passes) ->
# writes images/input.pgm and images/output.pgm
make run

# open the two files in your image viewer to compare
xdg-open images/input.pgm
xdg-open images/output.pgm

# step through in gdb (breaks at _start)
make debug

# remove build output
make clean
```

Manual steps (no Makefile):

```sh
python3 scripts/make_test_image.py images/input.pgm
riscv64-linux-gnu-as -o build/blur.o src/blur.s
riscv64-linux-gnu-ld -o build/blur build/blur.o
qemu-riscv64 build/blur
```

Use a real image instead of the checkerboard (must fit MAX_PIXELS=65536,
i.e. width*height <= 65536):

```sh
convert downloaded.jpg -resize 256x256 -colorspace Gray -depth 8 images/input.pgm
qemu-riscv64 build/blur
```

To change the blur strength, edit `PASS_COUNT` in `src/blur.s` and rebuild
(`make all`).
