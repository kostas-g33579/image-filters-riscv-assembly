#!/usr/bin/env python3
"""Tests for scripts/nifti_slice.py using synthetic NIfTI files with known contents.

Every file is built here, byte by byte, with an independent writer; the expected
slices are computed from the voxel function directly, not by calling the tool's code.
The tool is run as a separate process (`python3 -I`), so exit codes and messages are
tested too. The last test, skipped if the sample is absent, pins a real BraTS slice.

Usage: python3 tests/test_nifti.py      (or: make test-nifti)
"""
import gzip
import hashlib
import math
import os
import struct
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "scripts", "nifti_slice.py")
failures = 0


def check(name, ok, detail=""):
    global failures
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))
    failures += not ok


# ---------------------------------------------------------------- independent NIfTI-1 writer
CODES = {"uint8": (2, "B"), "int16": (4, "h"), "int32": (8, "i"), "float32": (16, "f"),
         "float64": (64, "d"), "int8": (256, "b"), "uint16": (512, "H"), "uint32": (768, "I")}


def make_nifti(dims, dtype, voxel, endian="<", slope=1.0, inter=0.0, magic=b"n+1\0", sizeof_hdr=348,
               bitpix=None, vox_offset=352.0, ndim=None, truncate=0, descrip=b"synthetic"):
    """dims = [x, y, z] or [x, y, z, t]; voxel(x, y, z, t) gives each value."""
    code, fmt = CODES[dtype]
    nbytes = struct.calcsize(fmt)
    nd = ndim if ndim is not None else len(dims)
    dim = [nd] + list(dims) + [1] * (7 - len(dims))
    h = bytearray(348)
    h[0:4] = struct.pack(endian + "i", sizeof_hdr)
    h[40:56] = struct.pack(endian + "8h", *dim[:8])
    h[70:74] = struct.pack(endian + "2h", code, bitpix if bitpix is not None else 8 * nbytes)
    h[76:108] = struct.pack(endian + "8f", 1, 1.5, 2.0, 2.5, 1, 1, 1, 1)       # distinct voxel sizes
    h[108:112] = struct.pack(endian + "f", vox_offset)
    h[112:120] = struct.pack(endian + "2f", slope, inter)
    h[148:148 + len(descrip)] = descrip
    h[344:348] = magic
    body = bytearray()
    X, Y, Z = dims[0], dims[1], dims[2]
    T = dims[3] if len(dims) > 3 else 1
    for t in range(T):
        for z in range(Z):
            for y in range(Y):
                for x in range(X):
                    body += struct.pack(endian + fmt, voxel(x, y, z, t))
    data = bytes(h) + b"\0" * 4 + bytes(body)          # 348 header + 4 extension bytes = vox_offset 352
    return data[:len(data) - truncate] if truncate else data


def write(d, name, data, gz=False):
    p = os.path.join(d, name)
    with (gzip.open(p, "wb") if gz else open(p, "wb")) as f:
        f.write(data)
    return p


