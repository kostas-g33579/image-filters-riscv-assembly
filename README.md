# riscv-image-blur

Kernel-convolution image filters (blur, sharpen, edge detect, emboss, Sobel)
on a real PGM image file, written in RISC-V assembly (RV64) with real Linux
file I/O — no libc, just `openat`/`read`/`write`/`close`/`exit` syscalls via
`ecall`. Companion project to `../riscv-asm`, same toolchain
(`riscv64-linux-gnu-as`/`ld`, `qemu-riscv64`, `riscv64-linux-gnu-gdb`).

One generic 3x3 convolution routine does all the work. A filter is just a
row in a table: nine kernel weights, a divisor, a bias, an optional
absolute-value step and a default pass count.

## Layout

- `src/filter.s` — the assembly program (filter table + convolution)
- `scripts/make_test_image.py` — generates a 256x256 checkerboard test PGM
  (16px cells) — not part of the RISC-V program, just test data
- `tests/run_tests.py` — regression tests against a Python reference
- `images/` — `input.pgm` / `output.pgm` (gitignored)
- `build/` — build output (gitignored)

## Usage

```sh
make run                       # blur (default), 8 passes
make run FILTER=sharpen        # pick a filter
make run FILTER=blur PASSES=3  # override the pass count (0..255)
make demo                      # every filter -> images/output_<filter>.pgm
make test                      # regression tests
make debug                     # attach gdb (breaks at _start)
make clean
```

`make run` generates a checkerboard `images/input.pgm` only if that file
doesn't exist, so your own image is never overwritten (`make image` forces
regeneration). Or run the binary directly:

```sh
qemu-riscv64 build/filter [name [passes]]
```

With no arguments it runs `blur`. An unknown filter name or an invalid pass
count prints a usage message and exits 1.

## Filters

Per pixel: `v = sum(kernel[i] * tap[i])`, then `|v|` if the abs flag is set,
then `v / divisor` (truncating toward zero), then `+ bias`, then clamp to
0..255. Out-of-range taps use the nearest edge pixel.

| name       | kernel (row-major)           | div | bias | abs | passes |
|------------|------------------------------|-----|------|-----|--------|
| `identity` | 0 0 0 / 0 1 0 / 0 0 0        | 1   | 0    | no  | 1      |
| `blur`     | 1 1 1 / 1 1 1 / 1 1 1        | 9   | 0    | no  | 8      |
| `gaussian` | 1 2 1 / 2 4 2 / 1 2 1        | 16  | 0    | no  | 4      |
| `sharpen`  | 0 -1 0 / -1 5 -1 / 0 -1 0    | 1   | 0    | no  | 1      |
| `edge`     | -1 -1 -1 / -1 8 -1 / -1 -1 -1| 1   | 0    | no  | 1      |
| `emboss`   | -2 -1 0 / -1 1 1 / 0 1 2     | 1   | 128  | no  | 1      |
| `sobelx`   | -1 0 1 / -2 0 2 / -1 0 1     | 4   | 0    | yes | 1      |
| `sobely`   | -1 -2 -1 / 0 0 0 / 1 2 1     | 4   | 0    | yes | 1      |

Adding a filter: add a name string and a record to `filter_table` in
`src/filter.s` (and to `FILTERS` in `tests/run_tests.py`, and the usage
string). No other code changes.

## Viewing the result

Open `images/input.pgm` and the output in your image viewer to compare. For
a real photo, convert and downscale with ImageMagick first — the program
has no image codec, only a raw-PGM reader:

```sh
convert downloaded.jpg -resize 256x256 -colorspace Gray -depth 8 images/input.pgm
```

## Why repeated passes (blur / gaussian)

A 3x3 kernel has a fixed 1-pixel radius. At 256x256 a single pass looks like
almost nothing happened, so blur defaults to 8 passes and gaussian to 4.
Repeating a uniform kernel approaches a Gaussian shape — the standard "fast
Gaussian approximation" trick. Each pass feeds the previous pass's result
back in (the buffers ping-pong, no copying). Sharpen, edge, emboss and Sobel
default to 1 pass.

## Image format

```
P5 <width> <height> <maxval=1..255> <width*height raw grayscale bytes>
```

The header is parsed leniently: any run of spaces/tabs/CR/LF between
fields, and `#`-to-end-of-line comments (so `P5\r\n` and ImageMagick's
comment lines work). Exactly one whitespace byte follows `maxval`, as the
PGM spec requires. `width*height` must be non-zero and at most
`MAX_PIXELS` (65536, i.e. up to 256x256). 16-bit PGMs (`maxval` > 255) are
rejected.

## Error handling

All failures print a message to stderr, exit 1 and leave no output file:
unknown filter / bad pass count (usage), cannot open input or output, bad or
truncated header, bad `maxval`, zero dimension, too many pixels, truncated
pixel data, and failed writes. Reads and writes loop over short transfers.

## How it works

1. **Arguments** — read from the stack at entry (`argc`, `argv`): the filter
   name is looked up in `filter_table` with `strcmp`, the optional pass count
   is parsed by `parse_uint`.
2. **Parse header** — `read_uint` reads decimal ASCII one byte at a time.
3. **Read + unpack** — pixel bytes are widened into a 4-byte word array
   (`pixel_in`) so the convolution works purely with `lw`/`sw`.
4. **Convolve** — for each pixel, loop over the 9 taps: `sample` fetches the
   pixel (clamping row/col to the edge via `clamp`), multiplied by the
   kernel weight and accumulated; then abs / divide / bias / clamp. After
   each pass the source/destination registers (`s10`/`s11`) swap, so `s10`
   holds the final buffer whatever the pass count.
5. **Pack + write** — words are narrowed to bytes, a fresh header is built
   with `itoa`, and `write_all` writes the file.

## Tests

`make test` runs the real binary under qemu in a temp directory (never
touching `images/`) and compares against an independent Python reference:
every filter on two image shapes, pass-count overrides, edge-size images
(1x1, single row/column), header layout variants, and every error path.
