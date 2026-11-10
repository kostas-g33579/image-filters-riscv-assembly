#!/usr/bin/env python3
"""Write src/gauss_fast.inc: the straight-line code for the separable pass at radius 1 to 4 (src/gauss.s includes it).

    python3 scripts/gen_gauss_fast.py            # (re)writes the file
    python3 scripts/gen_gauss_fast.py --check    # exits 1 if the committed file differs from what this would write
    python3 scripts/gen_gauss_fast.py --counts   # instruction counts of the loops (for the cycle predictions)

Why generated: four radii times two passes of near-identical code, where one wrong register in a hand copy would be
the kind of bug that survives review. The generic loops in gauss.s (any radius 0..15, weights read from memory every tap)
stay as they are and remain the reference for the fast ones: both give bit-identical results (tests/test_gauss.py).

What the fast code does differently: the radius+1 weights live in registers, so a tap pair costs `lhu lhu add mul add`
(5 instructions) instead of 11 (two loads, the weight load, three pointer updates and the loop branch), and all the loads
of a pixel are issued before any of them is used, so no load is followed by its consumer.

Horizontal pass (joined to the generic loop, which keeps doing the clamped pixels near both ends of the row): entered at
the first interior pixel, it does one pixel per iteration up to x = W - r and then jumps back to the generic loop.
Vertical pass: its own routine; the 2r+1 row pointers live in registers, four output samples per iteration (the row
pointers advance once per four samples), a loop of single samples for the remaining 0 to 3.
"""
import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "src", "gauss_fast.inc")
RADII = (1, 2, 3, 4)

# ---- horizontal: registers (see gauss.s: t0 = x, t5 = w[0] for the generic loop, t6 = &src[x], a1 = &dst[x], a3 = r,
# a4 = weights, a6 = W - r, a7 = 8192, s3 = W - 1 must survive)
H_W = ["s4", "s5", "s6", "s7", "s8"]                       # w[0..4]
H_C = "s9"                                                  # the centre sample
H_PAIRS = [("s10", "s11"), ("t1", "t2"), ("t3", "t4"), ("s0", "s1")]
H_ACC = "s2"

# ---- vertical
V_RP = ["s0", "s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8"]   # rows[0..8]
V_W = ["s9", "s10", "s11", "a5", "a6"]                      # w[0..4]
V_C, V_ACC = "t0", "t1"
V_PAIRS = [("t2", "t3"), ("t4", "t5"), ("t6", "a0"), ("a3", "a4")]


def h_body(R, lines):
    c, acc = H_C, H_ACC
    lines.append("    lhu %s, 0(t6)" % c)
    for k in range(1, R + 1):
        lo, hi = H_PAIRS[k - 1]
        lines.append("    lhu %s, %d(t6)" % (lo, -2 * k))
        lines.append("    lhu %s, %d(t6)" % (hi, 2 * k))
    lines.append("    mul %s, %s, %s" % (acc, c, H_W[0]))
    for k in range(1, R + 1):
        lo, hi = H_PAIRS[k - 1]
        lines.append("    add %s, %s, %s" % (lo, lo, hi))
        lines.append("    mul %s, %s, %s" % (lo, lo, H_W[k]))
        lines.append("    add %s, %s, %s" % (acc, acc, lo))
    lines.append("    add %s, %s, a7" % (acc, acc))
    lines.append("    srli %s, %s, 14" % (acc, acc))
    lines.append("    sh %s, 0(a1)" % acc)


def h_block(R):
    L = ["", "# ---- horizontal pass, radius %d: weights in s4..s%d; one pixel per iteration from x = r to x = W - r - 1" % (R, 4 + R),
         ".Lh_f%d:" % R]
    for k in range(R + 1):
        L.append("    lw %s, %d(a4)" % (H_W[k], 4 * k))
    L.append(".Lh_f%d_loop:" % R)
    h_body(R, L)
    L += ["    addi a1, a1, 2", "    addi t6, t6, 2", "    addi t0, t0, 1", "    blt t0, a6, .Lh_f%d_loop" % R, "    j .Lh_pixel"]
    return L


def v_body(R, off, L):
    rp = V_RP
    L.append("    lhu %s, %d(%s)" % (V_C, off, rp[R]))
    for k in range(1, R + 1):
        a, b = V_PAIRS[k - 1]
        L.append("    lhu %s, %d(%s)" % (a, off, rp[R - k]))
        L.append("    lhu %s, %d(%s)" % (b, off, rp[R + k]))
    L.append("    mul %s, %s, %s" % (V_ACC, V_C, V_W[0]))
    for k in range(1, R + 1):
        a, b = V_PAIRS[k - 1]
        L.append("    add %s, %s, %s" % (a, a, b))
        L.append("    mul %s, %s, %s" % (a, a, V_W[k]))
        L.append("    add %s, %s, %s" % (V_ACC, V_ACC, a))
    L.append("    add %s, %s, a7" % (V_ACC, V_ACC))
    L.append("    srli %s, %s, 14" % (V_ACC, V_ACC))
    L.append("    sh %s, %d(a1)" % (V_ACC, off))