def run(*args):
    r = subprocess.run([sys.executable, "-I", TOOL, *args], capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout, r.stderr


def read_pgm(path):
    data = open(path, "rb").read()
    toks, pos = [], 2
    while len(toks) < 3:
        while data[pos:pos + 1].isspace():
            pos += 1
        s = pos
        while data[pos:pos + 1].isdigit():
            pos += 1
        toks.append(int(data[s:pos]))
    w, h, mv = toks
    body = data[pos + 1:]
    vals = list(body[:w * h]) if mv < 256 else list(struct.unpack(">%dH" % (w * h), body[:2 * w * h]))
    return w, h, mv, vals


def V(x, y, z, t):                      # a voxel function with no accidental symmetries
    return (x + 3 * y + 7 * z + 11 * t) % 200 + t


def expected(axis, index, dims, t):
    """Expected rows[0] at the top, written down from the orientation rules in the docstring."""
    X, Y, Z = dims
    if axis == "z":
        return X, Y, [[V(x, y, index, t) for x in range(X)] for y in range(Y - 1, -1, -1)]
    if axis == "y":
        return X, Z, [[V(x, index, z, t) for x in range(X)] for z in range(Z - 1, -1, -1)]
    return Y, Z, [[V(index, y, z, t) for y in range(Y)] for z in range(Z - 1, -1, -1)]


def main():
    with tempfile.TemporaryDirectory() as d:
        dims = [7, 5, 4]

        # --- slices, all three axes, several indices, 3-D plain file
        p = write(d, "a.nii", make_nifti(dims, "uint8", V))
        for axis in "xyz":
            n = {"x": 7, "y": 5, "z": 4}[axis]
            for index in sorted({0, n // 2, n - 1}):
                out = os.path.join(d, "s.pgm")
                code, so, se = run(p, "--axis", axis, "--index", str(index), "-o", out)
                w, h, mv, vals = read_pgm(out)
                ew, eh, rows = expected(axis, index, dims, 0)
                check(f"3-D uint8, axis {axis} index {index}: orientation and values",
                      code == 0 and (w, h) == (ew, eh) and vals == [v for r in rows for v in r], se[:80])
        code, so, se = run(p, "--info-only")
        check("default slice is the middle one", "index 2 of 0..3" in so, so)

        # --- 4-D: channels differ, gzip, big-endian
        dims4 = [6, 5, 3, 3]
        for label, kw, gz in (("little-endian .nii", {}, False), ("gzip .nii.gz", {}, True),
                              ("big-endian .nii", {"endian": ">"}, False), ("big-endian .nii.gz", {"endian": ">"}, True)):
            p4 = write(d, "b" + label.replace(" ", "_") + (".nii.gz" if gz else ".nii"),
                       make_nifti(dims4, "int16", V, **kw), gz=gz)
            for t in range(3):
                out = os.path.join(d, "t.pgm")
                code, so, se = run(p4, "--channel", str(t), "--axis", "z", "--index", "1", "-o", out)
                w, h, mv, vals = read_pgm(out)
                ew, eh, rows = expected("z", 1, dims4[:3], t)
                check(f"4-D int16 {label}, channel {t}", code == 0 and vals == [v for r in rows for v in r], se[:80])

        # --- every supported data type reproduces exact whole numbers
        for dt in ("uint8", "int8", "int16", "uint16", "int32", "uint32", "float32", "float64"):
            pd = write(d, f"dt_{dt}.nii", make_nifti([5, 4, 3], dt, lambda x, y, z, t: (x * 10 + y + z) % 120))
            out = os.path.join(d, "dt.pgm")
            code, so, se = run(pd, "--axis", "z", "--index", "0", "-o", out)
            w, h, mv, vals = read_pgm(out)
            exp = [(x * 10 + y) % 120 for y in range(3, -1, -1) for x in range(5)]
            check(f"data type {dt}: exact values", code == 0 and vals == exp and "written unchanged" in so, so[:60] + se[:60])

        # --- maxval selection
        for top, want in ((200, 255), (255, 255), (256, 4095), (4095, 4095), (4096, 65535), (60000, 65535)):
            pm = write(d, f"mv{top}.nii", make_nifti([4, 4, 1], "int32", lambda x, y, z, t: top if (x, y) == (0, 0) else 1))
            out = os.path.join(d, "m.pgm")
            code, so, se = run(pm, "-o", out)
            check(f"largest value {top} -> maxval {want}", code == 0 and read_pgm(out)[2] == want, so)
        pm = write(d, "mvforce.nii", make_nifti([4, 4, 1], "uint8", lambda x, y, z, t: x))
        code, so, se = run(pm, "-o", os.path.join(d, "m.pgm"), "--maxval", "1000")
        check("--maxval forces a larger maxval", code == 0 and read_pgm(os.path.join(d, "m.pgm"))[2] == 1000)
        code, so, se = run(pm, "-o", os.path.join(d, "m.pgm"), "--maxval", "2")
        check("--maxval smaller than the data is refused", code != 0 and "cannot hold" in se, se)

        # --- scaling
        ps = write(d, "scl.nii", make_nifti([4, 3, 1], "int16", lambda x, y, z, t: x + 10 * y, slope=2.0, inter=5.0))
        out = os.path.join(d, "sc.pgm")
        code, so, se = run(ps, "-o", out)
        exp = [2 * (x + 10 * y) + 5 for y in range(2, -1, -1) for x in range(4)]
        check("scl_slope / scl_inter are applied", code == 0 and read_pgm(out)[3] == exp and "applied" in so, so)
        code, so, se = run(ps, "-o", out, "--raw")
        exp = [x + 10 * y for y in range(2, -1, -1) for x in range(4)]
        check("--raw ignores scaling", code == 0 and read_pgm(out)[3] == exp, so)

        # --- non-integer data are rescaled, and the tool says so
        pf_ = write(d, "frac.nii", make_nifti([4, 4, 1], "float32", lambda x, y, z, t: 0.25 * (x + 4 * y) - 1.0))
        out = os.path.join(d, "f.pgm")
        code, so, se = run(pf_, "-o", out)
        w, h, mv, vals = read_pgm(out)
        check("fractional/negative data: rescaled to the full range and announced",
              code == 0 and "RESCALED" in so and min(vals) == 0 and max(vals) == 65535 and mv == 65535, so)
        order_ok = [vals[(3 - y) * 4 + x] for y in range(4) for x in range(4)] == sorted(vals)
        check("rescaling is monotonic", order_ok)
        pn = write(d, "neg.nii", make_nifti([4, 4, 1], "int16", lambda x, y, z, t: x - 2))
        code, so, se = run(pn, "-o", os.path.join(d, "n.pgm"))
        check("negative whole numbers are rescaled, not written wrongly", code == 0 and "RESCALED" in so, so)

        # --- window suggestion on a known distribution: half background, foreground 1..1000
        pw = write(d, "win.nii", make_nifti([40, 50, 1], "int16", lambda x, y, z, t: 0 if y < 25 else 1 + (x + 40 * (y - 25)) * 999 // 999))
        code, so, se = run(pw, "--info-only")
        pool = sorted(1 + (x + 40 * (y - 25)) for y in range(25, 50) for x in range(40))
        p1, p99 = pool[int(round(.01 * (len(pool) - 1)))], pool[int(round(.99 * (len(pool) - 1)))]
        check("window suggestion from foreground p1..p99",
              f"window {(p1 + p99) // 2} {p99 - p1}" in so and "50.0% of pixels are zero" in so, so)

        # --- device-limit message
        # the limit is inclusive: exactly 90,000 pixels (300 x 300) fits, one more row does not
        for w_, h_, word in ((100, 100, "FITS"), (300, 300, "FITS"), (301, 300, "TOO BIG"), (301, 300, "streaming")):
            pl = write(d, f"big{w_}x{h_}.nii", make_nifti([w_, h_, 1], "uint8", lambda x, y, z, t: 1))
            code, so, se = run(pl, "--info-only")
            check(f"{w_}x{h_} slice vs the firmware limit says {word!r}", code == 0 and word in so, so)

        # --- malformed and hostile files must fail cleanly (exit code != 0, a message, no traceback)
        good = make_nifti([4, 4, 2], "int16", V)
        bad = {
            "bad magic": make_nifti([4, 4, 2], "int16", V, magic=b"ni1\0"),
            "garbage magic": make_nifti([4, 4, 2], "int16", V, magic=b"XXXX"),
            "NIfTI-2 header size": make_nifti([4, 4, 2], "int16", V, sizeof_hdr=540),
            "wrong header size": make_nifti([4, 4, 2], "int16", V, sizeof_hdr=1234),
            "shorter than a header": good[:200],
            "empty file": b"",
            "bitpix disagrees with datatype": make_nifti([4, 4, 2], "int16", V, bitpix=8),
            "5 dimensions": make_nifti([4, 4, 2, 1], "int16", V, ndim=6),
            "one dimension": make_nifti([4, 4, 2], "int16", V, ndim=1),
            "zero-sized dimension": make_nifti([4, 0, 2], "int16", V),
            "negative dimension": make_nifti([4, -3, 2], "int16", V),
            "vox_offset too small": make_nifti([4, 4, 2], "int16", V, vox_offset=100.0),
            "vox_offset huge": make_nifti([4, 4, 2], "int16", V, vox_offset=1e12),
            "vox_offset NaN": make_nifti([4, 4, 2], "int16", V, vox_offset=float("nan")),
            "truncated voxel data": make_nifti([4, 4, 2], "int16", V, truncate=20),
            "unsupported data type": None,
        }
        h = bytearray(good)
        h[70:74] = struct.pack("<2h", 128, 24)                 # RGB24: not supported
        bad["unsupported data type"] = bytes(h)
        for name, data in bad.items():
            pb = write(d, "bad.nii", data)
            code, so, se = run(pb, "--info-only")
            check(f"malformed file refused cleanly: {name}", code != 0 and "error" in se.lower() and "Traceback" not in se, se[:100])
        # huge declared size must be refused WITHOUT trying to allocate it
        h = bytearray(good)
        h[40:56] = struct.pack("<8h", 3, 30000, 30000, 30000, 1, 1, 1, 1)
        pb = write(d, "huge.nii", bytes(h))
        code, so, se = run(pb, "--info-only")
        check("absurd declared size is refused without loading", code != 0 and "refusing" in se, se[:100])
        # NaN voxel
        pnan = write(d, "nan.nii", make_nifti([3, 3, 1], "float32", lambda x, y, z, t: float("nan") if x == 1 else 1.0))
        code, so, se = run(pnan, "-o", os.path.join(d, "x.pgm"))
        check("NaN voxel values are refused", code != 0 and "NaN" in se, se)
        # bad requests on a good file
        pg = write(d, "good.nii", good)
        for what, args in (("channel out of range", ["--channel", "1"]), ("negative channel", ["--channel", "-1"]),
                           ("index out of range", ["--index", "9"]), ("negative index", ["--index", "-1"])):
            code, so, se = run(pg, "--info-only", *args)
            check(f"bad request refused: {what}", code != 0 and "Traceback" not in se, se[:100])
        code, so, se = run(os.path.join(d, "does_not_exist.nii"), "--info-only")
        check("missing file is a clean error", code != 0 and "Traceback" not in se and "cannot read" in se, se)
        code, so, se = run(pg)
        check("no output file and no --info-only is a usage error", code != 0, se)
        # a gzip file that is not a valid gzip stream
        pgz = os.path.join(d, "broken.nii.gz")
        open(pgz, "wb").write(b"\x1f\x8b\x08\x00" + b"\xde\xad" * 50)
        code, so, se = run(pgz, "--info-only")
        check("corrupt gzip is a clean error", code != 0 and "Traceback" not in se, se[:100])

    # --- a real BraTS slice, pinned (the slice was checked by eye: tumour ring on the T1c channel)
    sample = os.path.join(ROOT, "data", "msd_task01_sample", "BRATS_001.nii.gz")
    if os.path.exists(sample):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "r.pgm")
            code, so, se = run(sample, "--channel", "2", "--axis", "z", "--index", "77", "-o", out)
            w, h, mv, vals = read_pgm(out)
            check("real BraTS_001, T1c, axial 77: 240x240, maxval 4095, max 2238",
                  code == 0 and (w, h, mv) == (240, 240, 4095) and max(vals) == 2238, so)
            check("real BraTS_001, T1c, axial 77: file hash is the checked slice",
                  hashlib.sha256(open(out, "rb").read()).hexdigest()[:16] == "b0c22a49b2c8c5c6", hashlib.sha256(open(out, "rb").read()).hexdigest()[:16])
    else:
        print("SKIP  real BraTS sample (data/msd_task01_sample/BRATS_001.nii.gz not present)")

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    sys.exit(1 if failures else 0)


main()
