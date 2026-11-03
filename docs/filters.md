# Filter reference

Every filter is a 3x3 convolution. For each pixel the program takes the 3x3
neighborhood, multiplies each of the nine pixels by a weight (the *kernel*),
adds the results, then:

1. takes the absolute value (only if the filter's *abs* flag is set),
2. divides by the *divisor*, rounding to nearest,
3. adds the *bias*,
4. clamps to 0..255.

Pixels past the image border use the nearest edge pixel. The kernel is read
row by row: top-left, top-middle, top-right, then the middle row, then the
bottom row.

Two rules of thumb for reading a kernel:

- **Weights sum to the divisor** (so the result keeps overall brightness):
  blur, gaussian, sharpen.
- **Weights sum to 0**: flat areas become black (or the bias), and only
  *changes* between neighboring pixels show up: edge, emboss, the Sobel
  filters.

## Summary

| Filter     | Kernel                      | Div | Bias | Abs | Default passes | What it does |
|------------|-----------------------------|----:|-----:|:---:|---------------:|--------------|
| `identity` | 0 0 0 / 0 1 0 / 0 0 0       | 1   | 0    | no  | 1 | Returns the image unchanged. A test baseline, not a real filter. |
| `blur`     | 1 1 1 / 1 1 1 / 1 1 1       | 9   | 0    | no  | 8 | Averages each pixel with its 8 neighbors: smooths and softens. |
| `gaussian` | 1 2 1 / 2 4 2 / 1 2 1       | 16  | 0    | no  | 4 | Blur that weights nearby pixels more than far ones: smoother, less blocky. |
| `sharpen`  | 0 -1 0 / -1 5 -1 / 0 -1 0   | 1   | 0    | no  | 1 | Exaggerates how different a pixel is from its neighbors: crisper edges and texture. |
| `edge`     | -1 -1 -1 / -1 8 -1 / -1 -1 -1 | 1 | 0    | yes | 1 | Outlines every spot that differs from its surroundings, in all directions. |
| `emboss`   | -2 -1 0 / -1 0 1 / 0 1 2    | 1   | 128  | no  | 1 | Relief effect, as if lit from the top-left; flat areas are mid-gray. |
| `sobel`    | gx + gy (see below)         | 4   | 0    | yes | 1 | Edge strength in any direction: the standard edge detector. |
| `sobelx`   | -1 0 1 / -2 0 2 / -1 0 1    | 4   | 0    | yes | 1 | Vertical edges only (brightness changing left to right). |
| `sobely`   | -1 -2 -1 / 0 0 0 / 1 2 1    | 4   | 0    | yes | 1 | Horizontal edges only (brightness changing top to bottom). |

## Each filter in detail

**identity.** Weight 1 on the center pixel, 0 everywhere else, so the output
equals the input. It exists so the tests can check that the pipeline itself
(reading, buffering, writing) doesn't corrupt anything.

**blur.** A box blur: all nine pixels count equally, divided by 9. One 3x3
pass has a 1-pixel radius, which is barely visible on a 256x256 image, so it
defaults to 8 passes. Repeating a box blur approaches a Gaussian shape.

**gaussian.** Same idea as blur, but the center counts 4, the sides 2 and the
corners 1 (divided by 16), so close pixels matter more. The result is smoother
and avoids the faint blockiness of a box blur. Defaults to 4 passes. Somewhat
redundant with `blur`, but it looks nicer.

**sharpen.** The center is multiplied by 5 and the four direct neighbors are
subtracted. The weights still sum to 1, so brightness is preserved, but any
pixel that stands out from its surroundings is pushed further out, which
reads as extra crispness. Sensible at 1 pass; repeating it amplifies noise and
creates bright/dark halos along edges.

**edge.** A Laplacian: the center counts 8 and each of the 8 neighbors
subtracts 1. In a flat area the result is exactly 0 (black); wherever a pixel
differs from its neighborhood it lights up. The abs flag is on, so both sides
of an edge show (without it, only pixels brighter than their surroundings
would). It responds to fine texture and noise as well as true outlines, so
photos come out busy.

**emboss.** A diagonal gradient: pixels toward the bottom-right add, pixels
toward the top-left subtract. The kernel sums to 0, so flat areas come out
exactly at the bias, 128 (neutral gray), and edges become light or dark
depending on which way they face, which gives the raised-relief look.

**sobel.** The standard edge detector. It computes two gradients, `gx` (the
`sobelx` kernel) and `gy` (the `sobely` kernel), and outputs `|gx| + |gy|`
divided by 4. Strong edges at any angle come out bright; flat areas are black.
The middle row/column counts double, which makes it less noise-sensitive than
`edge`.

**sobelx.** Only the horizontal-direction gradient: `|gx|`. It lights up
vertical edges (where brightness changes left to right) and ignores
horizontal ones. The abs flag makes both dark-to-light and light-to-dark
transitions show.

**sobely.** The same for the other direction: `|gy|`, lighting up horizontal
edges (brightness changing top to bottom).

## Notes and limits

- Only `blur` and `gaussian` make sense with many passes; the others default
  to 1. Any filter accepts a pass count: `filter sharpen 3`.
- The Sobel filters divide by 4 so a perfect step edge reaches exactly 255.
  Where both gradients are strong (diagonal or corner edges), `sobel` can
  exceed 255 and is clamped.
- All filters are 3x3 and grayscale (8-bit PGM only). Filters that aren't
  convolutions (median, threshold) would need separate code.
- The kernels live in `filter_table` in `src/filter.s`. `sobel` is the one
  entry with a second kernel (a pointer to the `sobely` record).
