# Separable Gaussian pass

Larger smoothing kernels: `src/gauss.s` holds two assembly routines, a horizontal pass over a row and a vertical pass
producing a row. Together they apply a symmetric kernel of width 2r+1 (r up to 15) as two 1-D passes, so the cost grows with the
width instead of with its square. It was first built and measured on the chip as a prototype (below), and is now part of the
streaming mode of the firmware (section "In the streaming pipeline"): a request can carry a kernel instead of naming a 3x3
filter. It is not in the whole-image mode (`IMG1`), the Linux harness (`src/filter.s`) or the filter table.

## The arithmetic

A kernel is `w[0..r]`: `w[0]` weighs the centre sample and `w[k]` weighs *each* of the two samples at distance k. The
weights are non-negative integers that sum **exactly** to 2^14 (`w[0] + 2*sum(w[1..r]) = 16384`). Then

    out[x] = ( w[0]*s[x] + sum_{k=1..r} w[k] * (s[clamp(x-k)] + s[clamp(x+k)]) + 2^13 ) >> 14

with `clamp` the same nearest-edge rule as the 3x3 filters. Because the weights sum to 2^14 exactly, a constant image comes
out unchanged, and because they are non-negative the 32-bit accumulator stays below 65535 * 2^14 + 2^13 < 2^31, so there is
no overflow and no clamping to 0..maxval. (Weights scaled to 2^16 would overflow; derivative kernels with negative weights
need different handling.) `scripts/gauss_weights.py SIGMA RADIUS` makes the weights: the ideal Gaussian is scaled to 2^14,
rounded down, and the missing units go to the weights with the largest fractional parts (a side weight is worth two units,
the centre one). `sigma` is in pixels: divide a size in millimetres by the pixel spacing.

The vertical pass takes `2r+1` row pointers (the caller repeats the first or last row at the image edge) and an
accumulator row of W 32-bit words. The two passes round twice, so the 2-D result can differ from an exactly rounded 2-D
convolution by at most 1 (checked).

## Tests (`make test-gauss`, about 2 s)

The assembly runs under `qemu-riscv32` through `pico/test/gauss_linux.c` and is compared with three independent answers:
an exact integer model (the symmetric kernel written out, the row padded by repeating its ends: a different formulation from
the assembly's per-tap clamping), an exact single-rounding 2-D convolution in integers (the result must be within 1), and a
double-precision Gaussian blur (the result must be within a bound derived from the weight quantisation, which shows the
weights really are a Gaussian). 13,811 horizontal rows (r 0 to 15, widths 1 to 1100, seven patterns including all-65535 and
alternating 0 / 65535, eight kinds of kernel including an impulse and a kernel with all its weight at the largest distance),
3,920 vertical rows and 320 whole images (r in 0, 1, 2, 3, 5, 8, 15) are bit-exact; constant rows and the extreme rows come out unchanged; an impulse
comes out as the kernel; nothing is written past the end of the output row or of the accumulator; the second moment of the
integer kernel equals sigma^2. About 45 deliberately injected bugs in the assembly (the rounding constant, the shifts, the
interior bound at each end, each pointer start, the loop counts, the edge clamps, the pair loop and its tail, the stores, the
saved registers) were each built and run: all caught.

## Cost on the chip

Predicted before any run, by applying the datasheet's cycle table (ALU and multiply 1, load 1 or 2 when the next instruction
depends on it, taken branch 2) to the inner loops: the horizontal pass about `20 + 12 r` cycles per pixel and the vertical pass
`18 + 10 r`. Measured on a Pico 2 (150 MHz, 256 x 96 image, the core's own `mcycle` / `minstret` counters, interrupts off), with
every output checksum identical to the Python model:

| r | H pass | V pass | 2-D measured | 2-D predicted | error | 256 x 256 | 512 x 512 |
|--:|--:|--:|--:|--:|--:|--:|--:|
| 1 | 33.2 | 24.5 | 57.7 | 59.2 | -2.5% | 25 ms | 101 ms |
| 2 | 49.3 | 33.5 | 82.8 | 81.5 | +1.6% | 36 ms | 145 ms |
| 3 | 60.6 | 42.6 | 103.2 | 103.9 | -0.7% | 45 ms | 180 ms |
| 4 | 72.0 | 51.8 | 123.8 | 126.6 | -2.2% | 54 ms | 216 ms |
| 5 | 83.6 | 60.9 | 144.5 | 149.3 | -3.2% | 63 ms | 253 ms |
| 7 | 107.2 | 79.2 | 186.4 | 195.4 | -4.6% | 81 ms | 326 ms |
| 8 | 119.2 | 88.3 | 207.5 | 218.6 | -5.1% | 91 ms | 363 ms |
| 15 | 207.1 | 152.2 | 359.3 | 385.8 | -6.9% | 157 ms | 628 ms |

(Cycles per pixel in the middle columns.) A line through the measurements is `H = 22.7 + 12.24 r`, `V = 15.3 + 9.13 r`,
2-D `= 38.0 + 21.4 r`; the prediction was `38 + 22 r`. The vertical pass is about 9% cheaper than the model charged for it, the
horizontal pass up to 11% dearer at r = 2; the reason has not been traced and nothing depends on it. The IPC is 0.82 to 0.98,
so the loops are limited by the number of instructions they execute, not by stalls.

Against getting the same smoothing by repeating the 3x3 box (n passes have sigma^2 = 2n/3; the 3x3 generic path measures 47
cycles per pixel per pass), the separable pass is no gain below sigma 1 and pays off from sigma 1.5 up: at sigma 1.5 it is
1.1x faster (r = 5), at sigma 2.33 (the default `blur`, 8 passes) 2.0x faster (r = 7), at sigma 3 2.8x (r = 9) and at sigma 5
4.9x (r = 15, against 37 passes), and the shape is a true Gaussian instead of a repeated box.

To repeat the measurement:

    make pico                                           # builds build-pico/gauss_probe.uf2 as well
    picotool load -f build-pico/gauss_probe.uf2         # replaces the filter firmware on the board until you flash it again
    sudo python3 pico/tools/gauss_probe_check.py        # prints measured and predicted cycles per pixel and checks the checksums

`gauss_probe_check.py --selftest` checks the checker itself. For scale, the USB transfer of a 512 x 512 16-bit image takes
about 1 s, so even r = 15 (0.63 s) is below it; a 3x3 pass of the hand-tuned filter path (47 cycles per pixel) is cheaper than
this generic r = 1 (58).

## Where the time goes, and what would be faster (not done)

The loops execute about one instruction per cycle, so the speed-ups are in executing fewer. The horizontal inner loop spends 11
instructions per tap pair, 4 of them on pointer and counter updates; unrolling it by two removes about a quarter. For r up to
about 4 the weights fit in registers and the loop can be written out straight (`lhu lhu add mul add` per pair, 5 instructions
instead of 11), as the Sobel fast path does. The vertical pass loads and stores the accumulator once per tap pair; doing two
or four tap pairs per round trip saves 15 to 20% of its instructions. None of this is needed yet: at 256 x 256 even r = 7 costs
81 ms.

## In the streaming pipeline

The streaming pipeline (`docs/streaming.md`) kept a ring of exactly 3 rows per stage; a separable stage keeps `2r+1`. Row j of a
stage is produced once rows up to j+r have arrived (or the last row has, then the bottom edge repeats the last row), so a stage
adds r rows of delay instead of 1. The horizontal pass is taken as each row arrives, into the stage's ring; the vertical pass,
over rows j-r .. j+r clamped to the image, is taken when row j is emitted, into the next stage's arrival buffer (or the output
row). `passes` stages in a row apply the kernel that many times. Memory is `passes*(2r+1)*width*2` bytes plus a staging row, an
output row and `width*4` bytes of accumulators, independent of the height; the 3x3 stages are unchanged and bit-identical.

