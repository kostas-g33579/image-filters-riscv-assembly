#!/usr/bin/env python3
"""Regression tests for blur.s.

Runs build/blur under qemu-riscv64 inside a throwaway temp directory (the
program uses cwd-relative images/input.pgm and images/output.pgm), so the
project's own images/ folder is never touched. Compares the output against
an independent Python reference: 3x3 box blur, integer divide by 9,
clamp-to-edge, repeated PASS_COUNT times.

Usage: python3 tests/run_tests.py     (or: make test)
"""
import os
import random
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "build", "blur")
QEMU = os.environ.get("QEMU", "qemu-riscv64")

with open(os.path.join(ROOT, "src", "blur.s")) as f:
    src = f.read()
PASSES = int(re.search(r"^\.equ PASS_COUNT,\s*(\d+)", src, re.M).group(1))
MAX_PIXELS = int(re.search(r"^\.equ MAX_PIXELS,\s*(\d+)", src, re.M).group(1))


def reference(w, h, px, passes=PASSES):
    cur = list(px)
    for _ in range(passes):
        nxt = [0] * (w * h)
        for y in range(h):
            for x in range(w):
                t = 0
                for dy in (-1, 0, 1):
                    yy = min(max(y + dy, 0), h - 1)
                    for dx in (-1, 0, 1):
                        xx = min(max(x + dx, 0), w - 1)
                        t += cur[yy * w + xx]
                nxt[y * w + x] = t // 9
        cur = nxt
    return bytes(cur)


def run(input_bytes):
    """Run the program on input_bytes (None = no input file). Returns
    (exit_code, stderr_text, output_bytes_or_None)."""
    with tempfile.TemporaryDirectory() as d:
        os.mkdir(os.path.join(d, "images"))
        if input_bytes is not None:
            with open(os.path.join(d, "images", "input.pgm"), "wb") as f:
                f.write(input_bytes)
        r = subprocess.run([QEMU, BIN], cwd=d, capture_output=True, timeout=300)
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


def blur_case(name, w, h, px, header=None):
    code, err, out = run(pgm(w, h, px, header))
    want = f"P5\n{w} {h}\n255\n".encode() + reference(w, h, px)
    check(name, code == 0 and out == want,
          f"exit={code} {err.strip()}" if code else "output mismatch")


def error_case(name, input_bytes, expect_msg):
    code, err, out = run(input_bytes)
    check(name, code == 1 and expect_msg in err and out is None,
          f"exit={code} stderr={err.strip()!r} output={'present' if out is not None else 'none'}")


def main():
    if not os.path.exists(BIN):
        sys.exit("build/blur not found - run `make` first")

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

    # --- header variants (same pixels, different header layout) ---
    px = rand_px(12 * 9, 6)
    blur_case("header: CRLF after P5", 12, 9, px, b"P5\r\n12 9\n255\n")
    blur_case("header: CRLF between fields", 12, 9, px, b"P5\r\n12\r\n9\r\n255\n")
    blur_case("header: comment right after P5", 12, 9, px, b"P5# note\n12 9\n255\n")
    blur_case("header: comment lines + odd spacing", 12, 9, px,
              b"P5\n# one\n#two\n  12\t 9 \n# three\n255\n")
    blur_case("header: all on one line", 12, 9, px, b"P5 12 9 255\n")
    blur_case("header: maxval below 255 accepted", 12, 9, px, b"P5\n12 9\n100\n")

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
