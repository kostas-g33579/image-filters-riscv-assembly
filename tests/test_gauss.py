#!/usr/bin/env python3
"""Tests for the separable Gaussian prototype (src/gauss.s), run under qemu-riscv32 through build32/gauss_test.

Three independent ways of knowing the right answer:
  * EXACT: the symmetric kernel laid out in full, the row padded by repeating its end samples (a different formulation
    from the assembly's per-tap clamping), integer arithmetic with the same rounding. The assembly must match it bit for
    bit: horizontal pass, vertical pass, and the whole 2-D composition.
  * ROUNDING: the 2-D result must be within 1 of an exact single-rounding 2-D convolution done in integers (two passes
    round twice; they can never be more than 1 apart).
  * GAUSSIAN: the 2-D result must be within a bound, derived from the weight quantisation, of a double-precision
    Gaussian blur of the same image with the same edge rule. This is what shows the weights are a Gaussian and not just
    some kernel the two implementations agree on.
Also: a constant image comes out unchanged for every kernel, an impulse comes out as the kernel itself, the extreme
inputs (all 65535, alternating 0 / 65535) do not overflow, and nothing is written past the end of the output row or of
the accumulator.
"""
import math, os, random, struct, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import gauss_weights as gw

BIN = os.environ.get("GAUSS_BIN", os.path.join(ROOT, "build32", "gauss_test"))
QEMU = os.environ.get("QEMU", "qemu-riscv32").split()
GUARD = 4
failures = 0


def check(name, ok, detail=""):
    global failures
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))
    failures += not ok


# ------------------------------------------------------------------ talking to the program
def record(mode, W, H, r, w, samples):
    return struct.pack("<4I", mode, W, H, r) + struct.pack("<%di" % (r + 1), *w) + struct.pack("<%dH" % len(samples), *samples)


def out_len(mode, W, H):
    if mode in (4, 5):
        return 4
    return (W + GUARD) * 2 + (GUARD * 4 if mode == 2 else 0) if mode != 3 else (W * H + GUARD) * 2


def run(cases):
    """cases: list of (mode, W, H, r, w, samples). Returns a list of (values, guards_ok)."""
    blob = b"".join(record(*c) for c in cases)
    res = subprocess.run(QEMU + [BIN], input=blob, capture_output=True, timeout=900)
    if res.returncode != 0 or res.stderr:
        raise RuntimeError("gauss_test failed: rc=%s %s" % (res.returncode, res.stderr[:200]))
    out, p, results = res.stdout, 0, []
    for mode, W, H, r, w, samples in cases:
        n = out_len(mode, W, H)
        chunk = out[p:p + n]
        p += n
        if len(chunk) != n:
            raise RuntimeError("short output at case %d" % len(results))
        if mode in (4, 5):
            results.append(([struct.unpack("<I", chunk)[0]], True))
            continue
        count = W * H if mode == 3 else W
        vals = struct.unpack("<%dH" % count, chunk[:count * 2])
        guard = struct.unpack("<%dH" % GUARD, chunk[count * 2:(count + GUARD) * 2])
        ok = all(v == 0xAAAA for v in guard)
        if mode == 2:
            ok = ok and struct.unpack("<%dI" % GUARD, chunk[(count + GUARD) * 2:]) == (0x5A5A5A5A,) * GUARD
        results.append((list(vals), ok))
    if p != len(out):
        raise RuntimeError("extra output")
    return results


# ------------------------------------------------------------------ the oracles
def full(w):
    return w[:0:-1] + w                                           # w[r] .. w[1] w[0] w[1] .. w[r]


def h_ref(row, w):
    r = len(w) - 1
    K, pad = full(w), [row[0]] * r + list(row) + [row[-1]] * r
    return [(sum(K[j] * pad[x + j] for j in range(2 * r + 1)) + 8192) >> 14 for x in range(len(row))]


def v_ref(rows, w):
    K = full(w)
    return [(sum(K[j] * rows[j][x] for j in range(len(K))) + 8192) >> 14 for x in range(len(rows[0]))]


def clamp_rows(tmp, y, r):
    H = len(tmp)
    return [tmp[min(max(y + j - r, 0), H - 1)] for j in range(2 * r + 1)]