The request is the same 48-byte `STR1` header with bit 1 of `flags` set, followed by `u32 r` and the `r+1` weights (`pico/README.md`).
The host makes the weights, so the device needs no exponential and sigma can be given in millimetres. The device validates them
before it acknowledges, because the proof of "no overflow, no clamp" rests on them: radius at most 15, every weight non-negative,
and the total exactly 2^14 (computed in 64 bits, so a sum that wraps around 32 bits is still refused); otherwise status 10.

    pico_filter.py in.pgm out.pgm gauss --sigma 2.0 [--radius R] [PASSES] [--window C W] [--verify]
    pico_filter.py in.pgm out.pgm gauss --sigma-mm 1.5 --pixel-mm 0.94

`--verify` compares with the exact Python model (`scripts/gauss_weights.py: blur`); a window is applied by the Linux harness.
The radius defaults to ceil(3 sigma), at most 15 (a note is printed when that cuts the kernel short of 3 sigma).

Tests (`make test-gauss-stream`, about 10 s; the model is written independently of the client's own): 714 jobs bit-exact
(radii 0 to 15, four kinds of kernel, widths 1 to 100, heights from 1 up through every height around 2r+1, 8, 12 and 16 bits,
1 and 2 passes, some windowed), chains of up to 5 passes on extreme data, a constant image unchanged, the 3x3 filters and the
separable ones alternating on one connection, the memory limit to the byte (with and without a window), ten ways to send a bad
kernel (radius 16 and 255, negative weights, sums of 0, 16383 and 16385, two sums that wrap around 32 bits, a weight of 2^31)
each answered with status 10 and followed by a good job, the same flag in whole-image mode, a bad width or pass count with a
kernel after the header, a host that stops before `r`, after `r` and in the middle of the weights, a bad sample in the first,
middle and last row, and a host that stops in the middle of a row. A host program checks the memory layout for every width 1 to 70,
radius 0 to 15 and 0 to 4 passes (regions inside the reported size, no overlap, 2-byte rows and 4-byte accumulators aligned), because
the emulator lets a misaligned access through and the Hazard3 cores may trap on one. The cycle counters read 0 under the
emulator.

49 deliberately injected bugs in the new streaming and protocol code (the ring index and size, when a row may be emitted, the edge
clamps, which buffer each stage writes to, the memory formula, the alignment of the accumulators, the kernel checks and the
protocol's parsing, status and timeouts) were each built and run: 44 caught, 5 cannot change an answer. The first round left four
real gaps, all now tested: a sum that wraps around 32 bits with non-negative weights, a negative last weight with a correct sum,
a radius above 15 reaching the validator or the memory function, and `stream_begin_sep` relying on its caller to check the kernel.

Measured on a Pico 2 (240 x 240, 12-bit, `gauss --sigma 2.0`, radius 6, whole slice `BRATS_001_t1c_z77.pgm`): the output is
byte-identical to the exact Python model, with and without a window (`--window 761 1350`), and the round trip is 0.22 s and
0.23 s. The core reports 12,056,550 cycles (11,099,914 instructions, IPC 0.92) without the window and 11,136,578 (10,289,133)
with it, against 9.6 million for the filter alone from the probe's cycles per pixel (166 at radius 6). The counters are read
around every row, in a stream, and the core also services USB inside that window; the two runs differ only in the 57,600 bytes
of output that the window removes, 0.92 million cycles, about 16 per byte. If the cost of USB were exactly linear in the
bytes moved, USB would account for about 3.7 million of the 12.06 million and the filter itself for about 8.4 million (145 cycles
per pixel), but two runs cannot show that it is linear. Either way the whole compute, 74 to 80 ms, sits inside the 211 ms that
the 115,200 bytes need to cross the USB link at its measured rate: the round trip is the USB time, which is the minimum of the
range estimated in "Is optimising the loops worth it now?" below.

Not done: whole-image mode, the Linux harness and the filter table; a fast path for small radii (see above); a sigma taken from
the voxel size of a NIfTI file (the client takes `--pixel-mm`); signed or negative-weight kernels (derivative filters), which
need a different overflow argument.

## Is optimising the loops worth it now? (an estimate, not a measurement)

The compute time (the measured cycles per pixel at 150 MHz) against the USB time (about 546 KB/s each way, measured), for a
round trip through `pico_filter.py`, where the upload and the download overlap and the compute overlaps them more or less (so the
round trip lies between the larger of the two and their sum):

| image | r | USB | compute now | round trip now | compute if the loops were faster | saves |
|---|--:|--:|--:|--:|--:|--:|
| 240 x 240 | 3 | 211 ms | 39 ms | 211 to 250 ms | 20 ms | 0 to 20 ms |
| 240 x 240 | 6 | 211 ms | 64 ms | 211 to 275 ms | 48 ms | 0 to 16 ms |
| 256 x 256 | 7 | 240 ms | 82 ms | 240 to 322 ms | 61 ms | 0 to 20 ms |
| 512 x 512 | 3 | 960 ms | 178 ms | 960 to 1139 ms | 89 ms | 0 to 89 ms |
| 512 x 512 | 7 | 960 ms | 328 ms | 960 to 1288 ms | 246 ms | 0 to 82 ms |

("Faster loops" assumes half the cycles for r up to 4 (weights in registers, straight-line loop) and three quarters above (unrolling), both
guesses from instruction counts.) Through USB the compute is 16 to 25% of the round trip at most, so optimising saves up to about
8%: not worth doing now. The answer changes for the planned viewer, where the image stays on the chip and every change of a
parameter filters it again, with no USB in the way: there 256 x 256 at r = 3 would go from about 22 to about 45 frames per second
and 512 x 512 at r = 3 from 5.6 to 11, but at r = 7 only from 3.1 to 4.1 (512 x 512) because the gain from unrolling is small;
for large radii the better tool is a method whose cost does not grow with r (running sums for a box, a recursive Gaussian), not
a faster generic loop. The decision belongs to the viewer, when its frame-rate target is known.

## Why not an FFT

Convolution by FFT costs the same whatever the kernel size, which is its attraction for very large kernels. Here it does not
pay, and it cannot be done in the streaming design:

* **Memory.** The FFT needs the whole image resident (the column transforms cross every row), which is the opposite of the
  streaming mode that lets any height through 420 KB. A real 256 x 256 image has a spectrum of 256 x 129 complex values,
  129 KB in 16-bit fixed point, which fits; but wrap-around makes an FFT convolution circular, so an image that fills 256
  needs padding to 512 x 512 (514 KB) to behave like the edge-repeating filters, which does not fit in 520 KB.
* **Arithmetic.** The Hazard3 cores have no floating-point unit; 16-bit fixed point needs scaling at each stage and loses
  precision on 16-bit data, and software floating point is about two orders of magnitude slower.
* **Speed.** A 256 x 256 transform is about 524,000 butterflies at about 22 cycles, so a convolution (forward, multiply,
  inverse) is about 358 cycles per pixel, 156 ms. The separable pass costs `38 + 22 r` cycles per pixel, so it is faster
  until r = 15, a 31 x 31 kernel. A Gaussian is separable, so nothing is gained; for very large sigma a recursive (IIR)
  Gaussian or a running sum costs a few multiplies per pixel whatever the size and needs only a few rows.

(The FFT figures are estimates from instruction counts, not measurements.)
