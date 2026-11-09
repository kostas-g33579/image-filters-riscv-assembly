#!/usr/bin/env python3
"""Differential test of the C DICOM reader (pico/src/dicom.c) against the Python one (scripts/dicom_slice.py).

Both readers are given the SAME files and must produce the SAME result: the same status (success or
the same error class), the same kept values (exactly: decimals are compared as exact mantissa and
exponent, worked out here by a third, independent routine), the same list of identifying fields
noted as present, the same pixel-data position, and the same verdict from the pixel-tag checks.

The corpus: synthetic files from the independent writer (tests/dicomgen.py) in both encodings that
reach every error class, adversarial number strings, nested sequences; every real file in
data/dicom/ when present; and thousands of randomly corrupted files. A final check requires the
corpus to REACH every error class of both readers, so a path cannot go untested unnoticed.

Usage: python3 tests/test_dicom_c.py      (or: make test-dicom-c)
"""
import importlib.util, os, random, re, struct, subprocess, sys
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ds = load("dicom_slice", os.path.join(ROOT, "scripts", "dicom_slice.py"))
gen = load("dicomgen", os.path.join(ROOT, "tests", "dicomgen.py"))
BIN = os.environ.get("DICOM_C_BIN", os.path.join(ROOT, "build32", "dicom_test"))
QEMU = os.environ.get("QEMU", "qemu-riscv32").split()
IMPL, EXPL = gen.IMPL, gen.EXPL
WS = " \t\n\x0b\x0c\r"

failures = 0


def check(name, ok, detail=""):
    global failures
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))
    failures += not ok


# ------------------------------------------------------------------ the Python side, as a canonical dump
ERR_MAP = [("does not end within", "HEADER_TOO_LARGE"), ("no DICOM preamble", "PREAMBLE"), ("too many elements", "TOO_MANY"),
           ("no Transfer Syntax UID", "NO_TS"), ("transfer syntax", "TS"),
           ("file ends inside an element header", "TRUNC_HEADER"), ("unknown value representation", "BAD_VR"),
           ("sequences nested deeper", "DEPTH"), ("expected a sequence item", "NOT_ITEM"),
           ("pixel data runs past", "PIXEL_TRUNC"), ("runs past the end", "RUNS_PAST"),
           ("pixel data is encapsulated", "ENCAPSULATED"), ("has value representation", "VR_MISMATCH"),
           ("malformed", "MALFORMED"), ("only meta information", "ONLY_META"), ("no pixel data", "NO_PIXEL"),
           ("required tag", "MISSING_TAG"), ("colour", "COLOUR"), ("PhotometricInterpretation", "PHOTOMETRIC"),
           ("frames in one file", "MULTIFRAME"), ("image size", "SIZE"),
           ("inconsistent BitsAllocated", "BITS_INCONSISTENT"), ("BitsAllocated=", "BITS_ALLOC"),
           ("PixelRepresentation=", "PIXEL_REP"), ("pixel data holds", "PIXEL_SHORT")]
ALL_CLASSES = {c for _, c in ERR_MAP}


def classify(msg):
    for sub, cls in ERR_MAP:
        if sub in msg:
            return cls
    raise AssertionError("unclassified Python error message: " + msg)


_DS_GRAMMAR = re.compile(r"^[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]{1,3})?\Z", re.ASCII)


def indep_decimals(raw):
    """Exact (mantissa, exponent) list from the raw bytes of a DS value; ValueError if it is not valid.
    A third routine, written from the grammar, independent of both readers."""
    s = raw.split(b"\0")[0].rstrip(b" \0").decode("latin-1")
    out = []
    for part in s.split("\\"):
        x = part.strip(WS)
        if not x:
            continue
        digits = re.split("[eE]", x)[0]
        if len(x) > 64 or not _DS_GRAMMAR.match(x) or sum(c in "0123456789" for c in digits) > 18:
            raise ValueError(x)
        d = Decimal(x)
        if d == 0:
            out.append((0, 0))
            continue
        sign, dig, exp = d.as_tuple()
        m = int("".join(map(str, dig)))
        while m % 10 == 0:
            m //= 10
            exp += 1
        out.append((-m if sign else m, exp))
    return out


