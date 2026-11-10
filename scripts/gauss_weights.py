#!/usr/bin/env python3
"""Integer weights for the separable Gaussian pass (src/gauss.s).

    gauss_weights.py SIGMA RADIUS          print the weights of a Gaussian with that sigma (in pixels), cut at RADIUS
    gauss_weights.py --box RADIUS          a box filter of width 2*RADIUS+1

A kernel is the list w[0..r]: w[0] is the weight of the centre pixel and w[k] the weight of EACH of the two pixels at
distance k, so the full symmetric kernel is w[r], ..., w[1], w[0], w[1], ..., w[r]. The weights are non-negative integers
that sum EXACTLY to 2**SHIFT (SHIFT = 14), which makes the pass exact for a constant image and keeps
65535 * 2**14 + 2**13 below 2**31 (no overflow in a 32-bit accumulator). The sigma is in pixels; to get a size in
millimetres divide by the pixel spacing.

The rounding is deterministic and uses no platform-dependent step beyond math.exp: the ideal weights are scaled to the
total 2**SHIFT, each is rounded down, and the remaining units go to the weights with the largest fractional parts
(ties to the smaller distance).
"""
import math, sys

SHIFT = 14
TOTAL = 1 << SHIFT
MAX_RADIUS = 15


def quantise(ideal):
    """ideal[k], k = 0..r: the real weights of the symmetric kernel (centre once, others counted twice in the total).
    Returns integers w with w[0] + 2*sum(w[1:]) == TOTAL exactly."""
    r = len(ideal) - 1
    mult = [1] + [2] * r
    total = sum(m * v for m, v in zip(mult, ideal))
    scaled = [v * TOTAL / total for v in ideal]
    w = [int(math.floor(v)) for v in scaled]
    short = TOTAL - sum(m * x for m, x in zip(mult, w))
    # give the missing units back, largest fractional part first; a weight at distance k>0 is worth 2 units
    order = sorted(range(r + 1), key=lambda k: (-(scaled[k] - w[k]), k))
    while short > 0:
        progressed = False
        for k in order:
            if mult[k] <= short:
                w[k] += 1
                short -= mult[k]
                progressed = True
                if short == 0:
                    break
        if not progressed:                                   # only 1 unit missing and every candidate is worth 2: the centre takes it
            w[0] += 1
            short -= 1
    assert sum(m * x for m, x in zip(mult, w)) == TOTAL and min(w) >= 0
    return w


def gaussian(sigma, radius):
    if not 0 <= radius <= MAX_RADIUS:
        raise ValueError("radius must be 0..%d" % MAX_RADIUS)
    if radius == 0:
        return [TOTAL]
    if not sigma > 0:
        raise ValueError("sigma must be positive")
    return quantise([math.exp(-0.5 * (k / sigma) ** 2) for k in range(radius + 1)])


def box(radius):
    if not 0 <= radius <= MAX_RADIUS:
        raise ValueError("radius must be 0..%d" % MAX_RADIUS)
    return quantise([1.0] * (radius + 1))


def blur(samples, width, height, w, passes=1):
    """The exact result of the separable pass on a row-major image (a list of ints), applied `passes` times: the model that
    src/gauss.s, the streaming pipeline and `pico_filter.py --verify` are checked against. Edge samples repeat."""
    r = len(w) - 1
    K = w[:0:-1] + w
    rows = [list(samples[y * width:(y + 1) * width]) for y in range(height)]
    for _ in range(passes):
        mid = []
        for row in rows:
            pad = [row[0]] * r + row + [row[-1]] * r
            mid.append([(sum(K[j] * pad[x + j] for j in range(2 * r + 1)) + (1 << (SHIFT - 1))) >> SHIFT for x in range(width)])
        rows = [[(sum(K[j] * mid[min(max(y + j - r, 0), height - 1)][x] for j in range(2 * r + 1)) + (1 << (SHIFT - 1))) >> SHIFT
                 for x in range(width)] for y in range(height)]
    return [v for row in rows for v in row]


def main(argv):
    try:
        if len(argv) == 3 and argv[1] == "--box":
            w = box(int(argv[2]))
        elif len(argv) == 3:
            w = gaussian(float(argv[1]), int(argv[2]))
        else:
            print(__doc__)
            return 2
    except ValueError as e:
        print("error:", e, file=sys.stderr)
        return 1
    print("w =", w, " (sum = %d = 2^%d)" % (w[0] + 2 * sum(w[1:]), SHIFT))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
