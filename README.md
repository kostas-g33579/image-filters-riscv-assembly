# riscv-image-blur

Medical-image processing written for RISC-V, from the assembly up. It started as 3x3 convolution filters in
assembly and grew into a small research tool that runs on a Raspberry Pi Pico 2:

1. **The filter core** (`src/kernel.s`): blur, sharpen, edge, emboss, Sobel and DICOM-style window/level on 8- and 16-bit
   images, in RISC-V assembly with no libc. It builds as RV64 (Linux, under `qemu-riscv64`) and as RV32 (the Pico 2's
   Hazard3 cores).
2. **The Pico 2 firmware** (`pico/`): the same assembly kernel behind a USB serial protocol, including a streaming mode that
   filters images of any height in 420 KB of RAM.
3. **An on-device DICOM to NIfTI anonymiser** (`pico/src/`, `pico/tools/`): the PC sends raw DICOM files to the board; the
   board returns only the voxel values, a NIfTI-1 header and a JSON file built from constants and validated numbers.
   Nothing a DICOM file says about a person or a site can reach the output, because the device is the trust boundary. Works
   on whole studies in a batch and was tested on real public MRI series (TCIA UPENN-GBM).

Companion project to `../riscv-asm`, same toolchain (`riscv64-linux-gnu-as`/`ld`, `qemu-riscv64`,
`riscv64-linux-gnu-gdb`). The C parts are built with `riscv64-elf-gcc` (RV32), the firmware with the Pico SDK.

## Quick start

```sh
# 1. The filters on the host (RV64 under qemu)
make run FILTER=sharpen                       # blur is the default; see "Usage" below
make test && make rv32 && make test32         # the filter regression tests, RV64 and RV32

# 2. The firmware (needs the Pico SDK, cmake, ninja, picotool)
make pico                                     # -> build-pico/image_filters.uf2
picotool load -f build-pico/image_filters.uf2 # or hold BOOTSEL and copy the file; see pico/README.md
sudo python3 pico/tools/pico_filter.py images/input.pgm out.pgm gaussian 4 --verify

# 3. The anonymiser (one series, then many)
sudo python3 pico/tools/pico_anonymize.py data/dicom/SERIES_DIR --subject P001 --sequence T1c
sudo python3 pico/tools/pico_anonymize.py --batch data/dicom --out output/batch --report output/batch.csv
# -> output/anonymized/P001_T1c/P001_T1c.nii and P001_T1c.json (exactly those two files)

# 4. The whole test suite (needs no hardware)
make testall test-proto test-stream test-dicom test-nifti test-dicom-c test-geom-c test-nifti-c test-sidecar-c \
     test-anon-c test-anonymize test-batch test-fetch test-bench
```

`INSTRUCTIONS.md` has the short command list; the documents under `docs/` and `pico/README.md` have the detail.

## The anonymiser

The goal is a researcher's tool for a clinical setting: take the DICOM files of a series, return a research-ready NIfTI
volume that provably carries no patient data, on hardware small enough to audit.

- **What goes in:** an uncompressed, single-frame, greyscale (8 or 16 bit) series, explicit or implicit VR little endian; in
  practice MR so far (the real tests are MRI). The files
  are *untrusted*: the reader is an allow-list parser that reads about 40 technical tags, only *notes the presence* of 24
  identifying tags (names and counts appear in the audit, values are never read), checks every length, and refuses what
  it does not understand.
- **What comes out:** the voxel values, re-packed to a fixed type; a 352-byte NIfTI-1 header made only of constants and
  validated numbers (sizes, voxel sizes, the voxel-to-world matrix, slope and intercept); and a JSON sidecar with the
  acquisition parameters in BIDS units, measured geometry, discrepancies between tags and measurement, and an audit. The
  only text taken from you is the labels you give (`--subject`, `--sequence`, `--site`, `--scanner`).
- **What it does not do:** it does not remove text burned into the pixels or faces (the audit says so), does not read
  compressed DICOM (JPEG 2000 and others are refused), multi-frame or colour files, does not yet restrict the modality
  (a CT or an ultrasound image of the right shape would be converted), and it is a research aid, not a
  certified de-identification product. See `docs/dicom.md` for the policy, the guarantees and the limits.
- **How fast:** a 192-slice series (19.45 MB) takes 35.6 s on a Pico 2 (about 546 KB/s each way over USB); 24 real series
  (413 MB of output, up to 512 x 512 x 248 and 617 KB per file) took 12.9 minutes, all byte-identical to the emulator's output.
