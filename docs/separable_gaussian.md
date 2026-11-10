# Separable Gaussian pass (prototype)

A first, unintegrated version of larger smoothing kernels: `src/gauss.s` holds two assembly routines, a horizontal pass over
a row and a vertical pass producing a row. Together they apply a symmetric kernel of width 2r+1 (r up to 15) as two 1-D
passes, so the cost grows with the width instead of with its square. It is not yet connected to `filter_core`, the filter
table, the streaming pipeline or the wire protocol: the point of the prototype is to fix the arithmetic, test it, and
measure the cost on the chip before building the rest.

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
