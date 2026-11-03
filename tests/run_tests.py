#!/usr/bin/env python3
"""Regression tests for filter.s.

Runs build/filter under qemu-riscv64 inside a throwaway temp directory (the
program uses cwd-relative images/input.pgm and images/output.pgm), so the
project's own images/ folder is never touched. Compares the output against
an independent Python reference of the kernel convolution: clamp-to-edge
taps, weighted sum, optional abs, round-to-nearest divide (halves away from
zero), bias scaled to the bit depth, clamp to 0..maxval, repeated for the
pass count.

Small and medium images are checked in full. Images too big for a pure-Python
full reference (up to the 16-million-pixel limit) are checked at sampled
points: all four corners, points along every edge, and random interior pixels.

The filter parameters below are deliberately duplicated from the assembly's
filter table rather than parsed from it, so a typo on either side shows up.

Usage: python3 tests/run_tests.py     (or: make test)
"""
import os
import random
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "build", "filter")
QEMU = os.environ.get("QEMU", "qemu-riscv64")

with open(os.path.join(ROOT, "src", "filter.s")) as f:
    src = f.read()
MAX_PIXELS = int(re.search(r"^\.equ MAX_PIXELS,\s*(\d+)", src, re.M).group(1))
MAX_PASSES = int(re.search(r"^\.equ MAX_PASSES,\s*(\d+)", src, re.M).group(1))

# name: (kernel row-major, divisor, bias, abs, default passes[, second kernel])
# With a second kernel the value is |sum1| + |sum2| (gradient magnitude).
FILTERS = {
    "identity": ([0, 0, 0, 0, 1, 0, 0, 0, 0], 1, 0, False, 1),
    "blur":     ([1, 1, 1, 1, 1, 1, 1, 1, 1], 9, 0, False, 8),
    "gaussian": ([1, 2, 1, 2, 4, 2, 1, 2, 1], 16, 0, False, 4),
    "sharpen":  ([0, -1, 0, -1, 5, -1, 0, -1, 0], 1, 0, False, 1),
    "edge":     ([-1, -1, -1, -1, 8, -1, -1, -1, -1], 1, 0, True, 1),
    "emboss":   ([-2, -1, 0, -1, 0, 1, 0, 1, 2], 1, 128, False, 1),
    "sobel":    ([-1, 0, 1, -2, 0, 2, -1, 0, 1], 4, 0, True, 1,
                 [-1, -2, -1, 0, 0, 0, 1, 2, 1]),
    "sobelx":   ([-1, 0, 1, -2, 0, 2, -1, 0, 1], 4, 0, True, 1),
    "sobely":   ([-1, -2, -1, 0, 0, 0, 1, 2, 1], 4, 0, True, 1),
}


