# riscv-image-blur

A 3x3 box blur on a real image file, written in RISC-V assembly (RV64) with
real Linux file I/O — no libc, just `openat`/`read`/`write`/`close`/`exit`
syscalls via `ecall`. Companion project to `../riscv-asm`, same toolchain
(`riscv64-linux-gnu-as`/`ld`, `qemu-riscv64`, `riscv64-linux-gnu-gdb`).

Runs the blur `PASS_COUNT` times (8 by default) at up to 256x256, ping-
ponging between two buffers each pass instead of copying memory. A single
3x3 pass is a 1-pixel-radius effect — imperceptible on a 256x256 image — so
repeating it is what actually makes the blur visible at this size (see
"Why repeated passes" below).

## Layout

- `src/blur.s` — the assembly program
- `scripts/make_test_image.py` — generates a 256x256 checkerboard test PGM
  (16px cells) — not part of the RISC-V program, just test data
- `images/` — `input.pgm` / `output.pgm` (gitignored)
- `build/` — build output (gitignored)

## Usage

```sh
make run      # generates images/input.pgm, builds blur.s, runs it under
              # qemu-riscv64, writes images/output.pgm
make debug    # same, but attaches gdb (breaks at _start)
make clean
```

To try a different test pattern, edit `scripts/make_test_image.py` (or swap
in your own generator) and re-run `make run` — the assembly program itself
doesn't care how `images/input.pgm` was produced, only that it matches the
header format below.

## Viewing the result

Open `images/input.pgm` and `images/output.pgm` in your image viewer of
choice to compare them — at 256x256 they display at a normal size, no
zooming needed.

## Why repeated passes

The blur's radius is fixed at 1 pixel each direction (3x3), no matter the
image size. At 16x16 that's ~12% of the width, obviously visible; at
256x256 it's ~0.4%, and a single pass looks like almost nothing happened.
Running the same pass `PASS_COUNT` times compounds the effect — this is
the standard "fast Gaussian approximation" trick (a uniform kernel
convolved with itself repeatedly approaches a Gaussian shape), not a hack
specific to this project. Measured here on the checkerboard test pattern,
contrast across a cell boundary collapses from 215 (crisp edge) to 36
after 8 passes.

The test image is a checkerboard rather than a single shape for the same
reason: blur is only visible *at edges*, and a checkerboard has an edge
every 16 pixels, so the effect shows up everywhere instead of only around
one shape's boundary.

## Feeding in a real image

Convert and downscale with ImageMagick first — `blur.s` has no image
codec, only a raw-PGM reader:

```sh
convert downloaded.jpg -resize 256x256 -colorspace Gray -depth 8 images/input.pgm
```

## Image format

`read_uint` skips `#`-to-end-of-line comments and any run of
spaces/tabs/newlines before each header field, so PGMs from other tools
(like ImageMagick's, which may add a comment line) parse correctly, not
just the exact single-space/single-newline layout
`scripts/make_test_image.py` produces:

```
P5\n<width> <height>\n255\n<width*height raw grayscale bytes>
```

`width*height` must stay within `MAX_PIXELS` (65536, i.e. up to 256x256) —
`convert -resize` is how you stay under that for a real photo.

## How it works

1. **Parse header** — `read_uint` reads decimal ASCII digits one byte at a
   time via `read()`, stopping at (and discarding) the first non-digit —
   used for width, height, and the discarded maxval field.
2. **Read + unpack** — one `read()` call pulls all `width*height` raw pixel
   bytes into a buffer, then each byte is widened into a 4-byte-aligned
   word array (`pixel_in`) so the convolution itself works purely with
   `lw`/`sw`, not byte-granularity loads.
3. **Convolve, `PASS_COUNT` times** — for every pixel, `sample` reads each
   of the 3x3 neighborhood positions from the current source buffer;
   `clamp` maps out-of-range row/col indices to the nearest valid edge
   instead of reading garbage or wrapping around. The 9-value sum is
   divided by 9 with `divu` (RV64GC's M-extension divider, not a shift
   trick — 9 isn't a power of 2). After each pass, the source/destination
   buffer registers (`s10`/`s11`) are swapped — no memory copy — so the
   next pass blurs the previous pass's result. `s10` always ends up
   holding the final buffer once the loop exits, regardless of whether
   `PASS_COUNT` is odd or even.
4. **Pack + write** — the averaged word array is narrowed back to bytes,
   and a fresh `P5` header is built with `itoa` (mirrors `read_uint`) before
   writing `images/output.pgm`.

This is also the first program in either project with 2-level call nesting
(`_start` -> `sample` -> `clamp`), so `sample` uses a real stack frame
(`addi sp,sp,-16` / `sd ra,...` / `ld ra,...`) to save `ra` instead of the
scratch-register trick used in `riscv-asm/src/mux16x2.s`, since a second
nested call would otherwise clobber it.

## Verification

`blur.s`'s output was checked against an independent Python reference
implementation of the same algorithm (3x3 box blur, integer divide,
clamp-to-edge, repeated `PASS_COUNT` times) on the 256x256 checkerboard
test image — all 65536 pixels matched exactly after 8 passes. `read_uint`'s
comment/whitespace handling was separately checked against a synthetic PGM
with comment lines and irregular spacing in its header.