- **Batches and data:** `--batch` does many series on one connection and resumes where it stopped.
  `scripts/fetch_tcia.py` downloads public TCIA series into `data/fetched/` after checking each archive.

## Layout

- `src/kernel.s` — the filter core: `filter_core` (3x3 convolution on 16-bit samples), `window_level`, and the filter table
- `src/filter.s` — the Linux harness: arguments, file I/O, header parsing, 8/16-bit sample conversion
- `src/records.inc`, `src/xlen.inc` — the filter-record layout and the RV32/RV64 abstraction shared by both files
- `pico/` — the Pico 2 firmware and everything around it; see `pico/README.md`
  - `src/main.c`, `proto.c`, `stream.c` — USB serial, the filter wire protocol (`IMG1`, `STR1`) and row streaming
  - `src/dicom.c`, `geom.c`, `nifti.c`, `sidecar.c` — the anonymiser's allow-list DICOM reader, stack geometry, NIfTI writer
    and JSON writer (no libc, no floating point formatting)
  - `src/anon.c` — the anonymiser protocol (`DCM1`): streams a series in, streams the volume out
  - `test/` — each module as a Linux program for `qemu-riscv32`, so the same code that runs on the chip is tested
  - `tools/` — `pico_filter.py`, `pico_anonymize.py` (single series and `--batch`), the USB benchmark, and the probe checker
  - `probe/`, `bench/` — on-chip test firmwares (the anonymiser modules on a built-in series; USB throughput)
- `scripts/` — test-data makers, the NIfTI and DICOM slice tools (also the Python reference implementations the C code is
  checked against), `fetch_tcia.py`, `estimate_cycles.py`
- `tests/` — the regression tests (about 5,000 lines of Python); see "Tests"
- `docs/` — `filters.md`, `window_level.md`, `rv32_port.md` (the port and Hazard3 timing), `streaming.md` (and the USB
  measurements), `dicom.md` (the DICOM tools and the anonymiser), `separable_gaussian.md` (larger smoothing kernels: the
  assembly, its cost on the chip, the streaming integration, and why not an FFT), `debugging.md` (OpenOCD and gdb on the board)
- `data/` (real DICOM series, not committed), `output/`, `images/`, `build*/` — local and git-ignored

## Usage (the filters on the host)

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

`make run` generates a checkerboard `images/input.pgm` only if that file doesn't exist, so your own image is never
overwritten (`make image` forces regeneration). Or run the binary directly:

```sh
qemu-riscv64 build/filter [name] [passes] [window CENTER WIDTH]
```

With no arguments it runs `blur`. `passes` and `window C W` may come in either order after the name. Anything invalid
prints a usage message and exits 1. See `docs/window_level.md` for what `window` does.

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
| `softemboss` | -2 -1 0 / -1 0 1 / 0 1 2   | 2   | 128  | no  | 1      |
| `sobel`    | `sobelx` and `sobely` kernels| 4   | 0    | yes | 1      |
| `sobelx`   | -1 0 1 / -2 0 2 / -1 0 1     | 4   | 0    | yes | 1      |
| `sobely`   | -1 -2 -1 / 0 0 0 / 1 2 1     | 4   | 0    | yes | 1      |

See `docs/filters.md` for a plain-language explanation of each filter. One generic 3x3 convolution routine does all the
work: a filter is just a row in a table (nine weights, a divisor, a bias, an optional absolute-value step and a default pass
count). `sobel`, `sobelx` and `sobely` also have a hand-scheduled fast path (measured on the chip: `sobel` 148 to 20.7
cycles per pixel, about 7x; `sobelx` and `sobely` about 3x).

Larger smoothing: on the Pico, the streaming mode also takes a separable Gaussian (or any symmetric kernel) with a chosen sigma
and radius up to 15, `pico_filter.py in.pgm out.pgm gauss --sigma 2.0`, about 2x cheaper than the repeated 3x3 `blur` at the same
smoothing and 5x at sigma 5 (`docs/separable_gaussian.md`; measured 186 cycles per pixel at radius 7; a 240 x 240 slice through
USB with a radius-6 Gaussian takes 0.22 s, the USB time, the filtering hiding behind it; radius 1 to 4 have a straight-line fast path, 26.5 to 60.3 cycles per pixel, about 2.1x faster).

