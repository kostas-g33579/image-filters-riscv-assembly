# Window / level

Medical images are usually stored with far more intensity levels than a
screen can show: a CT slice is typically 12 bits (4096 levels) stored in 16
bits, while a display shows 256 gray levels. Showing the whole range at once
squeezes soft tissue (which differs by only a few dozen to a few hundred
counts) into a handful of nearly identical grays. **Windowing** picks a
sub-range and stretches *only that* across the full black-to-white range.

- **Center (level) `c`**: the stored value that maps to mid-gray.
- **Width `w`**: how many stored values map across the black-to-white ramp.
  Narrow = high contrast over a small range; wide = low contrast over a big
  range.
- Everything below the window is black, everything above is white.

```
filter identity window 1064 400        # c = 1064, w = 400: soft-tissue window
filter gaussian window 1064 400        # denoise first, then window
make run FILTER=identity WINDOW="224 1400"
```

The output of a windowed run is always an **8-bit** image (maxval 255),
whatever the input depth. The filter runs on the full-precision samples
first; only the final display mapping reduces them to 8 bits.

## The function

This implements the DICOM linear window function (PS3.3, C.11.2.1.2) with
output range 0..255. For a stored value `x`:

```
x <= c - 0.5 - (w-1)/2   ->   0
x >  c - 0.5 + (w-1)/2   ->   255
otherwise:  y = ( (x - (c - 0.5)) / (w - 1) + 0.5 ) * 255
```

The `- 0.5` and `w - 1` terms are DICOM's own: they make the window treat
stored values as discrete levels (a window of width `w` spans exactly `w`
values) rather than as a continuous range.

## The integer version (what `window_level` in `src/kernel.s` computes)

There is no floating point on the target, so the formula is rearranged to
integers. Multiply the inside of the fraction through by 2:

```
N = 2x - 2c + w                       (an integer; the window test and the ramp both use it)

N <= 0          ->   0                (same as x <= c - 0.5 - (w-1)/2)
N > 2(w-1)      ->   255              (same as x >  c - 0.5 + (w-1)/2)
otherwise       ->   y = N * 255 / (2(w-1)),  rounded to nearest
                       = (N*255 + (w-1)) / (2(w-1))      (integer divide)
```

Adding `(w-1)` before dividing by `2(w-1)` is the "add half the divisor"
rounding trick (the same one the filter divide uses). Width 1 is the one
special case: `2(w-1)` would be zero, but then no `N` is in the middle range
(`N` is odd), so the function is a hard threshold: 0 at or below the center,
255 above.

`tests/run_tests.py` checks this against the DICOM formula evaluated in
exact rational arithmetic (Python `fractions`), straight from the wording
above, over ramps that cover the whole input range and at the window edges.

## What it does *not* do (yet)

- **Rescale slope/intercept.** DICOM CT stores raw values with a linear
  rescale to Hounsfield units (`HU = slope * raw + intercept`), and window
  centers are usually quoted in HU. Here centers and widths are in *stored*
  units. For the test phantom the stored value is HU + 1024, so a typical
  soft-tissue window of c = 40, w = 400 HU is `window 1064 400`.
- **Signed samples.** PGM samples are unsigned.
- **Inverted (MONOCHROME1) display** and non-linear (sigmoid) windows.

## Trying it

```sh
make phantom                       # synthetic 512x512 CT-like image
cp images/phantom16.pgm images/input.pgm
make run FILTER=identity WINDOW="1064 400"    # soft tissue
make run FILTER=identity WINDOW="224 1400"    # lungs
make run FILTER=identity WINDOW="1700 2000"   # bone
```
