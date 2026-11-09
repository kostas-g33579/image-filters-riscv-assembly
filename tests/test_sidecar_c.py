#!/usr/bin/env python3
"""Tests of the JSON sidecar generator (pico/src/sidecar.c).

The oracle is the CONSTRUCTION: each case plants known values in the DICOM files, and this file works out,
with exact Decimal arithmetic and from the rules written down in sidecar.h, what the JSON has to say. The
generator's output is parsed with a real JSON parser and compared strictly (types included: in Python 1 == True,
which would hide a 1 printed where true belongs).

 * every acquisition value, in BIDS units (seconds, degrees, tesla), exactly, and its range check at the boundaries;
 * the gauss -> tesla conversion of the field strength and its boundaries;
 * the text values: allowed characters, the 64-character limit, a backslash, rejection of anything else, and that a
   rejected value is never echoed (only "text_rejected" and the field name);
 * the measured block (size, voxel sizes, field of view, the matrix, type) against the verified Python Stack;
 * discrepancies, in order; the audit block (identifying field names and counts, never values); the labels;
 * that no identifying value from the files appears anywhere in the JSON, for the synthetic cases (planted
   sentinels) and for the 12 real series (values read independently).

Usage: python3 tests/test_sidecar_c.py      (or: make test-sidecar-c)
"""
import importlib.util, json, os, re, struct, subprocess, sys
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ds = load("dicom_slice", os.path.join(ROOT, "scripts", "dicom_slice.py"))
gen = load("dicomgen", os.path.join(ROOT, "tests", "dicomgen.py"))
BIN = os.environ.get("SIDECAR_C_BIN", os.path.join(ROOT, "build32", "sidecar_test"))
QEMU = os.environ.get("QEMU", "qemu-riscv32").split()
WS = " \t\n\x0b\x0c\r"
EXPL, IMPL = gen.EXPL, gen.IMPL
failures = 0


def check(name, ok, detail=""):
    global failures
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))
    failures += not ok


# ------------------------------------------------------------------ running the C generator
def run_c(series):
    """series: list of (labels tuple of 4 bytes, files). Returns a list of dicts."""
    blob = b""
    for labels, files in series:
        blob += b"".join(struct.pack("B", len(x)) + x for x in labels)
        blob += struct.pack("<I", len(files)) + b"".join(struct.pack("<I", len(f)) + f for f in files)
    r = subprocess.run(QEMU + [BIN], input=blob, capture_output=True, timeout=600)
    if r.returncode != 0 or r.stderr:
        raise RuntimeError("C harness failed: rc=%s %s" % (r.returncode, r.stderr[:200]))
    out, cur, json_lines = [], None, None
    for line in r.stdout.decode("latin-1").split("\n"):
        if json_lines is not None:                        # inside the JSON: collect until its end marker
            if line == "JSON_END":
                cur["JSON"] = "\n".join(json_lines) + "\n"
                json_lines = None
            else:
                json_lines.append(line)
        elif line == "SERIES":
            cur = {}
        elif line == "JSON_BEGIN":
            json_lines = []
        elif line == "END":
            out.append(cur)
        elif "=" in line and cur is not None:
            k, _, v = line.partition("=")
            cur[k] = v
    if len(out) != len(series):
        raise RuntimeError("C harness returned %d results for %d series" % (len(out), len(series)))
    return out


# ------------------------------------------------------------------ the oracle
def clean(raw):
    """A text value as the reader sees it: cut at a NUL, trailing spaces/NULs dropped, ASCII whitespace stripped."""
    s = raw.split(b"\0")[0].rstrip(b" \0").decode("latin-1")
    return s.strip(WS)


def first_dec(raw):
    s = raw.split(b"\0")[0].rstrip(b" \0").decode("latin-1")
    parts = [x.strip(WS) for x in s.split("\\") if x.strip(WS)]
    return Decimal(parts[0]) if parts else None


LO_OK = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 _.+-:*/()\\")
CS_OK = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _\\")


def text_ok(s, allowed):
    return 0 < len(s) <= 64 and all(c in allowed for c in s)


def r9(x):
    """A measured value as the generator rounds it: 9 decimals, integer arithmetic."""
    n = int(abs(x) * 1e9 + 0.5)
    if n == 0:
        return Decimal(0)
    v = Decimal(n) / Decimal(10 ** 9)
    return -v if x < 0 else v


