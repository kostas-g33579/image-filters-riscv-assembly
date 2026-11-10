#!/usr/bin/env python3
"""Run the on-chip timing probe of the separable Gaussian prototype and check it.

The probe firmware (pico/probe/gauss_probe.c, build-pico/gauss_probe.uf2) runs src/gauss.s on a built-in 256 x 96 image for
eight radii and prints, per pass, the cycles and instructions retired and an FNV-1a checksum of the result. This tool
recomputes the image and the results with the Python model (the same formula as tests/test_gauss.py), requires every
checksum to match, and prints cycles per pixel next to the prediction made from the datasheet's cycle table (for radius 1 to 4 from the instruction counts of the straight-line code).

usage: sudo python3 pico/tools/gauss_probe_check.py [--port DEV] [--transcript FILE] [--timeout S] [--selftest]
       --transcript FILE   check a saved transcript instead of talking to a board
       --selftest          check the checker: a faithful transcript must pass, corrupted ones must fail
"""
import argparse, importlib.util, os, re, select, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import gauss_weights as gw
import make_gauss_probe_data as pd

W, H = 256, 96
PRED_H = lambda r: 11 + 12 * r - 1 + 3 + 7          # cycles per interior pixel, generic loops, from the datasheet's cycle table
PRED_V = lambda r: 9 + 10 * r + 8
# Radius 1 to 4 take the straight-line code of src/gauss_fast.inc. Its instruction counts come from
# `scripts/gen_gauss_fast.py --counts`; each loop iteration costs one cycle more for the taken branch, no load is followed by its
# consumer. H: per interior pixel (14, 19, 24, 29 instructions + 1), the 2r clamped pixels at the ends take the generic edge code
# (30 + 22 r cycles each), plus about 65 cycles per row for saving and restoring s0..s11 and the dispatch. V: four samples per
# iteration (46, 68, 90, 112 instructions + 1) plus about 55 + 3r cycles per row for the same reasons.
FAST_H_INSTR = {1: 14, 2: 19, 3: 24, 4: 29}
FAST_V_QUAD = {1: 46, 2: 68, 3: 90, 4: 112}


def predicted(r):
    if r in FAST_H_INSTR:
        h = ((W - 2 * r) * (FAST_H_INSTR[r] + 1) + 2 * r * (30 + 22 * r) + 65) / W
        v = (FAST_V_QUAD[r] + 1) / 4 + (55 + 3 * r) / W
        return h, v
    edge = 2 * min(r, W // 2) / W
    return (1 - edge) * PRED_H(r) + edge * (30 + 22 * r), PRED_V(r)


def fnv(vals):
    h = 2166136261
    for v in vals:
        for b in (v & 0xFF, v >> 8):
            h = ((h ^ b) * 16777619) & 0xFFFFFFFF
    return h


def full(w):
    return w[:0:-1] + w


def model():
    x, img = 12345, []
    for _ in range(W * H):
        x = (x * 1664525 + 1013904223) & 0xFFFFFFFF
        img.append((x >> 12) & 0xFFFF)
    rows = [img[y * W:(y + 1) * W] for y in range(H)]
    exp = {"image": fnv(img)}
    for r in pd.RADII:
        w = gw.gaussian(pd.sigma(r), r)
        K = full(w)
        mid = []
        for row in rows:
            pad = [row[0]] * r + row + [row[-1]] * r
            mid.append([(sum(K[j] * pad[x + j] for j in range(2 * r + 1)) + 8192) >> 14 for x in range(W)])
        out = []
        for y in range(H):
            sel = [mid[min(max(y + j - r, 0), H - 1)] for j in range(2 * r + 1)]
            out.append([(sum(K[j] * sel[j][x] for j in range(2 * r + 1)) + 8192) >> 14 for x in range(W)])
        exp[("H", r)] = fnv([v for row in mid for v in row])
        exp[("V", r)] = fnv([v for row in out for v in row])
    return exp


def parse(text):
    d, cfg = {}, None
    for line in text.replace("\r\n", "\n").split("\n"):
        m = re.match(r"^CFG W=(\d+) H=(\d+) image=([0-9a-f]{8})$", line)
        if m:
            cfg = (int(m.group(1)), int(m.group(2)), int(m.group(3), 16))
        m = re.match(r"^([HV]) r=(\d+) cycles=(\d+) instret=(\d+) fnv=([0-9a-f]{8})$", line)
        if m:
            d[(m.group(1), int(m.group(2)))] = (int(m.group(3)), int(m.group(4)), int(m.group(5), 16))
    return cfg, d, "GPROBE-BEGIN" in text and "GPROBE-END" in text


def check(text, exp, show=True):
    cfg, d, whole = parse(text)
    bad = []
    if not whole:
        bad.append("the transcript is not complete (no GPROBE-BEGIN / GPROBE-END)")
    if cfg != (W, H, exp["image"]):
        bad.append("image: chip %r vs model %r" % (cfg, (W, H, exp["image"])))
    for key in [(p, r) for r in pd.RADII for p in "HV"]:
        if key not in d:
            bad.append("%s r=%d missing" % key)
        elif d[key][2] != exp[key]:
            bad.append("%s r=%d: chip %08x vs model %08x" % (key[0], key[1], d[key][2], exp[key]))
    if show:
        print("%3s | %9s %9s %6s | %9s %9s %6s | %9s %9s | %s" % ("r", "H meas", "H pred", "ratio", "V meas", "V pred", "ratio", "2-D meas", "2-D pred", "IPC (H, V)"))
        for r in pd.RADII:
            if ("H", r) in d and ("V", r) in d:
                ch, ih, _ = d[("H", r)]
                cv, iv, _ = d[("V", r)]
                ph, pv = predicted(r)
                n = W * H
                print("%3d | %9.1f %9.1f %6.2f | %9.1f %9.1f %6.2f | %9.1f %9.1f | %.2f, %.2f" % (r, ch / n, ph, ch / n / ph, cv / n, pv, cv / n / pv, (ch + cv) / n, ph + pv, ih / ch, iv / cv))
        print("(cycles per pixel, from the core's own mcycle / minstret counters; pred = the datasheet cycle table applied to the loops)")
    return bad


def read_board(port, timeout):
    spec = importlib.util.spec_from_file_location("pico_filter", os.path.join(ROOT, "pico", "tools", "pico_filter.py"))
    pf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pf)
    port = port or pf.autodetect_port()
    print("board:", pf.describe_device(port))
    link = pf.SerialLink(port)
    link.write(b"x")
    buf, end = b"", time.monotonic() + timeout
    while b"GPROBE-END" not in buf:
        left = end - time.monotonic()
        if left <= 0:
            raise SystemExit("timed out waiting for GPROBE-END; got %d bytes: %r" % (len(buf), buf[-120:]))
        r, _, _ = select.select([link.rfd], [], [], left)
        if r:
            c = os.read(link.rfd, 65536)
            if not c:
                raise SystemExit("link closed")
            buf += c
    link.close()
    return buf.decode("latin-1")


