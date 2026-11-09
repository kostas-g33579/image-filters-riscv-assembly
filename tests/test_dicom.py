#!/usr/bin/env python3
"""Tests for scripts/dicom_slice.py.

An independent DICOM WRITER (in this file, written separately from the reader) builds files
with known pixels and known tags; the tool must give back exactly those pixels, the right
window, the right slice order, a clear refusal for everything unsupported, and must never
print an identifying value. Real series in data/dicom/ (git-ignored, downloaded from TCIA)
are checked too when present, against a decoder that shares no code with the tool.

Usage: python3 tests/test_dicom.py      (or: make test-dicom)
"""
import contextlib, io, math, os, random, re, struct, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "scripts", "dicom_slice.py")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import importlib.util
spec = importlib.util.spec_from_file_location("dicom_slice", TOOL)
ds = importlib.util.module_from_spec(spec); spec.loader.exec_module(ds)
if os.environ.get("DICOM_TOOL"):                      # mutation testing: run a modified copy
    spec = importlib.util.spec_from_file_location("dicom_slice_m", os.environ["DICOM_TOOL"])
    ds = importlib.util.module_from_spec(spec); spec.loader.exec_module(ds)

failures = 0
IMPL, EXPL = "1.2.840.10008.1.2", "1.2.840.10008.1.2.1"


def check(name, ok, detail=""):
    global failures
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))
    failures += not ok


# ---------------------------------------------------------------- the independent writer
# (moved to tests/dicomgen.py so the C reader's tests can use it too)
_gspec = importlib.util.spec_from_file_location("dicomgen", os.path.join(ROOT, "tests", "dicomgen.py"))
dicomgen = importlib.util.module_from_spec(_gspec); _gspec.loader.exec_module(dicomgen)
pad, enc, US, DS, seq_undefined, seq_defined, pack_pixels, SENT, make = (
    dicomgen.pad, dicomgen.enc, dicomgen.US, dicomgen.DS, dicomgen.seq_undefined, dicomgen.seq_defined,
    dicomgen.pack_pixels, dicomgen.SENT, dicomgen.make)


# ---------------------------------------------------------------- running the tool
def run(args, cwd=None):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            ds.main(args)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (1 if e.code else 0)
            if isinstance(e.code, str):
                err.write(e.code + "\n")
    return code, out.getvalue(), err.getvalue()


def read_pgm(path):
    b = open(path, "rb").read()
    m = re.match(rb"P5\n(\d+) (\d+)\n(\d+)\n", b)
    w, h, mx = (int(x) for x in m.groups())
    body = b[m.end():]
    vals = list(body) if mx < 256 else list(struct.unpack(">%dH" % (w * h), body))
    return w, h, mx, vals


def tool(blob, extra=(), name="a.dcm"):
    """Write one file, run the tool, return (code, out, err, pgm or None)."""
    with tempfile.TemporaryDirectory() as t:
        f, o = os.path.join(t, name), os.path.join(t, "o.pgm")
        open(f, "wb").write(blob)
        code, out, err = run([f, "-o", o, *extra])
        return code, out, err, (read_pgm(o) if code == 0 and os.path.exists(o) else None)