def cnt(vals):
    d = {"YES": 0, "NO": 0, "other": 0, "absent": 0}
    for v in vals:
        d["absent" if not v else "YES" if v == b"YES" else "NO" if v == b"NO" else "other"] += 1      # the writer omits a falsy value
    return d


IDENT_WRITTEN = ["StudyDate", "InstitutionName", "StudyDescription", "SeriesDescription", "PatientName", "PatientID",
                 "PatientBirthDate", "PatientSex"]
C_TABLE_ORDER = ["StudyDate", "SeriesDate", "AcquisitionDate", "ContentDate", "AccessionNumber", "InstitutionName",
                 "InstitutionAddress", "ReferringPhysicianName", "StudyDescription", "PerformingPhysicianName",
                 "OperatorsName", "StationName", "DeviceSerialNumber", "SeriesDescription", "PatientName", "PatientID",
                 "PatientBirthDate", "PatientSex", "PatientAge", "PatientSize", "PatientWeight", "PatientAddress",
                 "PatientTelephoneNumbers", "PatientComments"]


def expected(P, files, labels, burned, removed, ident=True):
    st = ds.Stack()
    imgs = [ds.parse(f, "f") for f in files]
    for im in imgs:
        st.add(im)
    res = st.finish()
    t = imgs[0].tags
    alloc, signed = t["BitsAllocated"], bool(t["PixelRepresentation"])
    dtype = ("int8" if signed else "uint8") if alloc == 8 else ("int16" if signed else "uint16")
    cols, rows, n = res["cols"], res["rows"], res["count"]
    mm = res["pixdim"]
    names = ("subject", "sequence", "site", "scanner")
    e = {"tool": {"name": "dicom-sanitiser", "format_version": 1},
         "labels": {k: v.decode() for k, v in zip(names, labels) if v}}
    e["measured"] = {"size": [cols, rows, n], "voxel_size_mm": [r9(x) for x in mm],
                     "plane": ("sagittal", "coronal", "axial")[res["plane"]],
                     "field_of_view_mm": [r9(cols * mm[0]), r9(rows * mm[1]), r9(n * mm[2])],
                     "voxel_to_ras": [r9(x) for x in res["affine"]], "datatype": dtype,
                     "bits_stored": t.get("BitsStored", alloc)}
    ft, disc, text_rej, range_rej = {}, [], [], []
    for key, raw, lim in (("RepetitionTime", P.get("tr"), (0, 10 ** 6)), ("EchoTime", P.get("te"), (0, 10 ** 5)),
                          ("InversionTime", P.get("ti"), (0, 10 ** 5))):
        v = first_dec(raw) if raw is not None else None
        if v is not None:
            if lim[0] <= v <= lim[1]:
                ft[key] = v / 1000
            else:
                range_rej.append(key)
    v = first_dec(P["flip"]) if P.get("flip") is not None else None
    if v is not None:
        if 0 <= v <= 360:
            ft["FlipAngle"] = v
        else:
            range_rej.append("FlipAngle")
    fs = first_dec(P["field"]) if P.get("field") is not None else None
    fs_disc = None
    if fs is not None:
        if Decimal("0.1") <= fs <= 12:
            ft["MagneticFieldStrength"] = fs
        elif 1000 <= fs <= 120000:
            ft["MagneticFieldStrength"] = fs / 10000
            fs_disc = {"what": "field_strength_converted_from_gauss", "tag": fs, "used_tesla": fs / 10000}
        else:
            fs_disc = {"what": "field_strength_out_of_range", "tag": fs}
    for key, raw in (("ScanningSequence", P.get("scanseq")), ("SequenceVariant", P.get("seqvar")), ("MRAcquisitionType", P.get("acq"))):
        s = clean(raw) if raw is not None else ""
        if s:
            if text_ok(s, CS_OK):
                ft[key] = s
            else:
                text_rej.append(key)
    contrast = clean(P["contrast"]) if P.get("contrast") is not None else ""
    ft["ContrastAgentDeclared"] = True if contrast else None
    copied = []
    for key, raw in (("Manufacturer", P.get("manu")), ("ManufacturersModelName", P.get("model")),
                     ("SoftwareVersions", P.get("sw")), ("ProtocolName", P.get("proto"))):
        s = clean(raw) if raw is not None else ""
        if s:
            if text_ok(s, LO_OK):
                ft[key] = s
                copied.append(key)
            else:
                text_rej.append(key)
    thick = Decimal("%.10g" % P["thick"])
    if Decimal("0.000001") <= thick <= 1000:
        ft["SliceThickness"] = thick
    else:
        range_rej.append("SliceThickness")
    ft["RescaleSlope"] = Decimal("%.10g" % P["slope"]) if P.get("slope") is not None else Decimal(1)
    ft["RescaleIntercept"] = Decimal("%.10g" % P["inter"]) if P.get("inter") is not None else Decimal(0)
    e["from_tags"] = ft
    if fs_disc:
        disc.append(fs_disc)
    order_text = ["Manufacturer", "ManufacturersModelName", "SoftwareVersions", "ProtocolName", "ScanningSequence",
                  "SequenceVariant", "MRAcquisitionType"]
    order_range = ["RepetitionTime", "EchoTime", "InversionTime", "FlipAngle", "SliceThickness"]
    disc += [{"what": "text_rejected", "field": k} for k in order_text if k in text_rej]
    disc += [{"what": "value_out_of_range", "field": k} for k in order_range if k in range_rej]
    if res["thickness_mismatch"]:
        disc.append({"what": "slice_thickness_differs_from_spacing", "tag_mm": thick, "measured_mm": r9(mm[2])})
    if res["thickness_assumed"]:
        disc.append({"what": "slice_spacing_assumed_from_thickness", "used_mm": r9(mm[2])})
    e["discrepancies"] = disc
    ident_names = {x: n for x in IDENT_WRITTEN} if ident else {}
    e["audit"] = {"slices": n,
                  "identifying_fields_present_values_never_read": {x: n for x in C_TABLE_ORDER if x in ident_names},
                  "all_other_tags": "dropped",
                  "pixels": "the file's own integers, re-packed to the stated type; not filtered, not rescaled",
                  "burned_in_text": "not detected and not removed",
                  "faces": "not removed; a 3D head volume can allow a face to be reconstructed",
                  "burned_in_annotation_tag_by_slice": cnt(burned), "patient_identity_removed_tag_by_slice": cnt(removed),
                  "free_text_copied": copied}
    return e


