#!/usr/bin/env python3
"""Tests of the NIfTI side of the on-device anonymiser (pico/src/nifti.c).

What is checked, and against what:

 * The 352-byte header, byte for byte, against a header built here straight from the NIfTI-1 layout with
   struct (offsets from the specification), using the geometry the already-verified Python Stack produces.
   The layout itself is checked against a header written by other software (the real BraTS NIfTI file).
 * The voxel bytes: for every slice, a hash of the re-packed bytes against the same re-packing worked out
   independently here (shift by High Bit + 1 - Bits Stored, mask to Bits Stored, sign-extend if signed).
 * Refusals: MONOCHROME1, a bit depth / signedness / size that changes between slices, a rescale slope or
   intercept that changes between slices, a zero slope, a volume too large for NIfTI's 16-bit dimensions.
 * A second opinion from the repo's own strict NIfTI reader (scripts/nifti_slice.py): header + expected
   voxels written to a file must read back with the right size, type, voxel sizes, slope, intercept and values.

Usage: python3 tests/test_nifti_c.py      (or: make test-nifti-c)
"""
import gzip, importlib.util, io, os, random, struct, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ds = load("dicom_slice", os.path.join(ROOT, "scripts", "dicom_slice.py"))
gen = load("dicomgen", os.path.join(ROOT, "tests", "dicomgen.py"))
ns = load("nifti_slice", os.path.join(ROOT, "scripts", "nifti_slice.py"))
BIN = os.environ.get("NIFTI_C_BIN", os.path.join(ROOT, "build32", "nifti_test"))
QEMU = os.environ.get("QEMU", "qemu-riscv32").split()
failures = 0
DESCRIP = b"sanitised DICOM: pixels and geometry only"


def check(name, ok, detail=""):
    global failures
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))
    failures += not ok


# ------------------------------------------------------------------ the oracle, worked out here
def fnv(data):
    h = 2166136261
    for b in data:
        h = ((h ^ b) * 16777619) & 0xFFFFFFFF
    return h


def convert(pix, alloc, stored, high, signed, n):
    """Re-pack n samples: shift down to the stored bits, mask, sign-extend, little endian in the container type."""
    if alloc == 16 and stored == 16 and high == 15:
        return bytes(pix[:2 * n])                              # definitionally the identity (two's complement or not)
    if alloc == 8 and stored == 8 and high == 7:
        return bytes(pix[:n])
    shift, mask, out = high + 1 - stored, (1 << stored) - 1, bytearray()
    for i in range(n):
        raw = pix[i] if alloc == 8 else pix[2 * i] | (pix[2 * i + 1] << 8)
        v = (raw >> shift) & mask
        if signed and v & (1 << (stored - 1)):
            v -= 1 << stored
        out += struct.pack("<b" if (alloc == 8 and signed) else "<B" if alloc == 8 else "<h" if signed else "<H", v)
    return bytes(out)


def expected_header(res, dt, bitpix, slope, inter):
    h = bytearray(352)
    struct.pack_into("<i", h, 0, 348)
    struct.pack_into("<8h", h, 40, 3, res["cols"], res["rows"], res["count"], 1, 1, 1, 1)
    struct.pack_into("<2h", h, 70, dt, bitpix)
    struct.pack_into("<8f", h, 76, 1.0, res["pixdim"][0], res["pixdim"][1], res["pixdim"][2], 0, 0, 0, 0)
    struct.pack_into("<f", h, 108, 352.0)
    struct.pack_into("<2f", h, 112, slope, inter)
    h[123] = 2
    h[148:148 + len(DESCRIP)] = DESCRIP
    struct.pack_into("<2h", h, 252, 0, 1)
    struct.pack_into("<12f", h, 280, *res["affine"])
    h[344:348] = b"n+1\0"
    return bytes(h)


def datatype(alloc, signed):
    return (256 if signed else 2) if alloc == 8 else (4 if signed else 512)