disagreements = []
_orig_decode = ds._decode


def _wrapped_decode(v, kind, where):
    if kind != "DS":
        return _orig_decode(v, kind, where)
    try:
        want = indep_decimals(v)
    except ValueError:
        want = None
    try:
        _orig_decode(v, kind, where)
        tool_ok = True
    except ds.DicomError:
        tool_ok = False
    if tool_ok != (want is not None):
        disagreements.append((bytes(v), tool_ok, want))
    if not tool_ok:
        raise ds.DicomError("%s: value of an expected tag is malformed" % where)
    return want


ds._decode = _wrapped_decode      # the tool's own accept/reject decision is kept; the values become exact decimals

STR_FIELDS = ["TransferSyntax", "ImageType", "SOPClass", "Modality", "Manufacturer", "ManufacturerModelName",
              "PatientIdentityRemoved", "ScanningSequence", "SequenceVariant", "MRAcquisitionType",
              "SeriesInstanceUID", "Photometric", "SoftwareVersions", "ProtocolName", "BurnedInAnnotation"]
INT_FIELDS = ["SamplesPerPixel", "PlanarConfiguration", "Rows", "Columns", "BitsAllocated", "BitsStored", "HighBit",
              "PixelRepresentation", "NumberOfFrames", "InstanceNumber"]
DEC_FIELDS = ["SliceThickness", "PixelSpacing", "ImagePositionPatient", "ImageOrientationPatient", "RepetitionTime",
              "EchoTime", "InversionTime", "FlipAngle", "MagneticFieldStrength", "RescaleSlope", "RescaleIntercept",
              "WindowCenter", "WindowWidth"]


def esc(s):
    if len(s) > 64:
        return "~LONG"
    return "".join(c if 0x20 <= ord(c) <= 0x7e and c != "\\" else "\\x%02x" % ord(c) for c in s)


def py_dump(b):
    """The same dump the C harness prints, from the Python reader. Returns a dict."""
    avail = None
    if isinstance(b, tuple):                    # (whole file, number of bytes held): the streaming case
        b, avail = b
    try:
        img = ds.parse(b if avail is None else b[:avail], "f", total=len(b))
    except ds.DicomError as e:
        return {"STATUS": "ERR:" + classify(str(e))}
    d = {"STATUS": "OK"}
    try:
        ds.check_image(img, "f")
        d["CHECK"] = "OK"
    except ds.DicomError as e:
        d["CHECK"] = "ERR:" + classify(str(e))
    d["explicit"] = str(int(img.explicit))
    d["pix_off"], d["pix_len"] = str(img.pix_off), str(img.pix_len)
    d["identifying"] = ",".join(sorted(img.identifying))
    t = img.tags
    for f in STR_FIELDS:
        if f in t:
            d["str:" + f] = esc(t[f])
    if "ContrastBolusAgent" in t:
        d["int:ContrastDeclared"] = str(int(len(t["ContrastBolusAgent"]) > 0))
    for f in INT_FIELDS:
        if t.get(f) is not None:
            d["int:" + f] = str(t[f])
    for f in DEC_FIELDS:
        v = t.get(f)
        if v:
            d["dec:" + f] = "%d:%s" % (min(len(v), 255), ",".join("%d@%d" % x for x in v[:6]))   # the C count saturates at 255
    return d


def parse_c_output(text):
    recs, cur = [], {}
    for line in text.split("\n"):
        if line == "END":
            recs.append(cur)
            cur = {}
        elif "=" in line:
            k, v = line.split("=", 1)
            cur[k] = v
    return recs


def c_normalise(d):
    if "identifying" in d:
        d["identifying"] = ",".join(sorted(x for x in d["identifying"].split(",") if x))
    return d