def finish(t, t2, kernel2, use_abs, div, bias, maxval):
    """Everything after the weighted sums: magnitude/abs, rounded divide,
    bias scaled to the bit depth, clamp to 0..maxval."""
    if kernel2:
        t = abs(t) + abs(t2)
    if use_abs:
        t = abs(t)
    q = (abs(t) + div // 2) // div          # round half away from zero
    t = q if t >= 0 else -q
    t += (bias * (maxval + 1)) >> 8          # bias is in 1/256ths of maxval+1
    return min(max(t, 0), maxval)


def reference(name, w, h, px, passes=None, maxval=255):
    """Full reference over every pixel. px: sequence of ints."""
    kernel, div, bias, use_abs, default_passes, *rest = FILTERS[name]
    kernel2 = rest[0] if rest else None
    if passes is None:
        passes = default_passes
    cur = list(px)
    for _ in range(passes):
        nxt = [0] * (w * h)
        for y in range(h):
            for x in range(w):
                t = 0
                t2 = 0
                for i in range(9):
                    yy = min(max(y + i // 3 - 1, 0), h - 1)
                    xx = min(max(x + i % 3 - 1, 0), w - 1)
                    t += kernel[i] * cur[yy * w + xx]
                    if kernel2:
                        t2 += kernel2[i] * cur[yy * w + xx]
                nxt[y * w + x] = finish(t, t2, kernel2, use_abs, div, bias, maxval)
        cur = nxt
    return cur


def reference_at(name, w, h, get, x, y, maxval):
    """One output pixel of a SINGLE pass; get(index) -> sample value."""
    kernel, div, bias, use_abs, _, *rest = FILTERS[name]
    kernel2 = rest[0] if rest else None
    t = t2 = 0
    for i in range(9):
        yy = min(max(y + i // 3 - 1, 0), h - 1)
        xx = min(max(x + i % 3 - 1, 0), w - 1)
        v = get(yy * w + xx)
        t += kernel[i] * v
        if kernel2:
            t2 += kernel2[i] * v
    return finish(t, t2, kernel2, use_abs, div, bias, maxval)


def encode(px, maxval=255):
    """Samples -> file bytes: 1 byte each if maxval < 256 else 2, big-endian."""
    if maxval < 256:
        return bytes(px)
    out = bytearray()
    for v in px:
        out += bytes((v >> 8, v & 255))
    return bytes(out)


def run(input_bytes, args=()):
    """Run the program on input_bytes (None = no input file). Returns
    (exit_code, stderr_text, output_bytes_or_None)."""
    with tempfile.TemporaryDirectory() as d:
        os.mkdir(os.path.join(d, "images"))
        if input_bytes is not None:
            with open(os.path.join(d, "images", "input.pgm"), "wb") as f:
                f.write(input_bytes)
        r = subprocess.run([QEMU, BIN, *args], cwd=d, capture_output=True, timeout=600)
        out = os.path.join(d, "images", "output.pgm")
        data = open(out, "rb").read() if os.path.exists(out) else None
        return r.returncode, r.stderr.decode(errors="replace"), data


def pgm(w, h, px, header=None, maxval=255):
    hdr = header if header is not None else f"P5\n{w} {h}\n{maxval}\n".encode()
    return hdr + encode(px, maxval)


def rand_px(n, seed, maxval=255):
    rng = random.Random(seed)
    return [rng.randrange(maxval + 1) for _ in range(n)]


failures = 0


def check(name, ok, detail=""):
    global failures
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        failures += 1


def blur_case(name, w, h, px, header=None, filt="blur", passes=None, args=None, maxval=255):
    if args is None:
        args = [filt] + ([] if passes is None else [str(passes)])
    code, err, out = run(pgm(w, h, px, header, maxval), args)
    want = f"P5\n{w} {h}\n{maxval}\n".encode() + encode(reference(filt, w, h, px, passes, maxval), maxval)
    check(name, code == 0 and out == want,
          f"exit={code} {err.strip()}" if code else "output mismatch")


def sample_points(w, h, rng, n=1500):
    """Corners, points along all four edges, and random interior points."""
    pts = {(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)}
    for _ in range(n // 5):
        pts.add((rng.randrange(w), 0))
        pts.add((rng.randrange(w), h - 1))
        pts.add((0, rng.randrange(h)))
        pts.add((w - 1, rng.randrange(h)))
    for _ in range(n):
        pts.add((rng.randrange(w), rng.randrange(h)))
    return sorted(pts)


def big_case(name, w, h, filt, maxval, seed):
    """Large image, single pass, checked at sampled points against the
    reference. Raw random bytes are valid samples for maxval 255 or 65535."""
    assert maxval in (255, 65535)
    rng = random.Random(seed)
    n = w * h
    raw = rng.randbytes(n if maxval == 255 else 2 * n)
    if maxval == 255:
        get = raw.__getitem__
    else:
        get = lambda i: (raw[2 * i] << 8) | raw[2 * i + 1]
    code, err, out = run(f"P5\n{w} {h}\n{maxval}\n".encode() + raw, [filt, "1"])
    hdr = f"P5\n{w} {h}\n{maxval}\n".encode()
    if code != 0 or out is None or not out.startswith(hdr) or len(out) != len(hdr) + len(raw):
        check(name, False, f"exit={code} {err.strip()}")
        return
    body = out[len(hdr):]
    if maxval == 255:
        got = body.__getitem__
    else:
        got = lambda i: (body[2 * i] << 8) | body[2 * i + 1]
    bad = [(x, y) for (x, y) in sample_points(w, h, rng)
           if got(y * w + x) != reference_at(filt, w, h, get, x, y, maxval)]
    check(name, not bad, f"{len(bad)} sampled pixels differ, first at {bad[:3]}")


def error_case(name, input_bytes, expect_msg, args=()):
    code, err, out = run(input_bytes, args)
    check(name, code == 1 and expect_msg in err and out is None,
          f"exit={code} stderr={err.strip()!r} output={'present' if out is not None else 'none'}")


def main():
    if not os.path.exists(BIN):
        sys.exit("build/filter not found - run `make` first")

    # --- correctness vs reference ---
    cb = [235 if ((x // 16) + (y // 16)) % 2 == 0 else 20
          for y in range(64) for x in range(64)]
    blur_case("checkerboard 64x64", 64, 64, cb)
    blur_case("random 37x19 (non-square)", 37, 19, rand_px(37 * 19, 1))
    blur_case("1x1", 1, 1, [200])
    blur_case("1x17 (single column)", 1, 17, rand_px(17, 2))
    blur_case("23x1 (single row)", 23, 1, rand_px(23, 3))
    blur_case("2x2", 2, 2, rand_px(4, 4))
    blur_case("all 255 stays 255", 8, 8, [255] * 64)
    blur_case("256x256", 256, 256, rand_px(65536, 5))

    # --- every filter vs reference (default passes), on two image shapes ---
    cb2 = [235 if ((x // 4) + (y // 4)) % 2 == 0 else 20
           for y in range(24) for x in range(24)]
    rnd = rand_px(29 * 17, 8)
    for name in FILTERS:
        blur_case(f"filter {name}: checkerboard 24x24", 24, 24, cb2, filt=name)
        blur_case(f"filter {name}: random 29x17", 29, 17, rnd, filt=name)

    # --- 16-bit and other bit depths: every filter vs reference ---
    for maxval in (65535, 4095):
        rnd16 = rand_px(29 * 17, 20 + maxval % 7, maxval)
        for name in FILTERS:
            blur_case(f"{maxval}-max: filter {name}, random 29x17", 29, 17, rnd16,
                      filt=name, maxval=maxval)
    blur_case("maxval 1 (binary image)", 13, 9, rand_px(13 * 9, 21, 1), maxval=1)
    blur_case("maxval 255 (1 byte/sample boundary)", 9, 9, rand_px(81, 22, 255), maxval=255)
    blur_case("maxval 256 (2 bytes/sample boundary)", 9, 9, rand_px(81, 23, 256), maxval=256)
    blur_case("maxval 1000 (odd depth)", 11, 7, rand_px(77, 24, 1000), maxval=1000, filt="sharpen")
    px16 = rand_px(15 * 15, 25, 65535)
    code, err, out = run(pgm(15, 15, px16, maxval=65535), ["identity"])
    check("16-bit identity output == input", code == 0 and out == pgm(15, 15, px16, maxval=65535))
    flat16 = pgm(10, 10, [12345] * 100, maxval=65535)
    code, err, out = run(flat16, ["emboss"])
    check("16-bit emboss on flat image is mid-gray 32768",
          code == 0 and out == pgm(10, 10, [32768] * 100, maxval=65535))
    code, err, out = run(pgm(10, 10, [1000] * 100, maxval=4095), ["emboss"])
    check("12-bit emboss on flat image is mid-gray 2048",
          code == 0 and out == pgm(10, 10, [2048] * 100, maxval=4095))
    step16 = [0] * 5 + [65535] * 5
    code, err, out = run(pgm(10, 10, step16 * 10, maxval=65535), ["sobelx"])
    check("16-bit sobelx on a full-range step reaches 65535 exactly",
          code == 0 and out is not None and 65535 in [(out[i] << 8) | out[i + 1]
                                                       for i in range(len(out) - 200, len(out), 2)])

    # --- extreme aspect ratios (rows/cols clamp every pixel), full reference ---
    for (w, h) in ((65536, 1), (1, 65536), (4096, 16), (16, 4096)):
        for name in ("blur", "sobely"):
            blur_case(f"{name} {w}x{h}", w, h, rand_px(w * h, 15), filt=name, passes=2)

    # --- large images up to the pixel limit, sampled reference ---
    # (SKIP_BIG=1 skips the 4096x4096 and 16M-pixel cases for quick runs)
    big_case("blur 1024x1024 (8-bit)", 1024, 1024, "blur", 255, 30)
    big_case("sobel 2048x1536 (16-bit)", 2048, 1536, "sobel", 65535, 31)
    if not os.environ.get("SKIP_BIG"):
        big_case("sharpen 4096x4096 (8-bit, the pixel limit)", 4096, 4096, "sharpen", 255, 32)
        big_case("gaussian 4096x4096 (16-bit, the pixel limit)", 4096, 4096, "gaussian", 65535, 33)
        big_case("blur 16777216x1 (one huge row)", 16777216, 1, "blur", 255, 34)
        big_case("edge 1x16777216 (one huge column)", 1, 16777216, "edge", 255, 35)

    # --- pass count argument ---
    blur_case("blur with 1 pass", 20, 11, rand_px(220, 9), filt="blur", passes=1)
    blur_case("blur with 3 passes", 20, 11, rand_px(220, 9), filt="blur", passes=3)
    blur_case("sharpen with 3 passes", 20, 11, rand_px(220, 10), filt="sharpen", passes=3)
    blur_case("16-bit blur with 3 passes", 20, 11, rand_px(220, 26, 65535), filt="blur",
              passes=3, maxval=65535)
    blur_case(f"blur with max passes ({MAX_PASSES}) runs", 4, 4, rand_px(16, 11),
              filt="blur", passes=MAX_PASSES)

    # --- sanity properties ---
    px = rand_px(15 * 15, 12)
    code, err, out = run(pgm(15, 15, px), ["identity"])
    check("identity output == input", code == 0 and out == pgm(15, 15, px))
    code, err, out = run(pgm(15, 15, px), ["blur", "0"])
    check("0 passes output == input", code == 0 and out == pgm(15, 15, px))
    code, err, out = run(pgm(15, 15, px), [])
    code2, err2, out2 = run(pgm(15, 15, px), ["blur"])
    check("no args == blur", code == 0 and out == out2 and out is not None)
    flat = pgm(10, 10, [90] * 100)
    code, err, out = run(flat, ["edge"])
    check("edge on flat image is black", code == 0 and out == pgm(10, 10, [0] * 100))
    code, err, out = run(flat, ["emboss"])
    check("emboss on flat image is neutral gray 128 (zero-sum kernel)",
          code == 0 and out == pgm(10, 10, [128] * 100))
    for name in ("sobel", "sobelx", "sobely"):
        code, err, out = run(flat, [name])
        check(f"{name} on flat image is black", code == 0 and out == pgm(10, 10, [0] * 100))

    # edge uses |sum|: a lone bright pixel lights up its neighbors too
    dot = [0] * 25
    dot[12] = 100                                           # 5x5, center pixel
    want = [0] * 25
    for r in range(1, 4):
        for c in range(1, 4):
            want[r * 5 + c] = 100
    want[12] = 255                                          # 8*100 clamps
    code, err, out = run(pgm(5, 5, dot), ["edge"])
    check("edge shows both sides: lone pixel + its 8 neighbors",
          code == 0 and out == pgm(5, 5, want))

    # sobel magnitude must equal the single-direction filter when the other
    # gradient is zero (independent of the reference implementation)
    vstep = ([0] * 5 + [200] * 5) * 10                       # 10x10, vertical edge
    hstep = [0] * 50 + [200] * 50                            # 10x10, horizontal edge
    for img, single in ((vstep, "sobelx"), (hstep, "sobely")):
        _, _, o_mag = run(pgm(10, 10, img), ["sobel"])
        _, _, o_one = run(pgm(10, 10, img), [single])
        check(f"sobel == {single} when the other gradient is 0",
              o_mag is not None and o_mag == o_one)
    diag = [255 if x + y >= 10 else 0 for y in range(10) for x in range(10)]
    _, _, o_mag = run(pgm(10, 10, diag), ["sobel"])
    _, _, o_x = run(pgm(10, 10, diag), ["sobelx"])
    _, _, o_y = run(pgm(10, 10, diag), ["sobely"])
    check("sobel is stronger than either direction alone on a diagonal edge",
          o_mag is not None and o_mag[-100:] != o_x[-100:] and
          all(m >= max(a, b) for m, a, b in zip(o_mag[-100:], o_x[-100:], o_y[-100:])))

    # --- rounding: blur must not steadily darken the image ---
    w = h = 40
    rpx = rand_px(w * h, 14)
    code, err, out = run(pgm(w, h, rpx), ["blur", "32"])
    mean_in = sum(rpx) / len(rpx)
    mean_out = sum(out[-w * h:]) / (w * h) if out else -1
    check("blur 32 passes preserves mean brightness (within 1.5)",
          code == 0 and abs(mean_out - mean_in) < 1.5,
          f"in={mean_in:.2f} out={mean_out:.2f}")
    rpx16 = rand_px(w * h, 27, 65535)
    code, err, out = run(pgm(w, h, rpx16, maxval=65535), ["blur", "32"])
    mean_in = sum(rpx16) / len(rpx16)
    vals = [(out[i] << 8) | out[i + 1] for i in range(len(out) - 2 * w * h, len(out), 2)] if out else [-1]
    check("16-bit blur 32 passes preserves mean brightness (within 1.5/65536 of range ~ 400)",
          code == 0 and abs(sum(vals) / len(vals) - mean_in) < 400,
          f"in={mean_in:.1f} out={sum(vals) / len(vals):.1f}")
    # exact tie cases: /16 and /4 can produce x.5 values
    step = [0] * 3 + [10] * 3
    blur_case("sobelx rounding on step edge", 6, 6, step * 6, filt="sobelx")
    blur_case("gaussian rounding on step edge", 6, 6, step * 6, filt="gaussian")

    # --- header variants (same pixels, different header layout) ---
    px = rand_px(12 * 9, 6)
    blur_case("header: CRLF after P5", 12, 9, px, b"P5\r\n12 9\n255\n")
    blur_case("header: CRLF between fields", 12, 9, px, b"P5\r\n12\r\n9\r\n255\n")
    blur_case("header: comment right after P5", 12, 9, px, b"P5# note\n12 9\n255\n")
    blur_case("header: comment lines + odd spacing", 12, 9, px,
              b"P5\n# one\n#two\n  12\t 9 \n# three\n255\n")
    blur_case("header: all on one line", 12, 9, px, b"P5 12 9 255\n")
    px100 = rand_px(12 * 9, 6, 100)
    blur_case("header: maxval below 255 accepted", 12, 9, px100, b"P5\n12 9\n100\n", maxval=100)

    # --- argument errors: exit 1, usage on stderr, no output file ---
    ok = pgm(4, 4, rand_px(16, 13))
    error_case("unknown filter", ok, "usage", ["nosuchfilter"])
    error_case("filter name is case-sensitive", ok, "usage", ["Blur"])
    error_case("filter name prefix rejected", ok, "usage", ["blu"])
    error_case("passes not a number", ok, "usage", ["blur", "abc"])
    error_case("passes negative", ok, "usage", ["blur", "-1"])
    error_case("passes empty string", ok, "usage", ["blur", ""])
    error_case("passes too large", ok, "usage", ["blur", str(MAX_PASSES + 1)])
    error_case("passes huge (overflow guard)", ok, "usage", ["blur", "99999999999999999999"])

    # --- error handling: exit 1, message on stderr, no output file ---
    good = pgm(4, 4, rand_px(16, 7))
    error_case("missing input file", None, "cannot open")
    error_case("empty file", b"", "header")
    error_case("bad magic P6", b"P6\n4 4\n255\n" + bytes(48), "header")
    error_case("bad magic P2", b"P2\n4 4\n255\n" + bytes(16), "header")
    error_case("garbage", b"hello world, not an image", "header")
    error_case("header cut mid-number", b"P5\n25", "header")
    error_case("pixel data truncated", good[:-5], "truncated")
    error_case("no pixel data at all", b"P5\n4 4\n255\n", "truncated")
    error_case("16-bit data cut mid-sample", b"P5\n4 4\n65535\n" + bytes(31), "truncated")
    error_case("zero width", b"P5\n0 4\n255\n", "non-zero")
    error_case("zero height", b"P5\n4 0\n255\n", "non-zero")
    error_case("maxval 0", b"P5\n4 4\n0\n" + bytes(16), "maxval")
    error_case("maxval 65536 (beyond PGM)", b"P5\n4 4\n65536\n" + bytes(32), "maxval")
    error_case("8-bit sample above maxval", b"P5\n2 2\n100\n" + bytes([1, 2, 101, 4]), "exceeds maxval")
    error_case("16-bit sample above maxval", b"P5\n2 2\n1000\n" + encode([1, 2, 1001, 4], 1000), "exceeds maxval")
    side = int(MAX_PIXELS ** 0.5)
    error_case("over the pixel limit", f"P5\n{side + 1} {side}\n255\n".encode() + bytes((side + 1) * side), "too large")
    error_case("W*H overflow wrap", b"P5\n4294967296 4294967296\n255\n", "too large")

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    sys.exit(1 if failures else 0)


main()