def strict_equal(a, b, path=""):
    """Equality that tells bool, None and numbers apart. Returns None if equal, else a description."""
    if isinstance(a, bool) or isinstance(b, bool) or a is None or b is None:
        return None if (a is b) else f"{path}: {a!r} vs {b!r}"
    if isinstance(a, dict) and isinstance(b, dict):
        if list(a) != list(b) and set(a) != set(b):
            return f"{path}: keys {sorted(set(a) ^ set(b))}"
        for k in a:
            r = strict_equal(a[k], b[k], f"{path}/{k}")
            if r:
                return r
        return None
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return f"{path}: length {len(a)} vs {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            r = strict_equal(x, y, f"{path}[{i}]")
            if r:
                return r
        return None
    if isinstance(a, (int, Decimal)) and isinstance(b, (int, Decimal)):
        return None if Decimal(a) == Decimal(b) else f"{path}: {a} vs {b}"
    return None if a == b else f"{path}: {a!r} vs {b!r}"


# ------------------------------------------------------------------ building cases
def lint(text):
    """Text-level rules for every number (outside string literals): no trailing zero after a decimal point, no -0,
    no leading '+', no leading '.', no leading zeros. The values are compared numerically elsewhere, which would
    accept 0.9765625000, so this looks at the text people read."""
    t = re.sub(r'"(?:[^"\\]|\\.)*"', '""', text)
    bad = []
    if re.search(r"\d\.\d*0(?=[^\d])", t):
        bad.append("trailing zero")
    if re.search(r"(?<![\d.])-0(?![\d.])", t):
        bad.append("-0")
    if re.search(r"\+\d", t):
        bad.append("plus sign")
    if re.search(r"(?<![\d])\.\d", t):
        bad.append("leading point")
    if re.search(r"(?<![\d.])0\d", t):
        bad.append("leading zero")
    if re.search(r"0{21,}", t):                       # plain decimals are used up to 10^20; beyond that the exponent form
        bad.append("a plain decimal longer than 20 places")
    if re.search(r"\.0{20,}", t):                    # ... and down to 10^-20
        bad.append("a plain decimal smaller than 1e-20")
    return bad