def run_c(files, chunk=600):
    out = []
    for i in range(0, len(files), chunk):
        blob = b"".join(struct.pack("<I", len(f)) + f if not isinstance(f, tuple) else
                        struct.pack("<II", len(f[0]) | 0x80000000, f[1]) + f[0][:f[1]] for f in files[i:i + chunk])
        r = subprocess.run(QEMU + [BIN], input=blob, capture_output=True, timeout=600)
        if r.returncode != 0 or r.stderr:
            raise RuntimeError("C harness failed: rc=%s %s" % (r.returncode, r.stderr[:200]))
        recs = parse_c_output(r.stdout.decode("latin-1"))
        if len(recs) != len(files[i:i + chunk]):
            raise RuntimeError("C harness returned %d records for %d files" % (len(recs), len(files[i:i + chunk])))
        out += [c_normalise(x) for x in recs]
    return out


def compare(label, files):
    """Run both readers on every file; report mismatches. Returns (n, mismatches, status counts)."""
    c_res = run_c(files)
    bad, counts = [], {}
    for i, (f, c) in enumerate(zip(files, c_res)):
        p = py_dump(f)
        for k in ("STATUS", "CHECK"):
            if k in p:
                counts[p[k]] = counts.get(p[k], 0) + 1
        if p != c:
            diff = sorted(k for k in set(p) | set(c) if p.get(k) != c.get(k))
            bad.append((i, [(k, p.get(k), c.get(k)) for k in diff[:3]]))
    check(f"{label}: {len(files)} files give identical results in C and Python", not bad, str(bad[:2]))
    return counts


# ------------------------------------------------------------------ corpus 1: synthetic files
def raw_el(g, e, vr, val, explicit):
    return gen.enc(g, e, vr, val, explicit)