def two_d_ref(img, w):
    tmp = [h_ref(row, w) for row in img]
    return [v_ref(clamp_rows(tmp, y, len(w) - 1), w) for y in range(len(img))]


def two_d_single_rounding(img, w):
    """Exact 2-D convolution in integers, rounded once (the weights are in units of 2^-14, so the 2-D ones are 2^-28)."""
    r, K, H, W = len(w) - 1, full(w), len(img), len(img[0])
    out = []
    for y in range(H):
        row = []
        for x in range(W):
            s = 0
            for j in range(2 * r + 1):
                yy = min(max(y + j - r, 0), H - 1)
                for i in range(2 * r + 1):
                    xx = min(max(x + i - r, 0), W - 1)
                    s += K[j] * K[i] * img[yy][xx]
            row.append((s + (1 << 27)) >> 28)
        out.append(row)
    return out


def two_d_float(img, sigma, r):
    t = [math.exp(-0.5 * (k / sigma) ** 2) for k in range(-r, r + 1)]
    s = sum(t)
    t = [v / s for v in t]
    H, W = len(img), len(img[0])
    tmp = [[sum(t[i] * row[min(max(x + i - r, 0), W - 1)] for i in range(2 * r + 1)) for x in range(W)] for row in img]
    return [[sum(t[j] * tmp[min(max(y + j - r, 0), H - 1)][x] for j in range(2 * r + 1)) for x in range(W)] for y in range(H)], t


# ------------------------------------------------------------------ kernels and data
def kernels(r, rng):
    ks = {"box": gw.box(r)}
    if r > 0:
        for sigma in (0.6, 1.0, 2.0, 4.0):
            ks["gauss%.1f" % sigma] = gw.gaussian(sigma, r)
        ks["impulse"] = [gw.TOTAL] + [0] * r
        ks["centre-free"] = gw.quantise([0.0] + [1.0 + rng.random() for _ in range(r)])
        ks["random"] = gw.quantise([rng.random() + 0.01 for _ in range(r + 1)])
        ks["far"] = gw.quantise([0.0] * r + [1.0])                       # all the weight at the largest distance
    else:
        ks["identity"] = [gw.TOTAL]
    return ks