def py_expected(files, hash_slices=None):
    """What the C side must print for this series."""
    d, st, first = {}, ds.Stack(), None
    for i, f in enumerate(files):
        try:
            img = ds.parse(f, "f")
        except ds.DicomError:
            d[f"F{i}"] = "ERR:PARSE"
            return d, None
        try:
            ds.check_image(img, "f")
        except ds.DicomError:
            d[f"F{i}"] = "ERR:CHECK"
            return d, None
        try:
            st.add(img)
        except ds.GeomError as e:
            d[f"F{i}"] = "ERR:" + e.cls
            return d, None
        t = img.tags
        alloc, signed = t["BitsAllocated"], bool(t["PixelRepresentation"])
        stored = t.get("BitsStored", alloc)
        high = t.get("HighBit", stored - 1)
        slope = (t.get("RescaleSlope") or [1.0])[0]
        inter = (t.get("RescaleIntercept") or [0.0])[0]
        if t["Photometric"] != "MONOCHROME2":
            d[f"F{i}"] = "ERR:PHOTOMETRIC"
            return d, None
        if slope == 0:
            d[f"F{i}"] = "ERR:RESCALE"
            return d, None
        fmt = (t["Rows"], t["Columns"], alloc, stored, high, signed)
        if first is None:
            first = (fmt, slope, inter)
        else:
            if fmt != first[0]:
                d[f"F{i}"] = "ERR:FORMAT"
                return d, None
            if (slope, inter) != first[1:]:
                d[f"F{i}"] = "ERR:RESCALE"
                return d, None
        d[f"F{i}"] = "OK"
        if hash_slices is None or i in hash_slices:
            n = t["Rows"] * t["Columns"]
            d[f"S{i}"] = "%08x" % fnv(convert(f[img.pix_off:], alloc, stored, high, signed, n))
    res = st.finish()
    (rows, cols, alloc, stored, high, signed), slope, inter = first
    if cols > 32767 or rows > 32767 or res["count"] > 32767:
        d["RESULT"] = "ERR:TOO_LARGE"
        return d, None
    d["HEADER"] = expected_header(res, datatype(alloc, signed), alloc, slope, inter).hex()
    return d, (res, first)


def run_c(series):
    blob = b""
    for files in series:
        blob += struct.pack("<I", len(files)) + b"".join(struct.pack("<I", len(f)) + f for f in files)
    r = subprocess.run(QEMU + [BIN], input=blob, capture_output=True, timeout=900)
    if r.returncode != 0 or r.stderr:
        raise RuntimeError("C harness failed: rc=%s %s" % (r.returncode, r.stderr[:200]))
    out, cur = [], None
    for line in r.stdout.decode().split("\n"):
        if line == "SERIES":
            cur = {}
        elif line == "END":
            out.append(cur)
        elif "=" in line and cur is not None:
            k, v = line.split("=", 1)
            cur[k] = v
    if len(out) != len(series):
        raise RuntimeError("C harness returned %d results for %d series" % (len(out), len(series)))
    return out


def vals_for(alloc, stored, signed, rows, cols, rng):
    lo, hi = (-(1 << (stored - 1)), (1 << (stored - 1)) - 1) if signed else (0, (1 << stored) - 1)
    return lambda k: [rng.randint(lo, hi) for _ in range(rows * cols)]


