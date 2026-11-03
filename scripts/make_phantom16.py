#!/usr/bin/env python3
# Generates a synthetic 16-bit "CT-like" test image (P5 PGM, maxval 4095, two
# bytes per sample): a body outline of soft tissue with two lungs, a spine, a
# few ribs and a small bright lesion, plus Gaussian noise.
#
# Stored values are Hounsfield units + 1024 (the usual 12-bit CT offset), so:
#     air ~ 24   lung ~ 224   fat ~ 924   soft tissue ~ 1064   bone ~ 1700+
# The tissues all fall within a few hundred counts of each other out of 4095,
# so on a linear 0..255 display the soft-tissue detail is nearly invisible
# until you apply a window, e.g.:
#     build/filter identity window 1064 400     (soft-tissue window)
#     build/filter identity window 224 1400     (lung window)
#     build/filter identity window 1700 2000    (bone window)
#
# usage: make_phantom16.py [OUTPUT [SIZE]]   (default images/phantom16.pgm, 512;
# a SIZE such as 256 draws the same anatomy at a smaller scale, which is what
# fits in the Pico's memory)
import math
import random
import sys

N = int(sys.argv[2]) if len(sys.argv) > 2 else 512
SCALE = 512 / N                                       # geometry is defined on a 512 grid
MAXVAL = 4095
rng = random.Random(7)

def ellipse(x, y, cx, cy, rx, ry):
    return ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 <= 1.0

def circle(x, y, cx, cy, r):
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r

pix = []
for py in range(N):
    for px in range(N):
        x, y = px * SCALE, py * SCALE
        v = 24                                        # air
        if ellipse(x, y, 256, 262, 215, 160):         # body: soft tissue
            v = 1064
            if ellipse(x, y, 256, 262, 205, 150) and not ellipse(x, y, 256, 262, 190, 136):
                v = 924                               # subcutaneous fat layer
            if ellipse(x, y, 175, 255, 78, 105) or ellipse(x, y, 337, 255, 78, 105):
                v = 224                               # lungs
                if circle(x, y, 150, 240, 5):         # a small nodule, brighter than lung
                    v = 1000
            if circle(x, y, 256, 345, 24):            # vertebral body
                v = 1750
            if circle(x, y, 256, 345, 10):
                v = 1200                              # canal
            for ang in range(20, 160, 14):            # ribs: bright arcs near the wall
                a = math.radians(ang)
                for side in (-1, 1):
                    rx = 256 + side * 188 * math.cos(a) * 0.98
                    ry = 262 + 140 * math.sin(a) * 0.98
                    if circle(x, y, rx, ry, 6):
                        v = 1650
            if circle(x, y, 268, 215, 14):            # subtle soft-tissue lesion
                v = 1100
        v += round(rng.gauss(0, 14))                  # scanner noise
        pix.append(min(max(v, 0), MAXVAL))

path = sys.argv[1] if len(sys.argv) > 1 else "images/phantom16.pgm"
with open(path, "wb") as f:
    f.write(f"P5\n{N} {N}\n{MAXVAL}\n".encode("ascii"))
    for v in pix:
        f.write(bytes((v >> 8, v & 255)))
print(f"wrote {path}: {N}x{N}, 16-bit samples, maxval {MAXVAL}")