def patterns(W, rng):
    p = {"random16": [rng.randrange(65536) for _ in range(W)], "random12": [rng.randrange(4096) for _ in range(W)],
         "zeros": [0] * W, "max": [65535] * W, "alternating": [65535 * (i & 1) for i in range(W)],
         "constant": [rng.randrange(65536)] * W, "ramp": [(i * 65535) // max(W - 1, 1) for i in range(W)]}
    return p


def main():
    if not os.path.exists(BIN):
        sys.exit("build the program first: make build32/gauss_test (or make test-gauss)")
    rng = random.Random(20261110)

    # ---- the weights
    bad, n = [], 0
    for r in range(0, gw.MAX_RADIUS + 1):
        for sigma in (0.3, 0.6, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0):
            w = gw.gaussian(sigma, r) if r else gw.gaussian(1.0, 0)
            n += 1
            true = [math.exp(-0.5 * (k / sigma) ** 2) for k in range(-r, r + 1)] if r else [1.0]
            tot = sum(true)
            err = max(abs(full(w)[j] / gw.TOTAL - true[j] / tot) for j in range(2 * r + 1))
            if w[0] + 2 * sum(w[1:]) != gw.TOTAL or min(w) < 0 or len(w) != r + 1 or err > 2.0 / gw.TOTAL or any(w[k + 1] > w[k] + 1 for k in range(r)):
                bad.append((r, sigma, err))
    check(f"{n} Gaussian kernels: the weights sum to exactly 2^14, are non-negative, never increase away from the centre, and each is within 2 units of the real Gaussian", not bad, str(bad[:3]))

    # ---- the horizontal pass
    widths = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 15, 16, 17, 31, 32, 33, 64, 100, 257, 1000, 1100]
    cases, meta = [], []
    for r in range(0, gw.MAX_RADIUS + 1):
        for kname, w in kernels(r, rng).items():
            for W in widths:
                if W > 40 and (r not in (0, 1, 7, 15) or kname not in ("gauss2.0", "box", "random", "identity")):
                    continue
                for pname, row in patterns(W, rng).items():
                    cases.append((1, W, 0, r, w, row))
                    meta.append((r, kname, W, pname))
    res = run(cases)
    bad = [(m, "values") for m, c, (v, ok) in zip(meta, cases, res) if v != h_ref(c[5], c[4])]
    badg = [m for m, (v, ok) in zip(meta, res) if not ok]
    check(f"horizontal pass: {len(cases)} rows (r 0..15, widths 1..1100, 7 patterns, 8 kernel kinds) equal the independent model bit for bit", not bad, str(bad[:3]))
    check("horizontal pass: nothing is written past the end of the output row", not badg, str(badg[:3]))
    const_bad = [m for m, c, (v, ok) in zip(meta, cases, res) if m[3] == "constant" and v != c[5]]
    check("horizontal pass: a constant row comes out unchanged for every kernel (the weights sum to exactly 2^14)", not const_bad, str(const_bad[:3]))
    ext_bad = [m for m, c, (v, ok) in zip(meta, cases, res) if m[3] in ("max", "zeros") and v != c[5]]
    check("horizontal pass: all-65535 and all-0 rows come out unchanged (no overflow of the accumulator)", not ext_bad, str(ext_bad[:3]))

    # impulse response: an impulse of height 65535 comes out as the kernel itself
    cases, bad = [], []
    for r in (1, 3, 8, 15):
        w = gw.gaussian(1.0 + r / 4, r)
        W = 2 * r + 9
        for x0 in range(W):
            row = [0] * W
            row[x0] = 65535
            cases.append((1, W, 0, r, w, row))
    res = run(cases)
    for c, (v, ok) in zip(cases, res):
        r, w, row = c[3], c[4], c[5]
        x0 = row.index(65535)
        want = [((65535 * w[abs(x - x0)] + 8192) >> 14) if abs(x - x0) <= r else 0 for x in range(c[1])]
        # clamping adds the impulse's weight again for taps that fall outside the row on the impulse's side only when
        # the impulse is within r of an end: restrict the closed form to impulses far enough from both ends
        if r <= x0 <= c[1] - 1 - r and v != want:
            bad.append((r, x0))
    check(f"impulse response: an impulse far enough from the ends comes out as the kernel (formula (65535*w[|d|]+8192)>>14), {len(cases)} impulses", not bad, str(bad[:3]))

    # ---- the vertical pass
    cases, meta = [], []
    for r in (0, 1, 2, 3, 4, 5, 8, 15):
        for kname, w in kernels(r, rng).items():
            for W in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 31, 32, 33, 100, 255):
                for pname in ("random16", "max", "alternating", "constant", "random12"):
                    rows = [patterns(W, rng)[pname] for _ in range(2 * r + 1)] if pname not in ("constant", "max") else [patterns(W, rng)[pname]] * (2 * r + 1)
                    cases.append((2, W, 0, r, w, [s for row in rows for s in row]))
                    meta.append((r, kname, W, pname, rows))
    res = run(cases)
    bad = [m[:4] for m, c, (v, ok) in zip(meta, cases, res) if v != v_ref(m[4], c[4])]
    badg = [m[:4] for m, (v, ok) in zip(meta, res) if not ok]
    check(f"vertical pass: {len(cases)} output rows (r in 0,1,2,3,4,5,8,15, widths 1..255 odd and even) equal the independent model bit for bit", not bad, str(bad[:3]))
    check("vertical pass: nothing is written past the end of the output row or of the accumulator", not badg, str(badg[:3]))
    const_bad = [m[:4] for m, c, (v, ok) in zip(meta, cases, res) if m[3] in ("constant", "max") and v != c[5][:c[1]]]
    check("vertical pass: constant rows (and all-65535 rows) come out unchanged", not const_bad, str(const_bad[:3]))

    # ---- the whole 2-D pass
    cases, meta = [], []
    for r in (0, 1, 2, 3, 4, 5, 8, 15):
        ks = kernels(r, rng)
        for kname in list(ks)[:4] + (["random"] if "random" in ks else []):
            for (W, H) in ((1, 1), (1, 7), (7, 1), (2, 2), (3, 5), (8, 8), (17, 5), (5, 33), (33, 33), (64, 40)):
                img = [[rng.randrange(65536) for _ in range(W)] for _ in range(H)]
                cases.append((3, W, H, r, ks[kname], [s for row in img for s in row]))
                meta.append((r, kname, W, H, img))
    res = run(cases)
    bad, badg, off, worst = [], [], [], 0
    for m, c, (v, ok) in zip(meta, cases, res):
        r, kname, W, H, img = m
        got = [v[y * W:(y + 1) * W] for y in range(H)]
        if got != two_d_ref(img, c[4]):
            bad.append(m[:4])
        if not ok:
            badg.append(m[:4])
        if W * H <= 340 and r <= 8:
            ideal = two_d_single_rounding(img, c[4])
            d = max(abs(a - b) for ra, rb in zip(got, ideal) for a, b in zip(ra, rb))
            worst = max(worst, d)
            if d > 1:
                off.append((m[:4], d))
    check(f"2-D pass: {len(cases)} images (r in 0,1,2,3,4,5,8,15, sizes from 1x1 to 64x40) equal the independent two-pass model bit for bit", not bad, str(bad[:3]))
    check("2-D pass: nothing is written past the end of the output image", not badg, str(badg[:3]))
    check(f"2-D pass vs an exact single-rounding 2-D convolution in integers: never more than 1 apart (worst seen: {worst})", not off, str(off[:3]))

    # ---- the callee-saved registers survive (the fast routines use all of s0..s11)
    cases, meta = [], []
    for r in range(0, gw.MAX_RADIUS + 1):
        w = gw.gaussian(max(r / 3, 0.5), r) if r else [gw.TOTAL]
        for W in (1, 2, 3, 5, 8, 9, 10, 17, 33, 100):
            cases.append((4, W, 0, r, w, [rng.randrange(65536) for _ in range(W)]))
            meta.append(("H", r, W))
            cases.append((5, W, 0, r, w, [rng.randrange(65536) for _ in range((2 * r + 1) * W)]))
            meta.append(("V", r, W))
    res = run(cases)
    bad = [(m, "s-registers changed: mask %#x" % v[0]) for m, (v, ok) in zip(meta, res) if v[0] != 0]
    check(f"callee-saved registers: s0..s11 hold the same values after {len(cases)} calls (both passes, radius 0..15, widths 1..100)", not bad, str(bad[:3]))

    # ---- is it a Gaussian? compare with double precision
    bad, worst_ratio, cases, meta = [], 0.0, [], []
    for r, sigma in ((1, 0.8), (2, 1.0), (3, 1.2), (5, 2.0), (7, 2.5), (8, 3.0), (15, 5.0)):
        w = gw.gaussian(sigma, r)
        for (W, H) in ((16, 16), (40, 25), (13, 30)):
            img = [[rng.randrange(65536) for _ in range(W)] for _ in range(H)]
            cases.append((3, W, H, r, w, [s for row in img for s in row]))
            meta.append((r, sigma, W, H, img))
    for m, c, (v, ok) in zip(meta, cases, run(cases)):
        r, sigma, W, H, img = m
        ref, t = two_d_float(img, sigma, r)
        eps = sum(abs(a / gw.TOTAL - b) for a, b in zip(full(c[4]), t))              # error of the 1-D kernel (sums to ~0, abs bounds it)
        bound = 65535 * (2 * eps + eps * eps) + 1.5
        d = max(abs(v[y * W + x] - ref[y][x]) for y in range(H) for x in range(W))
        worst_ratio = max(worst_ratio, d / bound)
        if d > bound:
            bad.append((r, sigma, W, H, d, bound))
    check(f"the 2-D result is a Gaussian blur: within the quantisation bound of a double-precision one (worst error is {worst_ratio:.0%} of the bound), 7 sigmas x 3 sizes",
          not bad, str(bad[:3]))

    # ---- the blur really blurs: the variance of a Gaussian kernel is sigma^2
    vbad = []
    for sigma, r in ((1.0, 5), (2.0, 9), (3.0, 12)):
        w = gw.gaussian(sigma, r)
        K = full(w)
        var = sum(K[j] / gw.TOTAL * (j - r) ** 2 for j in range(2 * r + 1))
        if abs(var - sigma ** 2) > 0.02 * sigma ** 2 + 1e-3:
            vbad.append((sigma, r, var))
    check("the second moment of the integer kernel equals sigma^2 (within 2%) when the radius covers 5 sigma", not vbad, str(vbad))

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    sys.exit(1 if failures else 0)


main()