def corpus_synthetic():
    files = []
    rng = random.Random(11)
    for ts in (IMPL, EXPL):
        ex = ts == EXPL
        # plain variety of valid files
        for rows, cols in [(1, 1), (1, 7), (7, 1), (3, 5), (16, 16), (5, 4)]:
            for alloc, stored, high in [(16, 16, 15), (16, 12, 11), (16, 12, 15), (8, 8, 7)]:
                for signed in (False, True):
                    vals = [rng.randrange(1 << min(stored, 12)) for _ in range(rows * cols)]
                    files.append(gen.make(rows, cols, vals, ts=ts, alloc=alloc, stored=stored, high=high, signed=signed,
                                          wc=[40, 80], ww=[400, 100], slope=1, inter=-1024, pos=(1.5, -2.25, 3),
                                          iop=(1, 0, 0, 0, 1, 0), inst=7, removed=b"YES", burned=b"NO"))
        files.append(gen.make(4, 5, ts=ts, photometric=b"MONOCHROME1"))
        files.append(gen.make(4, 5, ts=ts, ident=False))
        files.append(gen.make(4, 5, ts=ts, modality=b"CT"))
        # raw technical tags with adversarial text
        raws = [b"1.5", b"+1.5", b"-.5", b"5.", b"1E3", b"1.5e-3", b" 2 ", b"0", b"0.00", b"-0", b"0.9765625",
                b"1\\2\\3", b"1\\\\3", b"\\", b"", b"   ", b"nan", b"inf", b"-inf", b"1_0", b"1,5", b".", b"e5", b"1e",
                b"1e1234", b"1e999", b"1e-999", b"0x10", b"1.2.3", b"--1", b"1 2", b"12345678901234567",
                b"123456789012345678", b"1234567890123456789", b"0.000000000000000001", b"00012.50000",
                "١٢".encode("utf-8"), b"1.5\x00junk", b"1.5 \x00", b"\t7\n", b"9" * 70, b"3.14159265358979",
                b"1.5\\\\\\", b" \\ 4 ", b"\\".join(b"%d" % i for i in range(1, 9)), b"\\".join([b"7"] * 300),
                b"\\".join([b"2"] * 255), b"\\".join([b"2"] * 256), b"\\".join([b"3"] * 6), b"\\".join([b"3"] * 7), b"\r7\r", b" 7\r\n", b"\x0b7\x0c"]
        for r in raws:
            files.append(gen.make(3, 3, ts=ts, extra_before=raw_el(0x18, 0x80, b"DS", r, ex) +
                                  raw_el(0x18, 0x81, b"DS", b"7.25", ex)))
        # integer strings and strings
        for r in [b"7", b"+7", b"-7", b"007", b"123456789012", b"1234567890123", b"", b" ", b"7\\9", b"x", b"1.0", b"1_0",
                  b"\\7", b" \\ 8", b"-", b"+", "١".encode()]:
            files.append(gen.make(3, 3, ts=ts, inst=None, extra_before=raw_el(0x20, 0x13, b"IS", r, ex)))
            files.append(gen.make(3, 3, ts=ts, extra_before=raw_el(0x28, 0x08, b"IS", r, ex)))
        for r in [b"", b" ", b"SIEMENS", b"A" * 64, b"A" * 65, b"A" * 300, b"  pad  ", b"x\x00y", b"\xe9\xff\x01 z",
                  b"back\\slash", b"tab\there", b"\x85\xa0nb\xa0", b"\rSIEMENS\r", b"\r\r", b"A" * 255, b"A" * 256, b"A" * 257, b"A" * 512, b"\n" * 256]:
            files.append(gen.make(3, 3, ts=ts, extra_before=raw_el(0x8, 0x1090, b"LO", r, ex) +
                                  raw_el(0x18, 0x1030, b"LO", r, ex) + raw_el(0x18, 0x1020, b"LO", r, ex) +
                                  raw_el(0x18, 0x10, b"LO", r, ex) + raw_el(0x18, 0x20, b"CS", r, ex)))
        # repeated tags: the last one wins, an empty IS replaces an earlier value
        files.append(gen.make(3, 3, ts=ts, extra_before=raw_el(0x28, 0x08, b"IS", b"1", ex) +
                              raw_el(0x28, 0x08, b"IS", b"", ex)))
        files.append(gen.make(3, 3, ts=ts, extra_before=raw_el(0x18, 0x80, b"DS", b"5", ex) +
                              raw_el(0x18, 0x80, b"DS", b"", ex)))
        files.append(gen.make(3, 3, ts=ts, extra_before=raw_el(0x18, 0x80, b"DS", b"5", ex) +
                              raw_el(0x18, 0x80, b"DS", b"6", ex)))
        # sequences, private tags, nesting
        inner = gen.enc(0x8, 0x1150, b"UI", b"1.2.3", ex) + gen.enc(0x28, 0x10, b"US", gen.US(99), ex)
        nested = gen.seq_undefined(0x40, 0xA730, ex, [inner, inner], item_undefined=True)
        seqs = (gen.seq_undefined(0x8, 0x1140, ex, [inner + nested, inner], item_undefined=True) +
                gen.seq_undefined(0x8, 0x1111, ex, [inner], item_undefined=False) +
                gen.seq_defined(0x8, 0x1115, ex, [inner]) + gen.enc(0x9, 0x10, b"LO", b"PRIVATE", ex) +
                gen.enc(0x19, 0x10FF, b"UN", b"\x01\x02\x03", ex) + gen.enc(0x18, 0x80, b"DS", b"11", ex))
        files.append(gen.make(3, 4, ts=ts, extra_before=seqs))
        # one level below the limit, at the limit, and past it
        for depth in (14, 15, 16, 17, 18, 40):
            deep = gen.enc(0x8, 0x1150, b"UI", b"1.2", ex)
            for _ in range(depth):
                deep = gen.seq_undefined(0x8, 0x1140, ex, [deep], item_undefined=True)
            files.append(gen.make(2, 2, ts=ts, extra_before=deep))
        # error classes, one or more of each
        files.append(gen.make(4, 5, ts=ts, drop=("Rows",)))
        files.append(gen.make(4, 5, ts=ts, spp=3))
        files.append(gen.make(4, 5, ts=ts, photometric=b"RGB"))
        files.append(gen.make(4, 5, ts=ts, photometric=b"MONOCHROME2" + b"X" * 80))
        files.append(gen.make(4, 5, ts=ts, nframes=10))
        files.append(gen.make(4, 5, ts=ts, nframes=1))
        files.append(gen.make(4, 5, ts=ts, nframes=0))
        files.append(gen.make(0, 5, vals=[], ts=ts, pix_override=b"\0" * 8))
        files.append(gen.make(60000, 60000, vals=[], ts=ts, pix_override=b"\0" * 8))
        files.append(gen.make(10000, 10000, vals=[], ts=ts, pix_override=b"\0" * 8))
        files.append(gen.make(10001, 10000, vals=[], ts=ts, pix_override=b"\0" * 8))
        for alloc in (12, 32, 1, 24):
            files.append(gen.make(2, 2, ts=ts, alloc=alloc, stored=alloc, pix_override=b"\0" * 16))
        files.append(gen.make(2, 2, ts=ts, alloc=16, stored=17, high=16, pix_override=b"\0" * 16))
        files.append(gen.make(2, 2, ts=ts, alloc=16, stored=0, high=0, pix_override=b"\0" * 16))
        files.append(gen.make(2, 2, ts=ts, alloc=16, stored=12, high=10, pix_override=b"\0" * 16))
        files.append(gen.make(2, 2, ts=ts, alloc=16, stored=12, high=16, pix_override=b"\0" * 16))
        files.append(gen.make(2, 2, ts=ts, alloc=8, stored=8, high=8, pix_override=b"\0" * 16))
        files.append(gen.make(4, 5, ts=ts, pix_override=bytes(range(30))))      # too short
        files.append(gen.make(4, 5, ts=ts, pix_override=bytes(range(40))))      # exact
        files.append(gen.make(4, 5, ts=ts, pix_override=bytes(range(60))))      # padded
        files.append(gen.make(4, 5, ts=ts, pix_undefined=True))
        for tsuid in ["1.2.840.10008.1.2.4.50", "1.2.840.10008.1.2.1.99", "1.2.840.10008.1.2.2", "1.2.3.4",
                      "1.2.840.10008.1.2.5", "", "1.2.840.10008.1.2.1 ", "x" * 200]:
            files.append(gen.make(4, 5, ts=ts, tsuid=tsuid))
        # PixelRepresentation other than 0 / 1
        pr = (struct.pack("<HH2sH", 0x28, 0x103, b"US", 2) if ex else struct.pack("<HHI", 0x28, 0x103, 2))
        base_pr = gen.make(2, 2, ts=ts)
        assert base_pr.count(pr + gen.US(0)) == 1
        for v in (2, 3, 255, 65535):
            files.append(base_pr.replace(pr + gen.US(0), pr + gen.US(v), 1))
        # damage
        good = gen.make(4, 5, ts=ts, wc=100, ww=200, pos=(0, 0, 1), iop=(1, 0, 0, 0, 1, 0), inst=3)
        for cut in (0, 100, 131, 132, 133, 139, 140, 200, 300, len(good) - 40, len(good) - 1):
            files.append(good[:cut])
        files.append(good[:128] + b"XXXX" + good[132:])
        files.append(gen.make(2, 2, ts=ts, extra_before=struct.pack("<HH2sH", 8, 0x90, b"ZZ", 4) + b"abcd"))
        files.append(gen.make(2, 2, ts=ts, extra_before=struct.pack("<HHI", 8, 0x1140, 0x7FFFFFFF)))
        files.append(gen.make(2, 2, ts=ts, extra_before=struct.pack("<HHI", 8, 0x1140, 100000)))
        files.append(gen.make(2, 2, ts=ts, extra_before=struct.pack("<HHI", 8, 0x1140, 0xFFFFFFFF)))   # undefined, no items
        files.append(gen.make(2, 2, ts=ts, extra_before=struct.pack("<HHI", 8, 0x1140, 0xFFFFFFFF) +
                              struct.pack("<HHI", 0x10, 0x10, 0) + struct.pack("<HHI", 0xFFFE, 0xE0DD, 0)))
        files.append(gen.make(2, 2, ts=ts, extra_before=struct.pack("<HHI", 8, 0x1140, 0xFFFFFFFF) +
                              struct.pack("<HHI", 0xFFFE, 0xE000, 5000) + b"\0" * 20))
        files.append(gen.make(2, 2, ts=ts, extra_before=struct.pack("<HHI", 8, 0x1140, 0xFFFFFFFF) +
                              struct.pack("<HHI", 0xFFFE, 0xE000, 0xFFFFFFFF) + struct.pack("<HHI", 0x10, 0x10, 9000)))
        # Rows/columns of the wrong type, and a one-byte US
        files.append(gen.make(2, 2, ts=ts).replace(struct.pack("<HH2sH", 0x28, 0x10, b"US", 2) + gen.US(2),
                                                   struct.pack("<HH2sH", 0x28, 0x10, b"US", 1) + b"\x02", 1))
        files.append(gen.make(2, 2, ts=ts).replace(struct.pack("<HH2sH", 0x28, 0x10, b"US", 2) + gen.US(2),
                                                   struct.pack("<HH2sHI", 0x28, 0x10, b"UL", 4, 2)[:8] + struct.pack("<I", 2), 1))
        files.append(gen.make(2, 2, ts=ts).replace(b"\x00\x28\x08\x00", b"\x00\x28\x08\x00", 1))
    # only meta information; meta without a transfer syntax; data set without pixels
    meta_only = gen.make(2, 2)
    i = meta_only.index(b"DICM") + 4
    only_meta = meta_only[:i] + gen.enc(0x2, 0x10, b"UI", EXPL.encode(), True)
    files.append(only_meta)
    files.append(meta_only[:i] + gen.enc(0x2, 0x2, b"UI", b"1.2.3", True) + gen.enc(0x8, 0x60, b"CS", b"MR", True))
    files.append(meta_only[:i] + gen.enc(0x2, 0x10, b"UI", EXPL.encode(), True) + gen.enc(0x8, 0x60, b"CS", b"MR", True))
    files.append(meta_only[:i] + gen.enc(0x2, 0x10, b"UI", IMPL.encode(), True) + gen.enc(0x8, 0x60, b"CS", b"MR", False))
    files.append(b"")
    files.append(b"DICM")
    files.append(b"\0" * 128 + b"DICM")
    files.append(b"\0" * 131)
    return files