def fake_transcript(exp, crlf=True):
    lines = ["GPROBE-BEGIN", "CFG W=%d H=%d image=%08x" % (W, H, exp["image"])]
    for r in pd.RADII:
        ph, pv = predicted(r)
        for p, per in (("H", ph), ("V", pv)):
            lines.append("%s r=%d cycles=%d instret=%d fnv=%08x" % (p, r, int(per * W * H), int(per * W * H * 0.9), exp[(p, r)]))
    lines.append("GPROBE-END")
    return ("\r\n" if crlf else "\n").join(lines) + ("\r\n" if crlf else "\n")


def selftest(exp):
    good = fake_transcript(exp)
    ok = not check(good, exp, show=False) and not check(fake_transcript(exp, crlf=False), exp, show=False)
    cases = {"a checksum changed": good.replace("fnv=%08x" % exp[("V", 4)], "fnv=%08x" % (exp[("V", 4)] ^ 1), 1),
             "the image checksum changed": good.replace("image=%08x" % exp["image"], "image=00000000", 1),
             "a line missing": "\n".join(l for l in good.split("\n") if not l.startswith("H r=7 ")),
             "no end marker": good.replace("GPROBE-END", ""), "no begin marker": good.replace("GPROBE-BEGIN", ""),
             "H and V swapped": good.replace("H r=2 ", "X r=2 ").replace("V r=2 ", "H r=2 ").replace("X r=2 ", "V r=2 "),
             "garbage": "hello"}
    missed = [k for k, t in cases.items() if not check(t, exp, show=False)]
    print("selftest: a faithful transcript (with CR LF and with LF) passes: %s; %d corruptions, missed: %s" % (ok, len(cases), missed or "none"))
    return 0 if ok and not missed else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port")
    ap.add_argument("--transcript")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    exp = model()
    if a.selftest:
        return selftest(exp)
    text = open(a.transcript, newline="").read() if a.transcript else read_board(a.port, a.timeout)
    bad = check(text, exp)
    if bad:
        print("DIFFERENT from the model:")
        for b in bad[:12]:
            print("  ", b)
        return 1
    print("IDENTICAL to the model: every checksum of every pass matches.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
