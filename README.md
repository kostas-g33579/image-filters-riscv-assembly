# riscv-image-blur

Kernel-convolution image filters (blur, sharpen, edge detect, emboss, Sobel)
and DICOM-style window/level on 8- and 16-bit PGM images, written in RISC-V
assembly (RV64) with real Linux file I/O — no libc, just
`openat`/`read`/`write`/`close`/`exit` syscalls via `ecall`. Companion project to `../riscv-asm`, same toolchain
(`riscv64-linux-gnu-as`/`ld`, `qemu-riscv64`, `riscv64-linux-gnu-gdb`).

The program is split in two. `src/kernel.s` is the portable **filter core**:
pure computation on buffers, no syscalls, so it can later be driven by
something other than Linux. The source also builds as **RV32** (`make rv32`),
the architecture of the Raspberry Pi Pico 2's Hazard3 cores, and passes the
same tests under `qemu-riscv32` (see `docs/rv32_port.md`). `src/filter.s` is the Linux **harness** around it: command line, PGM file
I/O and sample conversion.

One generic 3x3 convolution routine does all the work. A filter is just a
row in a table: nine kernel weights, a divisor, a bias, an optional
absolute-value step and a default pass count.

## Layout

- `src/kernel.s` — the filter core: `filter_core` (3x3 convolution on 16-bit
  samples), `window_level`, and the filter table
- `src/filter.s` — the Linux harness: arguments, file I/O, header parsing,
  8/16-bit sample conversion
- `src/records.inc` — filter-record layout shared by both files
- `src/xlen.inc` — RV32/RV64 abstraction (the source builds at both widths)
- `docs/filters.md`, `docs/window_level.md`, `docs/rv32_port.md` —
  references (what each filter does, the windowing maths, the RV32 port and
  Hazard3 timing notes)
- `scripts/make_test_image.py` — generates a 256x256 checkerboard test PGM
  (16px cells); `scripts/make_phantom16.py` — a 512x512 16-bit CT-like
  phantom — not part of the RISC-V program, just test data
- `tests/run_tests.py` — regression tests against a Python reference
- `images/` — `input.pgm` / `output.pgm` (gitignored)
- `build/` — build output (gitignored)

## Usage

```sh
make run                       # blur (default), 8 passes
make run FILTER=sharpen        # pick a filter
make run FILTER=blur PASSES=3  # override the pass count (0..255)
make run FILTER=identity WINDOW="1064 400"   # window/level, 8-bit output
make phantom                   # synthetic 16-bit CT-like test image
make demo                      # every filter -> images/output_<filter>.pgm
make test                      # regression tests (RV64)
make rv32 && make test32       # build and test the RV32 version (qemu-riscv32)
make rv32zbb && make test32zbb # RV32 + Zbb min/max, as on the RP2350
make testall                   # all three builds
make debug                     # attach gdb (breaks at _start)
make clean
```

`make run` generates a checkerboard `images/input.pgm` only if that file
doesn't exist, so your own image is never overwritten (`make image` forces
regeneration). Or run the binary directly:

```sh
qemu-riscv64 build/filter [name] [passes] [window CENTER WIDTH]
```

With no arguments it runs `blur`. `passes` and `window C W` may come in
either order after the name. Anything invalid prints a usage message and
exits 1. See `docs/window_level.md` for what `window` does.

## Filters

Per pixel: `v = sum(kernel[i] * tap[i])`, then `|v|` if the abs flag is set
(`sobel` has a second kernel and uses `|v1| + |v2|`), then `v / divisor` (rounded to nearest, halves away from zero — plain
truncation would darken the image a little more with every pass), then
`+ bias`, then clamp to 0..maxval (the image's own maximum, 255 for 8-bit
files, 65535 for full 16-bit). Out-of-range taps use the nearest edge pixel.

The bias column is in 1/256ths of `maxval+1`, so 128 means mid-gray at any
bit depth: 128 at maxval 255, 2048 at 4095, 32768 at 65535.

| name       | kernel (row-major)           | div | bias | abs | passes |
|------------|------------------------------|-----|------|-----|--------|
| `identity` | 0 0 0 / 0 1 0 / 0 0 0        | 1   | 0    | no  | 1      |
| `blur`     | 1 1 1 / 1 1 1 / 1 1 1        | 9   | 0    | no  | 8      |
| `gaussian` | 1 2 1 / 2 4 2 / 1 2 1        | 16  | 0    | no  | 4      |
| `sharpen`  | 0 -1 0 / -1 5 -1 / 0 -1 0    | 1   | 0    | no  | 1      |
| `edge`     | -1 -1 -1 / -1 8 -1 / -1 -1 -1| 1   | 0    | yes | 1      |
| `emboss`   | -2 -1 0 / -1 0 1 / 0 1 2     | 1   | 128  | no  | 1      |
| `sobel`    | `sobelx` and `sobely` kernels| 4   | 0    | yes | 1      |
| `sobelx`   | -1 0 1 / -2 0 2 / -1 0 1     | 4   | 0    | yes | 1      |
| `sobely`   | -1 -2 -1 / 0 0 0 / 1 2 1     | 4   | 0    | yes | 1      |