def corpus_too_many():
    """The element-count limit (2,000,000 counted elements: each element read, the pixel data included),
    probed beyond it and at its exact boundary. Each file is 16 MB."""
    body = b"".join(struct.pack("<HHI", 0x0009, 0x0001, 0) for _ in range(2_000_001))
    base = gen.make(2, 2, ts=IMPL)
    meta_end = base.index(struct.pack("<HH", 0x0008, 0x0008))
    files = [base[:meta_end] + body + base[meta_end:], base[:meta_end] + body[:8 * 1000] + base[meta_end:]]
    pre = b"\0" * 128 + b"DICM" + gen.enc(0x2, 0x10, b"UI", IMPL.encode(), True)
    dummy = struct.pack("<HHI", 0x0009, 0x0001, 0)
    pixel = struct.pack("<HHI", 0x7FE0, 0x0010, 0)
    # counted elements = 1 (transfer syntax) + n dummies + 1 (pixel data)
    for n in (1_999_997, 1_999_998, 1_999_999):             # 1,999,999 / 2,000,000 (allowed) / 2,000,001 (too many)
        files.append(pre + dummy * n + pixel)
    return files


def corpus_fuzz(n, seed):
    rng = random.Random(seed)
    bases = []
    for ts in (IMPL, EXPL):
        ex = ts == EXPL
        inner = gen.enc(0x8, 0x1150, b"UI", b"1.2", ex)
        bases.append(gen.make(4, 5, ts=ts, wc=100, ww=200, pos=(0, 0, 1), iop=(1, 0, 0, 0, 1, 0), inst=3, slope=1,
                              inter=-1024, removed=b"YES",
                              extra_before=gen.seq_undefined(0x8, 0x1140, ex, [inner]) +
                              raw_el(0x18, 0x80, b"DS", b"1760", ex) + raw_el(0x18, 0x1030, b"LO", b"t1 axial", ex)))
    out = []
    for i in range(n):
        b = bytearray(bases[i % 2])
        kind = i % 5
        if kind == 0:
            for _ in range(rng.randrange(1, 4)):
                b[rng.randrange(132, len(b))] = rng.randrange(256)
        elif kind == 1:
            b = b[:rng.randrange(0, len(b))]
        elif kind == 2:
            p = rng.randrange(132, min(len(b) - 4, 500))
            b[p:p + 4] = struct.pack("<I", rng.choice([0, 1, 2, 0xFFFFFFFF, 0x7FFFFFFF, 0x10000, len(b), 0xFFFE]))
        elif kind == 3:
            p = rng.randrange(132, len(b))
            b[p:p] = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 9)))
        else:
            p = rng.randrange(132, min(len(b), 700))
            b[p:p + rng.randrange(1, 12)] = b""
        out.append(bytes(b))
    return out