def raw_el(g, e, vr, val, explicit):
    return gen.enc(g, e, vr, val, explicit)


def extras(P, explicit):
    out = b""
    for key, (g, e, vr) in (("tr", (0x18, 0x80, b"DS")), ("te", (0x18, 0x81, b"DS")), ("ti", (0x18, 0x82, b"DS")),
                            ("flip", (0x18, 0x1314, b"DS")), ("field", (0x18, 0x87, b"DS")),
                            ("scanseq", (0x18, 0x20, b"CS")), ("seqvar", (0x18, 0x21, b"CS")), ("acq", (0x18, 0x23, b"CS")),
                            ("contrast", (0x18, 0x10, b"LO")), ("manu", (0x8, 0x70, b"LO")), ("model", (0x8, 0x1090, b"LO")),
                            ("sw", (0x18, 0x1020, b"LO")), ("proto", (0x18, 0x1030, b"LO"))):
        if P.get(key) is not None:
            out += raw_el(g, e, vr, P[key], explicit)
    return out


BASE = dict(tr=b"1760", te=b"3.87", ti=b"950", flip=b"15", field=b"3", scanseq=b"IR\\GR", seqvar=b"SP\\MP", acq=b"3D",
            contrast=b"OMNISCAN", manu=b"SIEMENS", model=b"TrioTim", sw=b"syngo MR 2006T 4VB12T",
            proto=b"t1 axial stealth-post", thick=1.0)


def build(P, n=4, ts=EXPL, burned=None, removed=None, ident=True, orient="axial", **kw):
    ex = extras(P, ts == EXPL)
    files = gen.stack(n, orient, ts=ts, extra_before=ex, thick=P["thick"], ident=ident, burned=burned, removed=removed,
                      slope=P.get("slope"), inter=P.get("inter"), **kw)
    return files