def window_of(out):
    m = re.search(r"file window : .*->\s+window (-?\d+) (\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else None


# ================================================================= 1. exact pixels, both encodings
rng = random.Random(1)
ok = True; det = ""
for ts in (EXPL, IMPL):
    for rows, cols in [(1, 1), (1, 7), (7, 1), (3, 5), (16, 16), (48, 64), (255, 256), (2, 3)]:
        for hi in (200, 4000, 60000):
            vals = [rng.randrange(hi + 1) for _ in range(rows * cols)]
            vals[0] = hi
            code, out, err, pgm = tool(make(rows, cols, vals, ts=ts))
            want_mx = 255 if hi <= 255 else 4095 if hi <= 4095 else 65535
            if not (code == 0 and pgm and pgm[:3] == (cols, rows, want_mx) and pgm[3] == vals):
                ok, det = False, f"{ts} {rows}x{cols} hi={hi}: code {code} {err[:100]}"
check("Explicit and Implicit VR: pixels come back exactly, PGM maxval 255/4095/65535, sizes 1x1 .. 255x256", ok, det)

# ================================================================= 2. 8 bit, 12 bit stored, high bit shifts
vals = [rng.randrange(256) for _ in range(30)]
code, out, err, pgm = tool(make(5, 6, vals, alloc=8))
check("8 bits allocated", code == 0 and pgm[3] == vals and pgm[2] == 255, err)
vals = [rng.randrange(4096) for _ in range(30)]
for ts in (EXPL, IMPL):
    code, out, err, pgm = tool(make(5, 6, vals, alloc=16, stored=12, high=11, ts=ts))
    check(f"12 bits stored, high bit 11, garbage in the unused top bits is masked ({'explicit' if ts == EXPL else 'implicit'})",
          code == 0 and pgm[3] == vals, err)
    code, out, err, pgm = tool(make(5, 6, vals, alloc=16, stored=12, high=15, ts=ts))
    check(f"12 bits stored in the HIGH bits (high bit 15) are shifted down ({'explicit' if ts == EXPL else 'implicit'})",
          code == 0 and pgm[3] == vals, err)

# ================================================================= 3. signed samples
svals = [-5, -1, 0, 1, 100, -1024, 300, 2000, -300, 7, 0, 0]
code, out, err, pgm = tool(make(3, 4, svals, signed=True, wc=100, ww=400))
check("signed 16 bit, minimum -1024: shifted up by 1024, window centre shifted too",
      code == 0 and pgm[3] == [v + 1024 for v in svals] and window_of(out) == (1124, 400), (err, window_of(out)))
check("... and the tool says it shifted", "shifted up by 1024" in out, out)
svals2 = [-2000, 5, 6, 7, 8, 9]
code, out, err, pgm = tool(make(2, 3, svals2, signed=True, wc=0, ww=1000))
check("signed values below -1024: shifted by the minimum so the lowest becomes 0",
      code == 0 and pgm[3] == [v + 2000 for v in svals2] and window_of(out) == (2000, 1000), (err, out[-300:]))
code, out, err, pgm = tool(make(2, 3, [1, 2, 3, 4, 5, 6], signed=True))
check("signed type but all values non-negative: written unchanged", code == 0 and pgm[3] == [1, 2, 3, 4, 5, 6], err)
code, out, err, pgm = tool(make(3, 4, svals, signed=True, stored=12, high=11))
check("signed 12 bit stored (sign extended from bit 11)", code == 0 and pgm[3] == [v + 1024 for v in svals], err)

# ================================================================= 4. rescale
raw = [0, 100, 1024, 2000, 3000, 4095]
code, out, err, pgm = tool(make(2, 3, raw, slope=1, inter=-1024, wc=40, ww=400))
check("CT-style rescale (intercept -1024): HU + 1024 equals the stored values again, window 1064 400",
      code == 0 and pgm[3] == raw and window_of(out) == (1064, 400), (err, window_of(out)))
code, out, err, pgm = tool(make(2, 3, raw, slope=1, inter=-1024), ["--raw"])
check("--raw ignores the rescale", code == 0 and pgm[3] == raw and "not applied" in out, err)
code, out, err, pgm = tool(make(2, 3, [0, 10, 20, 30, 40, 50], slope=2, inter=5))
check("slope 2 intercept 5: whole numbers, written as 5,25,...", code == 0 and pgm[3] == [5, 25, 45, 65, 85, 105], err)
code, out, err, pgm = tool(make(2, 3, [0, 1, 2, 3, 4, 5], slope=0.5, inter=0, wc=1.25, ww=2.5))
exp = [round(v * 0.5 / 2.5 * 65535) for v in [0, 1, 2, 3, 4, 5]]
check("fractional slope 0.5: linear rescale to 0..65535 and the window converted the same way",
      code == 0 and pgm[3] == exp and window_of(out) == (round(1.25 / 2.5 * 65535), round(2.5 / 2.5 * 65535)),
      (err, window_of(out)))
check("... and the tool says RESCALED", "RESCALED" in out)

# ================================================================= 5. window handling
code, out, err, pgm = tool(make(2, 3, [1, 2, 3, 4, 5, 6], wc=[300, 40], ww=[800, 90]))
check("several window values: the first is used", window_of(out) == (300, 800), out)
code, out, err, pgm = tool(make(2, 3, [1, 2, 3, 4, 5, 6]))
check("no window in the file: no 'file window' line, a suggestion is still printed", window_of(out) is None and "suggested" in out)

# ================================================================= 6. MONOCHROME1
vals = [0, 10, 200, 255, 3, 4]
code, out, err, pgm = tool(make(2, 3, vals, alloc=8, photometric=b"MONOCHROME1"))
check("MONOCHROME1 8 bit is inverted (255 - v) and the tool says so",
      code == 0 and pgm[3] == [255 - v for v in vals] and "MONOCHROME1" in out, err)
vals = [0, 1000, 4095, 7, 8, 9]
code, out, err, pgm = tool(make(2, 3, vals, stored=12, high=11, photometric=b"MONOCHROME1"))
check("MONOCHROME1 12 bit stored is inverted within 12 bits", code == 0 and pgm[3] == [4095 - v for v in vals], err)

# ---- gaps found by mutation testing: boundaries and cases the first tests did not reach
code, out, err, pgm = tool(make(2, 3, [-5, 3, 100, 7, 8, 9], signed=True, wc=50, ww=100))
check("a small negative minimum (-5) still shifts by the standard 1024, not by 5",
      code == 0 and pgm[3] == [1019, 1027, 1124, 1031, 1032, 1033] and window_of(out) == (1074, 100), (err, pgm))
code, out, err, pgm = tool(make(2, 2, [-5, 0, 7, 100], signed=True, photometric=b"MONOCHROME1"))
check("MONOCHROME1 with signed samples: inverted as -1 - v (then shifted)",
      code == 0 and pgm[3] == [v + 1024 for v in [4, -1, -8, -101]], (err, pgm))
for hi, want in [(255, 255), (256, 4095), (4095, 4095), (4096, 65535), (65535, 65535), (1, 255), (0, 255)]:
    code, out, err, pgm = tool(make(1, 3, [0, hi // 2, hi]))
    check(f"largest value {hi}: PGM maxval {want}", code == 0 and pgm[2] == want, (err, pgm and pgm[2]))
ok = True
for k, accepted in [(20, False), (22, False), (24, True), (26, True), (30, True)]:
    code, out, err, pgm = tool(make(3, 4, alloc=16, pix_override=bytes(range(k))))
    ok &= (code == 0) == accepted and (accepted or ("needs" in err and "cannot read" not in err))
check("pixel data of 20 and 22 bytes for a 3x4 16-bit image is refused (needs 24), 24 or more (padding) is accepted", ok)
bad_vr = bytearray(make(3, 4))
rows_el = struct.pack("<HH2sH", 0x0028, 0x0010, b"US", 2) + US(3)
code, out, err, pgm = tool(bytes(bad_vr).replace(rows_el, struct.pack("<HH2sHI", 0x0028, 0x0010, b"UL", 4, 3)[:8] + struct.pack("<I", 3), 1))
check("refused: Rows carried with the wrong (but valid) value representation UL", code != 0 and "value representation" in err, err)
code, out, err, pgm = tool(make(2, 2, ts=IMPL, extra_before=struct.pack("<HHI", 0x0008, 0x1140, 100000)))
check("refused: an element claiming 100,000 bytes in a 300-byte file", code != 0 and "runs past the end" in err, err)

# ================================================================= 7. sequences, private tags, padding
for explicit_ts in (EXPL, IMPL):
    ex = explicit_ts == EXPL
    inner = enc(0x0008, 0x1150, b"UI", b"1.2.3", ex) + enc(0x0008, 0x1155, b"UI", b"4.5.6", ex)
    nested = seq_undefined(0x0040, 0xA730, ex, [inner, inner], item_undefined=True)
    seqs = (seq_undefined(0x0008, 0x1140, ex, [inner + nested, inner], item_undefined=True) +
            seq_undefined(0x0008, 0x1111, ex, [inner], item_undefined=False) +
            seq_defined(0x0008, 0x1115, ex, [inner]) +
            enc(0x0009, 0x0010, b"LO", b"PRIVATE", ex) + enc(0x0009, 0x1001, b"LO", b"x" * 7, ex) +
            enc(0x0019, 0x10FF, b"UN", b"\x01\x02\x03", ex))
    vals = [rng.randrange(4096) for _ in range(12)]
    code, out, err, pgm = tool(make(3, 4, vals, ts=explicit_ts, extra_before=seqs))
    check(f"sequences of undefined/defined length (nested), private tags, odd values: skipped correctly "
          f"({'explicit' if ex else 'implicit'})", code == 0 and pgm and pgm[3] == vals, err)

# ================================================================= 8. series: order, selection, folders
def series_dir(t, specs, name_fn=lambda i: "f%03d" % i):
    for i, kw in enumerate(specs):
        open(os.path.join(t, name_fn(i)), "wb").write(make(**kw))


IOP_AX = (1, 0, 0, 0, 1, 0)
specs = []
zs = [5.0, 3.0, 4.0, 1.0, 2.0]
random.Random(3).shuffle(zs)
for z in zs:
    specs.append(dict(rows=2, cols=2, vals=[int(z * 10)] * 4, pos=(0, 0, z), iop=IOP_AX, inst=int(100 - z)))
with tempfile.TemporaryDirectory() as t:
    series_dir(t, specs)
    open(os.path.join(t, "LICENSE"), "w").write("CC BY\n")
    res = {}
    for idx in range(5):
        o = os.path.join(t, "o.pgm")
        code, out, err = run([t, "--index", str(idx), "-o", o])
        res[idx] = (code, read_pgm(o)[3][0] if code == 0 else None, out)
    check("series shuffled on disk: --index 0..4 gives slices by position z=1..5 (not file order, not instance order)",
          [res[i][1] for i in range(5)] == [10, 20, 30, 40, 50], [res[i][:2] for i in range(5)])
    check("the non-DICOM LICENSE file is skipped and counted", "1 non-DICOM file(s) skipped" in res[0][2], res[0][2][:200])
    code, out, err = run([t, "--info-only"])
    check("default slice is the middle one", "index 2 of 0..4" in out, out)
    code, out, err = run([t, "--index", "5", "--info-only"])
    check("--index past the end is refused", code != 0 and "out of range" in err, err)
    code, out, err = run([t, "--index", "-1", "--info-only"])
    check("negative --index is refused", code != 0 and "out of range" in err, err)

# oblique orientation: the normal is not the z axis
c, s = math.cos(math.radians(30)), math.sin(math.radians(30))
iop = (1, 0, 0, 0, c, s)                      # rows along x, columns tilted 30 degrees about x
normal = (0, -s, c)
specs = []
ds_ = [3.0, -2.0, 7.5, 0.0, 1.5]
for d in ds_:
    specs.append(dict(rows=2, cols=2, vals=[int((d + 5) * 10)] * 4, pos=(d * 0, d * normal[1], d * normal[2]), iop=iop, inst=1))
with tempfile.TemporaryDirectory() as t:
    series_dir(t, specs)
    got = []
    for idx in range(5):
        o = os.path.join(t, "o.pgm")
        run([t, "--index", str(idx), "-o", o])
        got.append(read_pgm(o)[3][0])
    check("oblique series: ordered along the true normal", got == sorted(int((d + 5) * 10) for d in ds_), got)

# a general oblique orientation, normal computed independently here
cp_, sp_ = math.cos(math.radians(25)), math.sin(math.radians(25))
row_v = (0.8, 0.6, 0.0)
col_v = (-0.6 * cp_, 0.8 * cp_, sp_)
nrm = (row_v[1] * col_v[2] - row_v[2] * col_v[1], row_v[2] * col_v[0] - row_v[0] * col_v[2],
       row_v[0] * col_v[1] - row_v[1] * col_v[0])
dd = [2.0, -3.0, 0.5, 6.0, -1.0, 4.0]
specs = []
r2 = random.Random(5)
for d in dd:
    inplane = (r2.uniform(-20, 20), r2.uniform(-20, 20))
    p0 = [d * nrm[k] + inplane[0] * row_v[k] + inplane[1] * col_v[k] for k in range(3)]
    specs.append(dict(rows=2, cols=2, vals=[int(d * 10 + 100)] * 4, pos=tuple(p0), iop=row_v + col_v, inst=1))
with tempfile.TemporaryDirectory() as t:
    series_dir(t, specs)
    got = []
    for idx in range(6):
        o = os.path.join(t, "o.pgm"); run([t, "--index", str(idx), "-o", o]); got.append(read_pgm(o)[3][0])
    check("general oblique series with in-plane offsets: ordered along the true normal", got == sorted(int(d * 10 + 100) for d in dd), got)

# instance-number fallback, no geometry at all
with tempfile.TemporaryDirectory() as t:
    series_dir(t, [dict(rows=2, cols=2, vals=[v] * 4, inst=i) for v, i in [(30, 3), (10, 1), (20, 2)]])
    got = []
    for idx in range(3):
        o = os.path.join(t, "o.pgm"); run([t, "--index", str(idx), "-o", o]); got.append(read_pgm(o)[3][0])
    code, out, err = run([t, "--info-only"])
    check("no position: ordered by Instance Number, and the tool says so", got == [10, 20, 30] and "Instance Number" in out, (got, out))
with tempfile.TemporaryDirectory() as t:
    series_dir(t, [dict(rows=2, cols=2, vals=[v] * 4) for v in (1, 2, 3)])
    code, out, err = run([t, "--info-only"])
    check("neither position nor instance number: file-name order, with a warning", code == 0 and "warning" in out and "file-name order" in out, out)

# duplicate positions and a gap
with tempfile.TemporaryDirectory() as t:
    series_dir(t, [dict(rows=2, cols=2, pos=(0, 0, z), iop=IOP_AX) for z in (0, 1, 1, 2)])
    code, out, err = run([t, "--info-only"])
    check("duplicate positions give a warning", "share a position" in out, out)
with tempfile.TemporaryDirectory() as t:
    series_dir(t, [dict(rows=2, cols=2, pos=(0, 0, z), iop=IOP_AX) for z in (0, 1, 2, 5, 6)])
    code, out, err = run([t, "--info-only"])
    check("a gap in the slice spacing gives a warning", "uneven slice spacing" in out, out)

# several series in one folder
with tempfile.TemporaryDirectory() as t:
    specs = [dict(rows=2, cols=2, vals=[11] * 4, uid=b"1.1", pos=(0, 0, k), iop=IOP_AX) for k in range(3)] + \
            [dict(rows=2, cols=2, vals=[22] * 4, uid=b"2.2", pos=(0, 0, k), iop=IOP_AX) for k in range(5)]
    series_dir(t, specs)
    code, out, err = run([t, "--info-only"])
    check("two series in a folder: refused with a list to choose from", code != 0 and "2 series" in err and "--series 1 : 5 slices" in err, err)
    o = os.path.join(t, "o.pgm")
    run([t, "--series", "2", "-o", o])
    check("--series 2 picks the smaller series", read_pgm(o)[3][0] == 11)
    run([t, "--series", "1", "-o", o])
    check("--series 1 picks the largest", read_pgm(o)[3][0] == 22)
    code, out, err = run([t, "--series", "3", "--info-only"])
    check("--series out of range is refused", code != 0 and "out of range" in err, err)

# slices that disagree
with tempfile.TemporaryDirectory() as t:
    series_dir(t, [dict(rows=2, cols=2), dict(rows=3, cols=2)])
    code, out, err = run([t, "--info-only"])
    check("slices of one series with different sizes are refused", code != 0 and "disagree" in err, err)
with tempfile.TemporaryDirectory() as t:
    code, out, err = run([t, "--info-only"])
    check("an empty folder is refused", code != 0 and "no DICOM files" in err, err)
with tempfile.TemporaryDirectory() as t:
    open(os.path.join(t, "x.txt"), "w").write("hello")
    code, out, err = run([t, "--info-only"])
    check("a folder with no DICOM files is refused", code != 0 and "no DICOM files" in err, err)

# ================================================================= 9. refusals, each names what it is
REF = [
    ("JPEG baseline", dict(tsuid="1.2.840.10008.1.2.4.50"), "JPEG Baseline"),
    ("JPEG lossless", dict(tsuid="1.2.840.10008.1.2.4.70"), "JPEG Lossless"),
    ("JPEG 2000", dict(tsuid="1.2.840.10008.1.2.4.90"), "JPEG 2000"),
    ("RLE", dict(tsuid="1.2.840.10008.1.2.5"), "RLE"),
    ("deflate", dict(tsuid="1.2.840.10008.1.2.1.99"), "Deflated"),
    ("big endian", dict(tsuid="1.2.840.10008.1.2.2"), "Big Endian"),
    ("unknown transfer syntax", dict(tsuid="1.2.3.4.5.6"), "not supported"),
    ("colour (3 samples)", dict(spp=3), "colour"),
    ("PALETTE COLOR", dict(photometric=b"PALETTE COLOR"), "PALETTE COLOR"),
    ("RGB", dict(photometric=b"RGB"), "RGB"),
    ("multi-frame", dict(nframes=10), "multi-frame"),
    ("32 bits allocated", dict(alloc=16), None),
    ("encapsulated pixel data", dict(pix_undefined=True), "encapsulated"),
    ("missing Rows", dict(drop=("Rows",)), "Rows is missing"),
    ("zero columns", dict(cols=0, vals=[]), "outside"),
    ("huge image size", dict(rows=60000, cols=60000, vals=[], pix_override=b"\0" * 8), "outside"),
    ("pixel data too short", dict(rows=10, cols=10, pix_override=b"\0" * 50), "needs"),
]
for name, kw, word in REF:
    if word is None:
        continue
    kw = dict(kw); kw.setdefault("rows", 4); kw.setdefault("cols", 5)
    code, out, err, pgm = tool(make(**kw))
    check(f"refused: {name}", code != 0 and pgm is None and word.lower() in err.lower() and "Traceback" not in err, err[:200])
for ts in (IMPL, EXPL):                       # a 32-bit allocation is refused in both encodings
    code, out, err, pgm = tool(make(2, 2, ts=ts, alloc=32, pix_override=b"\0" * 16))
    check(f"refused: BitsAllocated 32 ({'explicit' if ts == EXPL else 'implicit'})", code != 0 and "BitsAllocated" in err, err)
blob = make(4, 5)
code, out, err, pgm = tool(blob[:128] + b"XXXX" + blob[132:])
check("refused: no DICM preamble", code != 0 and "preamble" in err, err)
code, out, err, pgm = tool(b"")
check("refused: empty file", code != 0 and "preamble" in err, err)
code, out, err, pgm = tool(blob[:len(blob) - 7])
check("refused: file cut off inside the pixel data", code != 0 and "runs past the end" in err, err)
code, out, err, pgm = tool(blob[:200])
check("refused: file cut off in the headers", code != 0 and "error:" in err and "Traceback" not in err, err)
code, out, err, pgm = tool(make(4, 5, tsuid=None, drop=()).replace(b"1.2.840.10008.1.2.1\0", b"1.2.840.10008.1.2.1\0")[:0])
code, out, err, pgm = tool(make(2, 2, ts=EXPL, extra_before=struct.pack("<HH2sH", 0x0008, 0x0090, b"ZZ", 4) + b"abcd"))
check("refused: an element with an invalid value representation", code != 0 and "value representation" in err, err)
deep = b"".join(struct.pack("<HHI", 0xFFFE, 0xE000, 0xFFFFFFFF) + struct.pack("<HHI", 0x0008, 0x1140, 0xFFFFFFFF) for _ in range(60))
code, out, err, pgm = tool(make(2, 2, ts=IMPL, extra_before=struct.pack("<HHI", 0x0008, 0x1140, 0xFFFFFFFF) + deep))
check("refused: sequences nested 60 deep (no stack overflow)", code != 0 and "deeper" in err and "Traceback" not in err, err[:200])
code, out, err, pgm = tool(make(2, 2, ts=IMPL, extra_before=struct.pack("<HHI", 0x0008, 0x1140, 0x7FFFFFFF)))
check("refused: an element claiming 2 GB inside a small file", code != 0 and "runs past the end" in err, err)
code, out, err, pgm = tool(make(2, 2).replace(struct.pack("<HH2sH", 0x0028, 0x0010, b"US", 2) + US(2),
                                              struct.pack("<HH2sH", 0x0028, 0x0010, b"US", 0), 1))
check("refused: Rows with an empty value", code != 0 and "malformed" in err, err)
code, out, err, pgm = tool(make(2, 2, ts=IMPL, extra_before=enc(0x0018, 0x0050, b"DS", b"abc", False)))
check("refused: a non-numeric decimal string in an expected tag", code != 0 and "malformed" in err, err)
code, out, err, pgm = tool(make(2, 3, [1, 2, 3, 4, 5, 6]), ["--maxval", "5"])
check("--maxval smaller than the data is refused", code != 0 and "cannot hold" in err, err)
code, out, err, pgm = tool(make(2, 3, [1, 2, 3, 4, 5, 6]), ["--maxval", "100"])
check("--maxval larger than needed is honoured", code == 0 and pgm[2] == 100, err)
code, out, err, pgm = tool(make(2, 3, [1, 2, 3, 4, 5, 6]), ["--info-only"])
check("--info-only writes nothing", code == 0 and pgm is None)
check("no -o and no --info-only is an error", run([os.devnull])[0] != 0)

# ================================================================= 10. privacy: sentinel values never leave the file
code, out, err, pgm = tool(make(3, 4, ts=EXPL, burned=b"YES", removed=b"YES"))
with tempfile.TemporaryDirectory() as t:
    f, o = os.path.join(t, "a.dcm"), os.path.join(t, "o.pgm")
    open(f, "wb").write(make(3, 4, burned=b"YES", removed=b"YES"))
    c2, out2, err2 = run([f, "-o", o])
    pg = open(o, "rb").read()
leak = [k for k, v in SENT.items() if v.decode() in out + err + out2 + err2 or v in pg]
check("no identifying value appears in output, errors or the PGM (sentinels in 8 tags)", not leak, leak)
check("... but the field NAMES that are present are listed", all(n in out for n in ("PatientName", "PatientID", "StudyDate")), out)
check("burned-in annotation is warned about", "BurnedInAnnotation=YES" in out, out)
check("'identity removed' flag is reported but does not hide the warning", "says identity was removed" in out and "Do not share" in out, out)
code, out, err, pgm = tool(make(3, 4, ident=False))
check("a file without identifying fields gets no privacy line", "privacy" not in out, out)
for ts in (IMPL, EXPL):                           # also in error messages
    code, out, err, pgm = tool(make(3, 4, ts=ts, drop=("Rows",)))
    check(f"error messages contain no identifying values ({'explicit' if ts == EXPL else 'implicit'})",
          code != 0 and not any(v.decode() in err for v in SENT.values()), err)
src = open(TOOL).read()
check("the tool's list of values it reads has no identifying tag in it",
      not set(ds.WANTED) & set(ds.IDENTIFYING), set(ds.WANTED) & set(ds.IDENTIFYING))

# ================================================================= 10b. strict numbers (no float() leniency)
# Decimal strings are read with a strict grammar: Python's float() would also accept nan, inf, "1_0" and
# unicode digits, and a nan reaching the geometry later would crash or silently poison it.
for ts in (IMPL, EXPL):
    ex = ts == EXPL
    for raw in (b"nan", b"inf", b"-inf", b"1_0", b"1,5", b".", b"e5", b"1e", b"1e1234", "\u0661".encode(), b"0x10",
                b"1.2.3", b"--1", b"1234567890123456789", b"9" * 70):
        code, out, err, pgm = tool(make(3, 3, ts=ts, extra_before=enc(0x18, 0x80, b"DS", raw, ex)))
        check(f"refused: decimal string {raw[:12]!r} ({'explicit' if ex else 'implicit'})",
              code != 0 and "malformed" in err, err)
    for raw in (b"1.5", b"+1.5", b"-.5", b"5.", b"1E3", b" 2 ", b"0.9765625", b"1\\2", b"123456789012345678"):
        code, out, err, pgm = tool(make(3, 3, ts=ts, extra_before=enc(0x18, 0x80, b"DS", raw, ex)))
        check(f"accepted: decimal string {raw[:12]!r} ({'explicit' if ex else 'implicit'})", code == 0, err)

# ================================================================= 11. fuzzing: never a traceback
base = make(4, 5, wc=100, ww=200, pos=(0, 0, 1), iop=IOP_AX, inst=3,
            extra_before=seq_undefined(0x0008, 0x1140, True, [enc(0x0008, 0x1150, b"UI", b"1.2", True)]))
bases = [base, make(4, 5, ts=IMPL, wc=100, ww=200, pos=(0, 0, 1), iop=IOP_AX, inst=3,
                    extra_before=seq_undefined(0x0008, 0x1140, False, [enc(0x0008, 0x1150, b"UI", b"1.2", False)]))]
fr = random.Random(7)
bad = []
n_ok = n_err = 0
with tempfile.TemporaryDirectory() as t:
    f, o = os.path.join(t, "a.dcm"), os.path.join(t, "o.pgm")
    for i in range(1200):
        b = bytearray(bases[i % 2])
        kind = i % 4
        if kind == 0:                                      # flip a few random bytes
            for _ in range(fr.randrange(1, 4)):
                b[fr.randrange(132, len(b))] = fr.randrange(256)
        elif kind == 1:                                    # truncate
            b = b[:fr.randrange(0, len(b))]
        elif kind == 2:                                    # corrupt a length field region near the start
            p = fr.randrange(132, min(len(b) - 4, 400))
            b[p:p + 4] = struct.pack("<I", fr.choice([0, 1, 0xFFFFFFFF, 0x7FFFFFFF, 0x10000, len(b)]))
        else:                                              # insert garbage in the middle
            p = fr.randrange(132, len(b))
            b[p:p] = bytes(fr.randrange(256) for _ in range(fr.randrange(1, 9)))
        open(f, "wb").write(bytes(b))
        try:
            code, out, err = run([f, "-o", o])
        except BaseException as e:                         # a traceback / any uncaught exception
            bad.append((i, kind, repr(e)[:120]))
            continue
        if code == 0:
            n_ok += 1
        elif err.startswith("error:"):
            n_err += 1
        else:
            bad.append((i, kind, err[:120]))
check(f"1200 corrupted files: every one either works or ends with a clean 'error:' ({n_ok} worked, {n_err} refused)",
      not bad, bad[:3])

# ================================================================= 12. the tool really runs under python -I as a script
with tempfile.TemporaryDirectory() as t:
    f, o = os.path.join(t, "a.dcm"), os.path.join(t, "o.pgm")
    open(f, "wb").write(make(3, 4, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]))
    r = subprocess.run([sys.executable, "-I", TOOL, f, "-o", o], capture_output=True, text=True, cwd=t)
    check("runs as `python3 -I scripts/dicom_slice.py`", r.returncode == 0 and read_pgm(o)[3] == list(range(1, 13)), r.stderr)

# ================================================================= 13. REAL series (skipped without data/dicom)
def naive_tag(b, g, e):
    """Find an element in an IMPLICIT VR file by scanning for its tag bytes (independent of the tool)."""
    needle = struct.pack("<HH", g, e)
    p = b.find(needle, 132)
    if p < 0:
        return None
    n = struct.unpack_from("<I", b, p + 4)[0]
    return b[p + 8:p + 8 + n]


REAL = os.path.join(ROOT, "data", "dicom")
real = sorted(d for d in (os.listdir(REAL) if os.path.isdir(REAL) else []) if os.path.isdir(os.path.join(REAL, d)))
if not real:
    print("SKIP  real series: data/dicom/ not present (download with the TCIA steps in docs/dicom.md)")
for d in real:
    folder = os.path.join(REAL, d)
    files = sorted(f for f in os.listdir(folder) if f.endswith(".dcm"))
    # independent: instance -> (z position, raw bytes) read by scanning the implicit-VR file
    meta = []
    for f in files:
        b = open(os.path.join(folder, f), "rb").read()
        pos = [float(x) for x in naive_tag(b, 0x0020, 0x0032).decode().strip().split("\\")]
        iop_ = [float(x) for x in naive_tag(b, 0x0020, 0x0037).decode().strip().split("\\")]
        rows = struct.unpack("<H", naive_tag(b, 0x0028, 0x0010))[0]
        cols = struct.unpack("<H", naive_tag(b, 0x0028, 0x0011))[0]
        signed = struct.unpack("<H", naive_tag(b, 0x0028, 0x0103))[0]
        px = b[len(b) - rows * cols * 2:]                       # PixelData is the last element
        vals = list(struct.unpack("<%d%s" % (rows * cols, "h" if signed else "H"), px))
        meta.append((pos, f, rows, cols, vals))
    # slice order along the true normal (axial, coronal and sagittal series alike), worked out here
    # from the first file's orientation vectors, not by the tool's code
    r_, c_ = iop_[:3], iop_[3:]
    normal = (r_[1] * c_[2] - r_[2] * c_[1], r_[2] * c_[0] - r_[0] * c_[2], r_[0] * c_[1] - r_[1] * c_[0])
    order_z = sorted(meta, key=lambda m: sum(a * b for a, b in zip(m[0], normal)))
    n = len(meta)
    res = []
    for idx in sorted({0, n // 4, n // 2, n - 1}):
        with tempfile.TemporaryDirectory() as t:
            o = os.path.join(t, "o.pgm")
            code, out, err = run([folder, "--index", str(idx), "-o", o])
            w, h, mx, got = read_pgm(o)
            z, f, rows, cols, vals = order_z[idx]
            lo = min(vals)
            off = 0 if lo >= 0 else (1024 if lo >= -1024 else -lo)
            want = [v + off for v in vals]
            res.append(code == 0 and (w, h) == (cols, rows) and got == want)
    check(f"real series {d}: slices 0, n/4, n/2, n-1 equal an independent decode, in order along the true slice normal ({n} slices)", all(res), res)
    secrets = set()
    for f in files[:3]:
        bb = open(os.path.join(folder, f), "rb").read()
        for g, e in ((0x0010, 0x0010), (0x0010, 0x0020), (0x0008, 0x0020), (0x0010, 0x1010)):
            v = naive_tag(bb, g, e)
            if v and v.strip(b"\0 "):
                secrets.add(v.strip(b"\0 ").decode("latin-1"))
    with tempfile.TemporaryDirectory() as t:                  # a neutral folder name: the real one is the patient ID
        neutral = os.path.join(t, "series")
        os.mkdir(neutral)
        for f in files:
            os.symlink(os.path.join(folder, f), os.path.join(neutral, f))
        o = os.path.join(t, "o.pgm")
        code, out, err = run([neutral, "-o", o])
        pg = open(o, "rb").read()
    leaked = [v for v in secrets if len(v) >= 3 and (v in out + err or v.encode() in pg)]
    check(f"real series {d}: names of identifying fields are listed, none of their {len(secrets)} actual values "
          f"(read independently from the files) appears in the output or the PGM",
          code == 0 and "PatientName" in out and not leaked and len(secrets) >= 3, (leaked, len(secrets)))

print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