def corpus_real():
    root = os.path.join(ROOT, "data", "dicom")
    files = []
    if not os.path.isdir(root):
        return files
    for d in sorted(os.listdir(root)):
        p = os.path.join(root, d)
        if os.path.isdir(p):
            for f in sorted(os.listdir(p)):
                files.append(open(os.path.join(p, f), "rb").read())      # includes the LICENSE file: not DICOM
    return files


def partial_points(f, full, rng):
    """Where to cut a file: around the start of its pixel data, inside the header, and at both ends."""
    n = len(f)
    pts = {0, 1, 100, 131, 132, 133, 140, n, n - 1, rng.randrange(0, n + 1)}
    if "pix_off" in full:
        po = int(full["pix_off"])
        pts |= {po, po + 1, po - 1, po - 8, po - 12, po + 17, min(n, po + 1000), rng.randrange(132, max(133, po))}
    else:
        pts |= {rng.randrange(0, n + 1) for _ in range(5)}
    return sorted(x for x in pts if 0 <= x <= n)


def check_partial(label, files):
    """Streaming parse (only the first `avail` bytes held): C == Python on every cut, and an independent oracle:
    for a file that parses in full, any cut at or after the start of the pixel data gives exactly the full result,
    and any earlier cut (that still holds a preamble) gives exactly HEADER_TOO_LARGE."""
    rng = random.Random(7)
    items, fulls = [], []
    for f in files:
        full = py_dump(f)
        for a in partial_points(f, full, rng):
            items.append((f, a))
            fulls.append(full)
    counts = compare(label + " (cut files)", items)
    bad = []
    for (f, a), full, c in zip(items, fulls, run_c(items)):
        if full.get("STATUS") != "OK":
            continue
        po = int(full["pix_off"])
        want = full if a >= po else ({"STATUS": "ERR:PREAMBLE"} if a < 132 else {"STATUS": "ERR:HEADER_TOO_LARGE"})
        if c != want:
            bad.append((len(f), a, po, c.get("STATUS"), want.get("STATUS")))
    check(label + ": a cut at or after the pixel data gives the full result, an earlier cut gives HEADER_TOO_LARGE",
          not bad, str(bad[:3]))
    return counts