def main():
    if not os.path.exists(BIN):
        sys.exit("build the C sidecar harness first: make build32/sidecar_test (or make test-sidecar-c)")
    cases = []                          # (label, labels, files, expected dict or None, expected harness status)
    LAB = (b"P017", b"T1c", b"S02", b"C01")

    def add(label, P, n=4, labels=LAB, burned=None, removed=None, ident=True, ts=EXPL, orient="axial", **kw):
        files = build(P, n, ts, burned, removed, ident, orient, **kw)
        bl = burned if burned is not None else None
        b = [bl] * n if not callable(bl) else [bl(k) for k in range(n)]
        r = [removed] * n if not callable(removed) else [removed(k) for k in range(n)]
        cases.append((label, labels, files, expected(P, files, labels, b, r, ident), None))

    # ---- typical values, both encodings, several orientations
    for ts in (EXPL, IMPL):
        for orient in ("axial", "sagittal", "oblique 1"):
            add(f"Siemens-style MPRAGE, {'explicit' if ts == EXPL else 'implicit'}, {orient}", BASE, ts=ts, orient=orient)
    ge = dict(BASE, ti=None, field=b"15000", scanseq=b"GR", seqvar=b"SS\\SP\\SK", contrast=None, manu=b"GE MEDICAL SYSTEMS",
              model=b"GENESIS_SIGNA", sw=b"09", proto=b"Stealth Perfusion/", tr=b"9.1", te=b"1.988000035")
    add("GE-style: field strength in gauss, no inversion time, no contrast tag", ge)
    add("nothing but the writer's defaults (no acquisition tags; the writer always writes Manufacturer ACME)", dict(thick=2.0, manu=b"ACME"))
    # ---- the field strength: tesla window, gauss window, boundaries
    for fs, tag in [(b"0.1", "tesla"), (b"0.55", "tesla"), (b"1.5", "tesla"), (b"3", "tesla"), (b"7", "tesla"), (b"12", "tesla"),
                    (b"0.09", "bad"), (b"12.5", "bad"), (b"13", "bad"), (b"999", "bad"), (b"1000", "gauss"), (b"5500", "gauss"),
                    (b"15000", "gauss"), (b"30000", "gauss"), (b"120000", "gauss"), (b"120001", "bad"), (b"-3", "bad"), (b"0", "bad"),
                    (b"1.5\\3", "tesla"), (b"15000\\1", "gauss")]:
        add(f"field strength {fs.decode()} ({tag})", dict(BASE, field=fs))
    # ---- the numeric range checks, at and beyond each boundary
    for key, vals in (("tr", [b"0", b"1", b"1000000", b"1000001", b"-1", b"1760\\2000", b"1e3"]),
                      ("te", [b"0", b"100000", b"100001", b"-0.5", b"0.0001"]),
                      ("ti", [b"0", b"100000", b"100001", b"-1"]),
                      ("flip", [b"0", b"360", b"361", b"-1", b"90", b"12.5"])):
        for raw in vals:
            add(f"{key} = {raw.decode()}", dict(BASE, **{key: raw}))
    for th in (0.0, 1e-6, 1e-7, 0.5, 1000.0, 1000.5, 5.0):
        add(f"slice thickness {th}", dict(BASE, thick=th))
    # ---- text values
    for raw, what in [(b"SIEMENS", "plain"), (b"A" * 64, "64 characters"), (b"A" * 65, "65 characters"), (b'say "hi"', "a quote"),
                      (b"back\\slash", "a backslash"), (b"\xe9\xff", "non-ASCII"), (b"tab\there", "a tab"), (b"x\x01y", "a control character"),
                      (b"a=b", "an equals sign"), (b"a,b", "a comma"), (b"{\"x\":1}", "braces and quotes"), (b"</script>", "angle brackets"),
                      (b"  padded  ", "padding"), (b"", "empty"), (b"   ", "blanks"), (b"x\x00junk", "after a NUL"), (b"a;b", "a semicolon"),
                      (b"Siemens Healthineers (1.5T)/Sola", "parentheses and a slash"), (b"a\\\\b", "two backslashes")]:
        add(f"manufacturer: {what}", dict(BASE, manu=raw, model=raw, sw=raw, proto=raw))
    for raw, what in [(b"IR\\GR", "plain"), (b"ir", "lowercase"), (b"IR GR", "a space"), (b"GR_X", "an underscore"), (b"IR-GR", "a hyphen"),
                      (b"A" * 64, "64 letters"), (b"A" * 65, "65 letters"), (b"", "empty"), (b"IR\\", "trailing backslash")]:
        add(f"sequence codes: {what}", dict(BASE, scanseq=raw, seqvar=raw, acq=raw))
    # ---- contrast: declared, empty, absent
    for raw in (b"OMNISCAN", b"x", b" ", b"", None):
        add(f"contrast agent {raw!r}", dict(BASE, contrast=raw))
    # ---- rescale and thickness discrepancies
    for sl, it in [(None, None), (1, 0), (2.5, -1024), (0.015625, 3), (65535, 0), (1e-4, 0)]:
        add(f"rescale slope {sl} intercept {it}", dict(BASE, slope=sl, inter=it))
    add("thickness 3 mm, spacing 1 mm: a discrepancy", dict(BASE, thick=3.0))
    add("single slice: spacing assumed from the thickness", dict(BASE, thick=2.5), n=1)
    add("single slice, thickness 0: assumed 1 mm", dict(BASE, thick=0.0), n=1)
    add("several things wrong at once (order of the discrepancies)",
        dict(BASE, thick=3.0, field=b"15000", manu=b"bad;one", proto=b"x\x01", scanseq=b"lower", tr=b"-5", flip=b"400"))
    # ---- the audit block
    add("no identifying fields at all", BASE, ident=False)
    add("burned-in annotation YES on every slice", BASE, burned=b"YES")
    add("burned-in annotation NO, identity removed YES", BASE, burned=b"NO", removed=b"YES")
    add("burned-in annotation mixed, identity-removed odd", BASE, n=6, burned=lambda k: [b"YES", b"NO", b"MAYBE", None, b"YES", b"NO"][k],
        removed=lambda k: [b"YES", b"YES", b"no", None, b"NO", b"YES"][k])
    # ---- the acquisition values come from the FIRST slice: here every later slice says something else
    def varying(k, ex=True):
        P = dict(BASE) if k == 0 else dict(BASE, tr=b"999", te=b"9", ti=b"9", flip=b"9", field=b"7", manu=b"OTHER", model=b"OTHER",
                                           sw=b"OTHER", proto=b"OTHER", scanseq=b"SE", seqvar=b"NONE", acq=b"2D", contrast=None)
        return extras(P, ex)
    vfiles = gen.stack(4, "axial", extra_before=lambda k: varying(k), thick=1.0)
    cases.append(("tags that change after the first slice (the first one counts)", LAB, vfiles,
                  expected(BASE, vfiles, LAB, [None] * 4, [None] * 4), None))
    # ---- numbers with large exponents (printed in exponent form beyond 20) and at the switch
    for sl in (1e20, 1e21, 1e25, 1.5e21, 1e-20, 1e-21, 1e-25, 2.5e-22):
        add(f"rescale slope {sl:g}", dict(BASE, slope=sl))
    add("rescale intercept 1e22", dict(BASE, inter=1e22))
    add("rescale intercept -3e-23", dict(BASE, inter=-3e-23))
    # ---- text that must not break the JSON; every allowed punctuation character in one string
    allp = b"a_b.c+d-e:f*g/h(i)j\\k"
    add("every allowed punctuation character", dict(BASE, manu=allp, model=allp, sw=allp, proto=allp))
    # ---- odd values of the two annotation tags (only YES and NO are recognised, exactly)
    odd = [b"Y", b"YE", b"YESX", b"yes", b"NOO", b"N", b"NA", b"NX", b"YES", b"NO", None, b"", b"XYZ"]
    add("annotation tags with look-alike values", BASE, n=len(odd), burned=lambda k: odd[k], removed=lambda k: odd[::-1][k])
    # ---- the lessons of the NIfTI tests, again: anisotropic pixels, 8-bit data, bits stored below bits allocated
    for orient in ("axial", "sagittal", "coronal", "oblique 1"):
        add(f"anisotropic pixels 0.5 x 0.8 mm, 2.5 mm slices, {orient}", BASE, orient=orient, ps=(0.5, 0.8), dz=2.5)
        add(f"anisotropic pixels 1.25 x 0.9375 mm, 0.7 mm slices, {orient}", BASE, orient=orient, ps=(1.25, 0.9375), dz=0.7)
    for signed in (False, True):
        add(f"8-bit data, {'signed' if signed else 'unsigned'}", BASE, alloc=8, stored=8, high=7, signed=signed)
        add(f"16-bit data, {'signed' if signed else 'unsigned'}", BASE, signed=signed)
        add(f"12 bits stored in 16, {'signed' if signed else 'unsigned'}", BASE, alloc=16, stored=12, high=11, signed=signed)
        add(f"9 bits stored in 16, {'signed' if signed else 'unsigned'}", BASE, alloc=16, stored=9, high=8, signed=signed)
    # ---- a tiny NEGATIVE matrix entry must print as 0, never as -0
    add("a matrix entry of -1e-12", BASE, iop=(1, 0, 0, 1e-12, 1, 0))
    add("matrix entries of both signs near zero", BASE, iop=(1, -1e-13, 0, 1e-12, 1, -2e-12))
    # ---- labels
    for labels, what in [((b"P017", b"T1c", b"", b""), "subject and sequence only"), ((b"", b"", b"", b""), "none"),
                         ((b"a" * 32, b"B-9_z", b"S", b"C"), "32 characters"), ((b"A_b-C9", b"", b"", b""), "all allowed characters")]:
        add(f"labels: {what}", BASE, labels=labels)

    # ---- the 12 real series
    real = []
    root = os.path.join(ROOT, "data", "dicom")
    if os.path.isdir(root):
        for d in sorted(os.listdir(root)):
            p = os.path.join(root, d)
            if os.path.isdir(p):
                imgs, _ = ds.load_series(p)
                ordered, _, _, _ = ds.order_slices(imgs)
                real.append((d, [open(i.path, "rb").read() for i in ordered]))
    # ---- invalid labels (the generator is never reached) go through the same harness
    bad_labels = [(b"bad label", "a space"), (b"semi;colon", "a semicolon"), (b'q"uote', "a quote"), (b"a" * 33, "33 characters"),
                  (b"caf\xc3\xa9", "non-ASCII"), (b"new\nline", "a newline"), (b"back\\slash", "a backslash"), (b"a.b", "a dot")]
    label_cases = [(what, (raw, b"", b"", b""), build(BASE, 2)) for raw, what in bad_labels]
    label_cases += [("invalid sequence label", (b"ok", b"no good", b"", b""), build(BASE, 2)),
                    ("invalid site label", (b"ok", b"", b"s/1", b""), build(BASE, 2)),
                    ("invalid scanner label", (b"ok", b"", b"", b"c 1"), build(BASE, 2))]

    # ---------------------------------------------------------------- run and compare
    results = run_c([(l, f) for _, l, f, _, _ in cases] + [(lab, f) for _, lab, f in label_cases] + [(LAB, f) for _, f in real])
    n1, n2 = len(cases), len(label_cases)
    bad_parse, bad_val, leaks, lints = [], [], [], []
    sentinels = [v.decode() for v in gen.SENT.values()]
    for (label, labels, files, exp, _), r in zip(cases, results[:n1]):
        text = r.get("JSON")
        if text is None:
            bad_val.append((label, "no JSON: " + str(r)[:80]))
            continue
        try:
            got = json.loads(text, parse_float=Decimal)
        except ValueError as ex:
            bad_parse.append((label, str(ex)[:60]))
            continue
        diff = strict_equal(exp, got)
        lint_bad = lint(text)
        if lint_bad:
            lints.append((label, lint_bad))
        if diff:
            bad_val.append((label, diff))
        if any(s in text for s in sentinels):
            leaks.append(label)
    check(f"{n1} cases: the JSON of every one parses as JSON", not bad_parse, str(bad_parse[:2]))
    check(f"{n1} cases: every value, every range check, every discrepancy and the audit block match the oracle", not bad_val, str(bad_val[:3]))
    check("no number in any JSON has a trailing zero, a -0, a plus sign, a leading point or a leading zero", not lints, str(lints[:3]))
    check("sc_valid_label(\"\") is false: an empty label is never valid (the caller decides whether one is needed)",
          all(r.get("EMPTY_LABEL_VALID") == "0" for r in results), "")
    check("no planted identifying value (name, ID, birth date, institution, descriptions) appears in any JSON", not leaks, str(leaks[:3]))
    lab_bad = [(w, r) for (w, _, _), r in zip(label_cases, results[n1:n1 + n2]) if not r.get("LABELS", "").startswith("ERR") or "JSON" in r]
    check(f"{n2} invalid labels are all refused before anything is generated", not lab_bad, str(lab_bad[:2]))

    # ---- real series
    ok_real, why = True, ""
    for (d, files), r in zip(real, results[n1 + n2:]):
        try:
            j = json.loads(r["JSON"], parse_float=Decimal)
            imgs = [ds.parse(f, "f") for f in files]
            t0 = imgs[0].tags
            good = (j["measured"]["size"] == [t0["Columns"], t0["Rows"], len(files)] and j["audit"]["slices"] == len(files)
                    and j["from_tags"]["MagneticFieldStrength"] in (Decimal("1.5"), Decimal(3))
                    and "PatientName" in j["audit"]["identifying_fields_present_values_never_read"])
            ge_case = d.startswith("UPENN-GBM-00283")
            has_gauss = any(x["what"] == "field_strength_converted_from_gauss" for x in j["discrepancies"])
            good = good and (has_gauss == ge_case)
            # the real identifying values, read independently, must not be in the JSON
            secrets = set()
            for f in files[:3]:
                for (g, e) in ((0x10, 0x10), (0x10, 0x20), (0x08, 0x20), (0x10, 0x1010)):
                    p = f.find(struct.pack("<HH", g, e), 132)
                    if p > 0:
                        v = f[p + 8:p + 8 + struct.unpack_from("<I", f, p + 4)[0]].strip(b"\0 ").decode("latin-1")
                        if len(v) >= 3:
                            secrets.add(v)
            leaked = [s for s in secrets if s in r["JSON"]]
            if not good or leaked or len(secrets) < 3:
                ok_real, why = False, f"{d}: good={good} leaked={leaked} secrets={len(secrets)}"
        except Exception as ex:                                      # noqa: BLE001 - report which series failed
            ok_real, why = False, f"{d}: {ex!r}"
    if real:
        check(f"the {len(real)} real series: valid JSON, right size, a field strength in tesla (the GE ones converted from gauss), "
              f"and none of their real identifying values anywhere in it", ok_real, why)
    else:
        print("SKIP  real series: data/dicom/ not present")

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    sys.exit(1 if failures else 0)


main()
