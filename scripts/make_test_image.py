#!/usr/bin/env python3
# Generates a grayscale PGM (P5, raw) checkerboard test image. A checkerboard
# makes repeated-pass blurring visible at any pass count, since softening
# happens at every cell boundary, not just a single shape's edge (a lone
# square only shows blur near its edges, easy to miss at 256x256).
import sys

W, H = 256, 256
CELL = 16
BG, FG = 20, 235

pixels = bytearray(W * H)
for y in range(H):
    for x in range(W):
        on = ((x // CELL) + (y // CELL)) % 2 == 0
        pixels[y * W + x] = FG if on else BG

path = sys.argv[1] if len(sys.argv) > 1 else "images/input.pgm"
with open(path, "wb") as f:
    f.write(f"P5\n{W} {H}\n255\n".encode("ascii"))
    f.write(pixels)

print(f"wrote {path}: {W}x{H}, {CELL}px checkerboard cells")