def main():
    if not os.path.exists(BIN):
        sys.exit("build the C reader first: make build32/dicom_test (or make test-dicom-c)")
    counts = {}

    def merge(c):
        for k, v in c.items():
            counts[k] = counts.get(k, 0) + v

    syn = corpus_synthetic()
    merge(compare("synthetic corpus", syn))
    merge(compare("element-count limit", corpus_too_many()))
    real = corpus_real()
    if real:
        merge(compare("real files (all of data/dicom, 12 series)", real))
    else:
        print("SKIP  real files: data/dicom/ not present")
    for seed, n in ((1, 2500), (2, 2500), (3, 2500)):
        merge(compare(f"corrupted files, seed {seed}", corpus_fuzz(n, seed)))

    good = [f for f in syn if py_dump(f).get("STATUS") == "OK"]
    merge(check_partial("synthetic", good[:200] + syn[::7]))
    if real:
        merge(check_partial("real", [f for i, f in enumerate(real) if i % 120 == 0]))
    merge(check_partial("corrupted", corpus_fuzz(1200, 11)))

    check("the tool's decimal grammar and the independent one agree on every value seen (accept and reject)",
          not disagreements, str(disagreements[:2]))
    reached = {k.split(":", 1)[1] for k in counts if k.startswith("ERR:")}
    check("the corpus reaches every one of the %d error classes" % len(ALL_CLASSES), reached >= ALL_CLASSES,
          "never reached: " + ", ".join(sorted(ALL_CLASSES - reached)))
    print("  status counts:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    sys.exit(1 if failures else 0)


main()