def main():
    if not os.path.exists(BIN):
        sys.exit("build the C NIfTI harness first: make build32/nifti_test (or make test-nifti-c)")
    rng = random.Random(77)
    cases = []                                    # (label, files, expected outcome string or None)
    R, C = 3, 5

    # ---- every data type and bit layout, with garbage in the unused bits, both orientations of signedness
    for alloc, stored, high in [(8, 8, 7), (16, 16, 15), (16, 12, 11), (16, 12, 15), (16, 10, 9), (16, 15, 14), (16, 9, 15)]:
        for signed in (False, True):
            for orient in ("axial", "oblique 2"):
                files = gen.stack(5, orient, rows=R, cols=C, alloc=alloc, stored=stored, high=high, signed=signed,
                                  vals=vals_for(alloc, stored, signed, R, C, rng))
                cases.append((f"{alloc} bits, {stored} stored, high bit {high}, {'signed' if signed else 'unsigned'}, {orient}", files, "OK"))
    # ---- rescale slope and intercept
    for slope, inter in [(None, None), (1, 0), (2.5, -1024), (0.015625, 3), (1, 1000), (None, 5), (7, None), (1e-4, 0), (65535, 0)]:
        kw = {}
        if slope is not None:
            kw["slope"] = slope
        if inter is not None:
            kw["inter"] = inter
        cases.append((f"rescale slope {slope} intercept {inter}", gen.stack(4, rows=R, cols=C, **kw), "OK"))
    cases.append(("slope changes on the last slice", gen.stack(4, rows=R, cols=C, slope=lambda k: 1 if k < 3 else 2), "RESCALE"))
    cases.append(("slope changes on the second slice", gen.stack(4, rows=R, cols=C, slope=lambda k: 1 if k < 1 else 2), "RESCALE"))
    cases.append(("intercept changes", gen.stack(4, rows=R, cols=C, inter=lambda k: 0 if k < 2 else -1), "RESCALE"))
    cases.append(("slope tag appears on a later slice only", gen.stack(4, rows=R, cols=C, slope=lambda k: 2 if k == 3 else 1), "RESCALE"))
    cases.append(("zero slope", gen.stack(3, rows=R, cols=C, slope=0), "RESCALE"))
    # ---- refusals
    cases.append(("MONOCHROME1", gen.stack(3, rows=R, cols=C, photometric=b"MONOCHROME1"), "PHOTOMETRIC"))
    cases.append(("MONOCHROME1 on a later slice", gen.stack(4, rows=R, cols=C, photometric=lambda k: b"MONOCHROME1" if k == 2 else b"MONOCHROME2"), "PHOTOMETRIC"))
    cases.append(("bits stored change on a later slice", gen.stack(4, rows=R, cols=C, stored=lambda k: 16 if k < 2 else 12, high=lambda k: 15 if k < 2 else 11), "FORMAT"))
    cases.append(("high bit changes", gen.stack(4, rows=R, cols=C, stored=12, high=lambda k: 11 if k < 3 else 15), "FORMAT"))
    cases.append(("signedness changes", gen.stack(4, rows=R, cols=C, signed=lambda k: k == 2), "FORMAT"))
    # gaps found by mutation testing: each field must be checked on its own, and the two pixel spacings must not be swapped
    cases.append(("only bits stored changes (high bit and allocation the same)", gen.stack(4, rows=R, cols=C, stored=lambda k: 16 if k < 2 else 12, high=15), "FORMAT"))
    cases.append(("only bits allocated changes (stored and high bit the same)", gen.stack(4, rows=R, cols=C, alloc=lambda k: 16 if k < 2 else 8, stored=8, high=7), "FORMAT"))
    for orient in ("axial", "sagittal", "coronal", "oblique 1"):
        cases.append((f"anisotropic pixels 0.5 x 0.8 mm, 2.5 mm slices, {orient}", gen.stack(5, orient, rows=R, cols=C, ps=(0.5, 0.8), dz=2.5), "OK"))
        cases.append((f"anisotropic pixels 1.25 x 0.9375 mm, 0.7 mm slices, {orient}", gen.stack(5, orient, rows=R, cols=C, ps=(1.25, 0.9375), dz=0.7), "OK"))
    cases.append(("bits allocated change", gen.stack(3, rows=R, cols=C, alloc=lambda k: 16 if k < 2 else 8), "FORMAT"))
    cases.append(("a volume wider than NIfTI's 16-bit dimension allows", gen.stack(2, rows=1, cols=33000), "TOO_LARGE"))
    cases.append(("exactly 32767 wide (allowed)", gen.stack(2, rows=1, cols=32767), "OK"))
    cases.append(("exactly 32768 wide (one too many)", gen.stack(2, rows=1, cols=32768), "TOO_LARGE"))
    cases.append(("exactly 32767 tall (allowed)", gen.stack(2, rows=32767, cols=1), "OK"))
    cases.append(("exactly 32768 tall (one too many)", gen.stack(2, rows=32768, cols=1), "TOO_LARGE"))
    # the slice count is a signed 16-bit field too: 32767 slices fit, 32768 do not (1 x 1 pixel slices, ~400 bytes each)
    cases.append(("32767 slices (allowed)", gen.stack(32767, rows=1, cols=1, dz=0.01), "OK"))
    cases.append(("32768 slices (one too many)", gen.stack(32768, rows=1, cols=1, dz=0.01), "TOO_LARGE"))
    # ---- the 12 real series
    real = []
    root = os.path.join(ROOT, "data", "dicom")
    if os.path.isdir(root):
        for d in sorted(os.listdir(root)):
            p = os.path.join(root, d)
            if os.path.isdir(p):
                imgs, _ = ds.load_series(p)
                ordered, _, _, _ = ds.order_slices(imgs)
                files = [open(i.path, "rb").read() for i in ordered]
                real.append((d, files))
                cases.append((f"real series {d}", files, "OK"))
    else:
        print("SKIP  real series: data/dicom/ not present")

    # ---------------------------------------------------------------- run C and compare with the oracle
    c_res = run_c([f for _, f, _ in cases])
    bad, expect_bad, full = [], [], []
    for (label, files, expect), c in zip(cases, c_res):
        # hashing every voxel of the real series in Python would take minutes: first, middle and last slices
        hs = {0, len(files) // 2, len(files) - 1} if label.startswith("real series") else None
        p, extra = py_expected(files, hs)
        cc = {k: v for k, v in c.items() if hs is None or not k.startswith("S") or int(k[1:]) in hs}
        if p != cc:
            diff = sorted(k for k in set(p) | set(cc) if p.get(k) != cc.get(k))
            bad.append((label, [(k, str(p.get(k))[:40], str(cc.get(k))[:40]) for k in diff[:2]]))
        status = "OK" if "HEADER" in p else (p.get("RESULT") or next((v for v in p.values() if v.startswith("ERR:")), "?")).replace("ERR:", "")
        if status != expect:
            expect_bad.append((label, expect, status))
        if extra is not None:
            full.append((label, files, extra, c))
    check(f"{len(cases)} series: header bytes, every voxel hash and every refusal match the independent oracle", not bad, str(bad[:2]))
    check("every series ends the way its construction says it must", not expect_bad, str(expect_bad[:3]))
    reached = {v[4:] for c in c_res for v in c.values() if isinstance(v, str) and v.startswith("ERR:")}
    reached |= {c["RESULT"][4:] for c in c_res if c.get("RESULT", "").startswith("ERR:")}
    want = {"PHOTOMETRIC", "FORMAT", "RESCALE", "TOO_LARGE"}
    check("the series reach every NIfTI error class", reached >= want, "never reached: " + ", ".join(sorted(want - reached)))

    # ---------------------------------------------------------------- a second opinion: the repo's strict NIfTI reader
    ok_reader, detail = True, ""
    picks = [c for c in full if c[0] in ("16 bits, 12 stored, high bit 11, unsigned, axial", "16 bits, 12 stored, high bit 11, signed, oblique 2",
                                          "8 bits, 8 stored, high bit 7, signed, axial", "rescale slope 2.5 intercept -1024",
                                          "16 bits, 16 stored, high bit 15, signed, axial")]
    picks += [c for c in full if c[0] in ("real series UPENN-GBM-00002_T1c", "real series UPENN-GBM-00283")]
    for label, files, (res, (fmt, slope, inter)), c in picks:
        rows, cols, alloc, stored, high, signed = fmt
        n = rows * cols
        data = b"".join(convert(f[ds.parse(f, "f").pix_off:], alloc, stored, high, signed, n) for f in files)
        blob = bytes.fromhex(c["HEADER"]) + data
        try:
            h = ns.read_header(io.BytesIO(blob))
            vol = ns.read_channel(io.BytesIO(blob[348:]), h, 0)
            tname = {2: "uint8", 4: "int16", 256: "int8", 512: "uint16"}[datatype(alloc, signed)]
            px = struct.unpack("<8f", struct.pack("<8f", 1.0, *res["pixdim"], 0, 0, 0, 0))[1:4]
            good = (h["dims"][:3] == [cols, rows, len(files)] and h["dtype"] == tname and
                    tuple(h["pixdim"][:3]) == px and abs(h["slope"] - slope) <= 1e-6 * abs(slope) and h["inter"] == inter and
                    h["descrip"] == DESCRIP.decode() and bytes(vol) == data[:len(bytes(vol))] and len(vol) * h["nbytes"] == len(data))
            if not good:
                ok_reader, detail = False, f"{label}: {h['dims']} {h['dtype']} {h['pixdim']} {h['slope']} {h['inter']}"
        except ns.NiftiError as e:
            ok_reader, detail = False, f"{label}: {e}"
    check(f"the repo's strict NIfTI reader accepts {len(picks)} of the written files and reads back the right size, type, "
          f"voxel sizes, slope, intercept and voxel bytes", ok_reader and len(picks) >= (7 if real else 5), detail)

    # ---------------------------------------------------------------- the layout, against a file written by other software
    real_nii = os.path.join(ROOT, "data", "msd_task01_sample", "BRATS_001.nii.gz")
    if os.path.exists(real_nii):
        with gzip.open(real_nii, "rb") as f:
            hd = f.read(352)
        pix = struct.unpack_from("<8f", hd, 76)
        rows3 = [struct.unpack_from("<4f", hd, 280 + 16 * r) for r in range(3)]
        diag = [abs(rows3[r][r]) for r in range(3)]
        good = (struct.unpack_from("<i", hd, 0)[0] == 348 and hd[344:348] == b"n+1\0" and struct.unpack_from("<f", hd, 108)[0] == 352.0
                and struct.unpack_from("<2h", hd, 252)[1] in (1, 2, 3, 4)
                and all(abs(diag[r] - pix[1 + r]) < 1e-3 for r in range(3)))
        check("our header layout (offsets of sizeof_hdr, vox_offset, sform_code, srow_x/y/z, magic) matches a NIfTI written by other software",
              good, f"{struct.unpack_from('<2h', hd, 252)} {rows3} {pix}")
    else:
        print("SKIP  layout check against BRATS_001.nii.gz: not present")

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    sys.exit(1 if failures else 0)


main()
