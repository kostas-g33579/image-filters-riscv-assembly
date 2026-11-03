#!/usr/bin/env python3
"""Regression tests for filter.s.

Runs build/filter under qemu-riscv64 inside a throwaway temp directory (the
program uses cwd-relative images/input.pgm and images/output.pgm), so the
project's own images/ folder is never touched. Compares the output against
an independent Python reference of the kernel convolution: clamp-to-edge
taps, weighted sum, optional abs, round-to-nearest divide (halves away from
zero), bias, clamp to 0..255,
repeated for the pass count.

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

# name: (kernel row-major, divisor, bias, abs, default passes)
FILTERS = {
    "identity": ([0, 0, 0, 0, 1, 0, 0, 0, 0], 1, 0, False, 1),
    "blur":     ([1, 1, 1, 1, 1, 1, 1, 1, 1], 9, 0, False, 8),
    "gaussian": ([1, 2, 1, 2, 4, 2, 1, 2, 1], 16, 0, False, 4),
    "sharpen":  ([0, -1, 0, -1, 5, -1, 0, -1, 0], 1, 0, False, 1),
    "edge":     ([-1, -1, -1, -1, 8, -1, -1, -1, -1], 1, 0, False, 1),
    "emboss":   ([-2, -1, 0, -1, 1, 1, 0, 1, 2], 1, 128, False, 1),
    "sobelx":   ([-1, 0, 1, -2, 0, 2, -1, 0, 1], 4, 0, True, 1),
    "sobely":   ([-1, -2, -1, 0, 0, 0, 1, 2, 1], 4, 0, True, 1),
}


def reference(name, w, h, px, passes=None):
    kernel, div, bias, use_abs, default_passes = FILTERS[name]
    if passes is None:
        passes = default_passes
    cur = list(px)
    for _ in range(passes):
        nxt = [0] * (w * h)
        for y in range(h):
            for x in range(w):
                t = 0
                for i in range(9):
                    yy = min(max(y + i // 3 - 1, 0), h - 1)
                    xx = min(max(x + i % 3 - 1, 0), w - 1)
                    t += kernel[i] * cur[yy * w + xx]
                if use_abs:
                    t = abs(t)
                q = (abs(t) + div // 2) // div   # round half away from zero
                t = q if t >= 0 else -q
                nxt[y * w + x] = min(max(t + bias, 0), 255)
        cur = nxt
    return bytes(cur)


def run(input_bytes, args=()):
    """Run the program on input_bytes (None = no input file). Returns
    (exit_code, stderr_text, output_bytes_or_None)."""
    with tempfile.TemporaryDirectory() as d:
        os.mkdir(os.path.join(d, "images"))
        if input_bytes is not None:
            with open(os.path.join(d, "images", "input.pgm"), "wb") as f:
                f.write(input_bytes)
        r = subprocess.run([QEMU, BIN, *args], cwd=d, capture_output=True, timeout=300)
        out = os.path.join(d, "images", "output.pgm")
        data = open(out, "rb").read() if os.path.exists(out) else None
        return r.returncode, r.stderr.decode(errors="replace"), data


def pgm(w, h, px, header=None):
    hdr = header if header is not None else f"P5\n{w} {h}\n255\n".encode()
    return hdr + bytes(px)


def rand_px(n, seed):
    rng = random.Random(seed)
    return bytes(rng.randrange(256) for _ in range(n))


failures = 0


def check(name, ok, detail=""):
    global failures
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        failures += 1


def blur_case(name, w, h, px, header=None, filt="blur", passes=None, args=None):
    if args is None:
        args = [filt] + ([] if passes is None else [str(passes)])
    code, err, out = run(pgm(w, h, px, header), args)
    want = f"P5\n{w} {h}\n255\n".encode() + reference(filt, w, h, px, passes)
    check(name, code == 0 and out == want,
          f"exit={code} {err.strip()}" if code else "output mismatch")


def error_case(name, input_bytes, expect_msg, args=()):
    code, err, out = run(input_bytes, args)
    check(name, code == 1 and expect_msg in err and out is None,
          f"exit={code} stderr={err.strip()!r} output={'present' if out is not None else 'none'}")


def main():
    if not os.path.exists(BIN):
        sys.exit("build/filter not found - run `make` first")

    # --- correctness vs reference ---
    cb = bytes(235 if ((x // 16) + (y // 16)) % 2 == 0 else 20
               for y in range(64) for x in range(64))
    blur_case("checkerboard 64x64", 64, 64, cb)
    blur_case("random 37x19 (non-square)", 37, 19, rand_px(37 * 19, 1))
    blur_case("1x1", 1, 1, bytes([200]))
    blur_case("1x17 (single column)", 1, 17, rand_px(17, 2))
    blur_case("23x1 (single row)", 23, 1, rand_px(23, 3))
    blur_case("2x2", 2, 2, rand_px(4, 4))
    blur_case("all 255 stays 255", 8, 8, bytes([255]) * 64)
    blur_case("max size 256x256", 256, 256, rand_px(65536, 5))

    # --- every filter vs reference (default passes), on two image shapes ---
    cb2 = bytes(235 if ((x // 4) + (y // 4)) % 2 == 0 else 20
                for y in range(24) for x in range(24))
    rnd = rand_px(29 * 17, 8)
    for name in FILTERS:
        blur_case(f"filter {name}: checkerboard 24x24", 24, 24, cb2, filt=name)
        blur_case(f"filter {name}: random 29x17", 29, 17, rnd, filt=name)

    # --- pass count argument ---
    blur_case("blur with 1 pass", 20, 11, rand_px(220, 9), filt="blur", passes=1)
    blur_case("blur with 3 passes", 20, 11, rand_px(220, 9), filt="blur", passes=3)
    blur_case("sharpen with 3 passes", 20, 11, rand_px(220, 10), filt="sharpen", passes=3)
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
    flat = pgm(10, 10, bytes([90]) * 100)
    code, err, out = run(flat, ["edge"])
    check("edge on flat image is black", code == 0 and out == pgm(10, 10, bytes(100)))
    code, err, out = run(flat, ["emboss"])
    check("emboss on flat image is value + 128 (kernel sums to 1)",
          code == 0 and out == pgm(10, 10, bytes([218] * 100)))

    # --- rounding: blur must not steadily darken the image ---
    w = h = 40
    rpx = rand_px(w * h, 14)
    code, err, out = run(pgm(w, h, rpx), ["blur", "32"])
    mean_in = sum(rpx) / len(rpx)
    mean_out = sum(out[-w * h:]) / (w * h) if out else -1
    check("blur 32 passes preserves mean brightness (within 1.5)",
          code == 0 and abs(mean_out - mean_in) < 1.5,
          f"in={mean_in:.2f} out={mean_out:.2f}")
    # exact tie cases: 9 pixels summing to x.5 can't occur for /9, but /16
    # and /4 can: check a hand-computed value for sobelx and gaussian
    step = bytes([0] * 3 + [10] * 3) * 6          # 6x6, vertical step edge
    blur_case("sobelx rounding on step edge", 6, 6, step, filt="sobelx")
    blur_case("gaussian rounding on step edge", 6, 6, step, filt="gaussian")

    # --- header variants (same pixels, different header layout) ---
    px = rand_px(12 * 9, 6)
    blur_case("header: CRLF after P5", 12, 9, px, b"P5\r\n12 9\n255\n")
    blur_case("header: CRLF between fields", 12, 9, px, b"P5\r\n12\r\n9\r\n255\n")
    blur_case("header: comment right after P5", 12, 9, px, b"P5# note\n12 9\n255\n")
    blur_case("header: comment lines + odd spacing", 12, 9, px,
              b"P5\n# one\n#two\n  12\t 9 \n# three\n255\n")
    blur_case("header: all on one line", 12, 9, px, b"P5 12 9 255\n")
    blur_case("header: maxval below 255 accepted", 12, 9, px, b"P5\n12 9\n100\n")

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
    error_case("zero width", b"P5\n0 4\n255\n", "non-zero")
    error_case("zero height", b"P5\n4 0\n255\n", "non-zero")
    error_case("maxval 0", b"P5\n4 4\n0\n" + bytes(16), "maxval")
    error_case("maxval 65535 (16-bit)", b"P5\n4 4\n65535\n" + bytes(32), "maxval")
    side = int(MAX_PIXELS ** 0.5)
    error_case("over MAX_PIXELS", f"P5\n{side + 1} {side}\n255\n".encode() + bytes((side + 1) * side), "MAX_PIXELS")
    error_case("W*H overflow wrap", b"P5\n4294967296 4294967296\n255\n", "MAX_PIXELS")

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    sys.exit(1 if failures else 0)


main()