def v_block(R):
    L = ["", "# ---- vertical pass, radius %d: row pointers in s0..s%d, weights in s9, s10, s11, a5, a6; four samples per iteration" % (R, 2 * R),
         ".Lv_f%d:" % R]
    for m in range(2 * R + 1):
        L.append("    REG_L %s, %d*PTRSIZE, a0" % (V_RP[m], m))
    for k in range(R + 1):
        L.append("    lw %s, %d(a4)" % (V_W[k], 4 * k))
    L.append("    li a7, 8192")
    L.append("    addi a2, a2, -4")
    L.append("    bltz a2, .Lv_f%d_rest" % R)
    L.append(".Lv_f%d_quad:" % R)
    for off in (0, 2, 4, 6):
        v_body(R, off, L)
    for m in range(2 * R + 1):
        L.append("    addi %s, %s, 8" % (V_RP[m], V_RP[m]))
    L += ["    addi a1, a1, 8", "    addi a2, a2, -4", "    bgez a2, .Lv_f%d_quad" % R]
    L.append(".Lv_f%d_rest:" % R)
    L += ["    addi a2, a2, 4", "    beqz a2, .Lv_fast_exit", ".Lv_f%d_one:" % R]
    v_body(R, 0, L)
    for m in range(2 * R + 1):
        L.append("    addi %s, %s, 2" % (V_RP[m], V_RP[m]))
    L += ["    addi a1, a1, 2", "    addi a2, a2, -1", "    bnez a2, .Lv_f%d_one" % R, "    j .Lv_fast_exit"]
    return L


def text():
    L = ["# GENERATED by scripts/gen_gauss_fast.py - do not edit. Straight-line separable pass for radius 1 to 4 (see that script).",
         "# Included by gauss.s. Registers: the horizontal blocks use s0, s1, s2, s4..s11, t1..t4 (all saved by gauss_h); the vertical",
         "# routine saves and uses every register except sp, ra, gp, tp."]
    L += ["", "# ---- horizontal pass: reached from gauss_h at the first interior pixel, only when r <= 4 (gauss_h tests that once per row)",
          ".Lh_fast_dispatch:", "    li t2, 2",
          "    bltu a3, t2, .Lh_f1", "    beq a3, t2, .Lh_f2", "    li t2, 3", "    beq a3, t2, .Lh_f3", "    j .Lh_f4"]
    for R in RADII:
        L += h_block(R)
    L += ["", "# ---- vertical pass: the whole routine for radius 1 to 4 (called from gauss_v with r in a3)",
          ".Lv_fast:", "    addi sp, sp, -12*PTRSIZE"]
    for i in range(12):
        L.append("    REG_S s%d, %d*PTRSIZE" % (i, i))
    L += ["    li t2, 2", "    bltu a3, t2, .Lv_f1", "    beq a3, t2, .Lv_f2", "    li t2, 3", "    beq a3, t2, .Lv_f3", "    j .Lv_f4"]
    for R in RADII:
        L += v_block(R)
    L += ["", ".Lv_fast_exit:"]
    for i in range(12):
        L.append("    REG_L s%d, %d*PTRSIZE" % (i, i))
    L += ["    addi sp, sp, 12*PTRSIZE", "    ret"]
    return "\n".join(L) + "\n"


def counts():
    """Instructions per pixel in the loops (the cycle model adds 1 for the taken branch per iteration)."""
    out = []
    for R in RADII:
        L = []
        h_body(R, L)
        h = len(L) + 3 + 1                                   # + addi a1, addi t6, addi t0 + blt
        L = []
        v_body(R, 0, L)
        body = len(L)
        quad = 4 * body + (2 * R + 1) + 2 + 1                # 4 samples + pointer updates + a1, a2 + bgez
        out.append((R, h, body, quad))
    return out


if __name__ == "__main__":
    if "--counts" in sys.argv:
        print("R | H loop instructions/pixel | V body | V per 4 samples (incl. updates and branch) | V per sample")
        for R, h, body, quad in counts():
            print("%d | %d | %d | %d | %.2f" % (R, h, body, quad, quad / 4))
        sys.exit(0)
    t = text()
    if "--check" in sys.argv:
        sys.exit(0 if os.path.exists(PATH) and open(PATH).read() == t else 1)
    open(PATH, "w").write(t)
    print("wrote", PATH, "(%d lines)" % t.count("\n"))