See `docs/filters.md` for a plain-language explanation of each filter.

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
P5 <width> <height> <maxval=1..65535> <width*height samples>
```

Samples are 1 byte each if `maxval` < 256, otherwise 2 bytes **big-endian**
(as the PGM format specifies). Internally everything is 16-bit, so one code
path handles all depths; the harness widens on read and narrows on write.
Output keeps the input's `maxval` and sample size — except in a `window`
run, which always writes 8-bit.

The header is parsed leniently: any run of spaces/tabs/CR/LF between
fields, and `#`-to-end-of-line comments (so `P5\r\n` and ImageMagick's
comment lines work). Exactly one whitespace byte follows `maxval`, as the
PGM spec requires. `width*height` must be non-zero and at most
`MAX_PIXELS` (16,777,216, i.e. up to 4096x4096). A sample above `maxval` is
rejected (that's an invalid PGM and would otherwise produce out-of-range
output).

## Error handling

All failures print a message to stderr, exit 1 and leave no output file:
unknown filter / bad pass count (usage), cannot open input or output, bad or
truncated header, bad `maxval`, zero dimension, too many pixels, truncated
pixel data, a sample above `maxval`, and failed writes. Reads and writes loop over short transfers.

## How it works

1. **Arguments** — read from the stack at entry (`argc`, `argv`): the filter
   name is looked up in `filter_table` with `strcmp`; `parse_uint` reads the
   pass count and the window center/width, each with its own limit.
2. **Parse header** — `read_uint` reads decimal ASCII one byte at a time.
3. **Read and widen** — the raw file bytes are read into one buffer, then
   widened (1 byte) or byte-swapped (2 bytes, big-endian) into 16-bit
   samples in the other.
4. **`filter_core`** — the portable part (`src/kernel.s`). Called as
   `filter_core(src, scratch, W, H, record, passes, maxval)`; it returns a
   pointer to whichever buffer holds the result. Per row, the three source
   rows (above / current / below) are clamped to the image once and turned
   into pointers; per pixel, the left/right columns are clamped, and the
   nine taps are plain `lhu` loads at fixed offsets, each multiplied by its
   kernel weight (`TAP` macro) — no function calls in the loop. Then abs /
   divide-with-rounding / scaled bias / clamp, and one `sh`. After each pass
   the source/destination registers swap, so no memory is copied.
5. **Window/level** (optional) — `window_level` maps the result to 8-bit
   display values.
6. **Narrow and write** — samples are converted back to file bytes, a header
   is built with `itoa`, and `write_all` writes it out.

`_start` also initializes `gp` (with linker relaxation off for that one
instruction). There is no libc startup code to do it, and without it the
linker's gp-relative `la` relaxation of nearby symbols would address
memory near zero.

## Performance and memory

Measured under `qemu-riscv64`:

| case                                  | time   |
|---------------------------------------|--------|
| 256x256, 8-bit, `blur` (8 passes)     | 0.02 s |
| 512x512, 16-bit, `blur` (8 passes)    | 0.06 s |
| 4096x4096, 16-bit, `gaussian` (1 pass)| 0.57 s |

Memory is two 16-bit buffers of `MAX_PIXELS` samples (64 MB of address space
for the 16.7-megapixel limit; the OS only commits the pages an image actually
touches). Edge clamping is hoisted out of the tap loop, so there are no
function calls per tap.

The two-buffer, whole-image design is the right shape for a host but not for
a microcontroller with ~500 KB of RAM; see the roadmap in the project notes
(line-buffer streaming is the planned answer).

## Tests

`make test` runs the real binary under qemu in a temp directory (never
touching `images/`) and compares against an independent Python reference.
Small and medium images are checked in full: every filter at 8, 12 and 16
bits, pass-count overrides, edge-size images (1x1, single row/column,
65536x1, 1x65536), header layout variants, bit-depth boundaries (maxval 1,
255, 256), and every error path. Images up to the 16-million-pixel limit
(4096x4096, 16777216x1, 1x16777216) are checked at sampled points — all four
corners, points along every edge, and random interior pixels — against the
same math. Window/level is checked against the DICOM function evaluated in
exact rational arithmetic. `SKIP_BIG=1 make test` skips the largest cases. `make testall` runs the whole
suite against both the RV64 and RV32 builds.