Adding a filter: add a name string and a record to `filter_table` in `src/kernel.s` (and to `FILTERS` in
`tests/run_tests.py`, and the usage string in `src/filter.s`). No other code changes.

## Viewing the result

Open `images/input.pgm` and the output in your image viewer to compare. For
a real photo, convert and downscale with ImageMagick first — the program
has no image codec, only a raw-PGM reader:

```sh
convert downloaded.jpg -resize 256x256 -colorspace Gray -depth 8 images/input.pgm
```

NIfTI output opens in any NIfTI viewer (for example ITK-SNAP).

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

## How it works (the Linux harness and the filter core)

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

Memory on the host is two 16-bit buffers of `MAX_PIXELS` samples (64 MB of address space for the 16.7-megapixel limit; the
OS only commits the pages an image actually touches). Edge clamping is hoisted out of the tap loop, so there are no function
calls per tap.

The two-buffer, whole-image design is the right shape for a host but not for a microcontroller with about 500 KB of RAM.
The firmware therefore has a second mode that streams the image row by row and keeps only a few rows of each filter pass
(`docs/streaming.md`): any height, with the width limited by `passes * 3 * width * 2` bytes of the 420,000-byte working
memory. On the chip the generic path needs about 47 cycles per pixel (read from the core's own `mcycle` counter;
`docs/rv32_port.md`, `docs/streaming.md`); over USB the transfer, not the filter, dominates.

## Tests

Everything below runs without hardware. The code that runs on the chip is compiled for the host as RV32 and run under
`qemu-riscv32`, so the same C and assembly is what gets tested.

| make target | what it covers |
|---|---|
| `test`, `test32`, `test32zbb`, `testall` | the filters against an independent Python reference: every filter at 8, 12 and 16 bits, pass overrides, edge-size images, header variants, every error path, large images at sampled points, window/level in exact rational arithmetic; RV64, RV32 and RV32+Zbb |
| `test-proto`, `test-stream` | the device wire protocol and row streaming against the Linux harness, byte for byte: every filter and depth, the memory limit to the byte, errors in the first, middle and last row, a host that stops mid-message |
| `test-bench` | the USB benchmark logic |
| `test-nifti`, `test-dicom` | the Python NIfTI and DICOM slice tools (the DICOM reader is strict, an allow-list, and checked with planted identifying values) |
| `test-dicom-c`, `test-geom-c`, `test-nifti-c`, `test-sidecar-c` | each C module of the anonymiser against its Python twin or an independent oracle: tens of thousands of synthetic, corrupted and cut-off files plus 12 real series, byte-exact where possible |
| `test-anon-c` | the anonymiser protocol in two emulator builds (real sizes, and tiny buffers with 1 to 13 byte reads): every error and its recovery, requests cut off anywhere, headers at every growth step, a privacy scan of every reply |
| `test-anonymize`, `test-batch` | the host client and batch mode against that protocol, and against 18 fake devices that misbehave |
| `test-fetch` | the download script's checks on untrusted archives (no network) |
| `test-gauss` | the separable Gaussian prototype (`src/gauss.s`) against an exact integer model, an exact 2-D convolution and a double-precision Gaussian |
| `test-gauss-stream` | the separable kernel in the streaming protocol: bit-exact against an independent model, the memory limit and layout, every bad kernel |

Beyond passing, the tests are themselves tested: for each C module and for the client and download script, realistic bugs
were injected one at a time and the suite had to fail (mutation testing). The survivors were each either a gap that became a
new test or a documented equivalent (a change that cannot alter any result). Some tests use the 12 real series in `data/dicom/`
(not committed) and skip when it is absent.

Two checks were run once, locally, against independent software and are not part of the suite. The anonymiser's output on the
12 real series was compared with ITK/GDCM's own reading of the same DICOM files using Convert3D (shipped with ITK-SNAP): the
voxel-to-world matrices agree to within 2e-5, and the dimensions, intensity ranges and every voxel value are identical. And
on a Pico 2 the output for 24 real series (413 MB) is byte-identical to the emulator's. See `docs/dicom.md`.

## Status and what is next

Working and tested: the filters (host and Pico), streaming, the anonymiser end to end on the board, batch mode, the
download script. Not done yet: a policy for burned-in annotations (refuse or blank, as an option), JPEG 2000 and other
compressed DICOM, and the original next step for the Pico, a viewer with manual segmentation masks (HDMI output, a joystick
cursor, live filters), which would use the second core.
