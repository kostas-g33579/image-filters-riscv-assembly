#!/usr/bin/env python3
"""Differential test of the C stack geometry (pico/src/geom.c) against the Python one (Stack in
scripts/dicom_slice.py), plus a check of the result against the DEFINITION of DICOM geometry.

Two kinds of check, because they catch different mistakes:

 * C against Python: the same slices go to both; the status of every slice, the final result and
   every double (printed as its 64-bit pattern) must agree to the last bit. This catches a mistake
   in the C port.
 * Against the definition: for a series that is accepted, the matrix must map voxel (i, j, k) to
   where DICOM says that pixel is, IPP_k + i * ps_col * r + j * ps_row * c, converted from DICOM's
   left-posterior-superior frame to NIfTI's right-anterior-superior (x and y negated). This uses each
   slice's OWN position, not the step vector the implementations use, and shares no code with either.
   It catches a mistake both implementations would share (a wrong row/column pairing, a missing
   sign, a transposed spacing).

The series: synthetic stacks from the independent writer (axial, flipped, sagittal, coronal, oblique;
gantry tilt; a descending stack; 1 to 40 slices; anisotropic pixels), every error class, random stacks
with noise on the positions, and the 12 real series (in order, reversed and shuffled).

Usage: python3 tests/test_geom_c.py      (or: make test-geom-c)
"""
import importlib.util, math, os, random, struct, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ds = load("dicom_slice", os.path.join(ROOT, "scripts", "dicom_slice.py"))
gen = load("dicomgen", os.path.join(ROOT, "tests", "dicomgen.py"))
BIN = os.environ.get("GEOM_C_BIN", os.path.join(ROOT, "build32", "geom_test"))
QEMU = os.environ.get("QEMU", "qemu-riscv32").split()
failures = 0


def check(name, ok, detail=""):
    global failures
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))
    failures += not ok


# ------------------------------------------------------------------ the two implementations
def hexd(x):
    return struct.pack(">d", x).hex()


def py_run(files):
    d = {}
    st = ds.Stack()
    for i, f in enumerate(files):
        try:
            img = ds.parse(f, "f")
        except ds.DicomError:
            d[f"F{i}"] = "ERR:PARSE"
            return d
        try:
            ds.check_image(img, "f")
        except ds.DicomError:
            d[f"F{i}"] = "ERR:CHECK"
            return d
        try:
            st.add(img)
        except ds.GeomError as e:
            d[f"F{i}"] = "ERR:" + e.cls
            return d
        d[f"F{i}"] = "OK"
    try:
        r = st.finish()
    except ds.GeomError as e:
        d["RESULT"] = "ERR:" + e.cls
        return d
    d["RESULT"] = "OK"
    for k in ("cols", "rows", "count", "plane", "thickness_assumed", "thickness_mismatch"):
        d[k] = str(r[k])
    d["pixdim"] = ",".join(hexd(x) for x in r["pixdim"])
    d["affine"] = ",".join(hexd(x) for x in r["affine"])
    return d


def run_c(series):
    blob = b""
    for files in series:
        blob += struct.pack("<I", len(files)) + b"".join(struct.pack("<I", len(f)) + f for f in files)
    r = subprocess.run(QEMU + [BIN], input=blob, capture_output=True, timeout=600)
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


def unhex(h):
    return struct.unpack(">d", bytes.fromhex(h))[0]


def truth_check(files, res, tol=1e-5):
    """The matrix must map every voxel to where DICOM puts that pixel (computed from each slice's own tags)."""
    A = [unhex(x) for x in res["affine"].split(",")]
    worst = 0.0
    for k, f in enumerate(files):
        t = ds.parse(f, "f").tags
        ipp, (r, c) = t["ImagePositionPatient"], (t["ImageOrientationPatient"][:3], t["ImageOrientationPatient"][3:])
        psr, psc = t["PixelSpacing"]
        rows, cols = t["Rows"], t["Columns"]
        for (i, j) in {(0, 0), (cols - 1, 0), (0, rows - 1), (cols - 1, rows - 1), (cols // 2, rows // 2)}:
            lps = [ipp[a] + i * psc * r[a] + j * psr * c[a] for a in range(3)]
            want = (-lps[0], -lps[1], lps[2])                       # LPS -> RAS
            got = [A[4 * a] * i + A[4 * a + 1] * j + A[4 * a + 2] * k + A[4 * a + 3] for a in range(3)]
            worst = max(worst, max(abs(got[a] - want[a]) for a in range(3)))
    return worst <= tol, worst


# ------------------------------------------------------------------ building synthetic stacks (tests/dicomgen.py)
rotation, ORIENT, cross, stack = gen.rotation, gen.ORIENT, gen.cross, gen.stack


def main():
    if not os.path.exists(BIN):
        sys.exit("build the C geometry first: make build32/geom_test (or make test-geom-c)")
    cases = []                                  # (label, files, expected RESULT/F status or None)

    # ---- valid stacks
    for orient in ORIENT:
        for n in (1, 2, 3, 7, 25):
            for ts in (gen.EXPL, gen.IMPL):
                cases.append((f"{orient}, {n} slices", stack(n, orient, ts=ts), "OK"))
        cases.append((f"{orient}, tilted stack", stack(9, orient, tilt=0.35), "OK"))
        cases.append((f"{orient}, anisotropic pixels, 2.5 mm slices", stack(6, orient, dz=2.5, ps=(0.5, 0.75)), "OK"))
        cases.append((f"{orient}, stack running against the normal, sent ascending", stack(8, orient, descending=True), "OK"))
        cases.append((f"{orient}, 40 slices", stack(40, orient, dz=0.9), "OK"))
    cases.append(("single slice, thickness 2.5", stack(1, thick=2.5), "OK"))
    cases.append(("single slice, thickness 0 (assumed 1 mm)", stack(1, thick=0), "OK"))
    cases.append(("thickness tag disagrees with the spacing (mismatch flag)", stack(5, dz=1.0, thick=3.0), "OK"))
    cases.append(("thickness tag within 1% of the spacing", stack(5, dz=1.0, thick=1.005), "OK"))
    # the step is taken from the first two slices, so an error in their positions is multiplied by the slice number:
    # a stack of 12 tolerates ~1e-4 mm of noise, one of 200 about 5e-6 mm; real scanner positions are far better
    cases.append(("positions precise to 1e-7 mm", stack(40, dz=1.0, noise=1e-7, rng=random.Random(1)), "OK"))
    cases.append(("positions with 1e-5 mm noise, 12 slices", stack(12, dz=1.0, noise=1e-5, rng=random.Random(1)), "OK"))
    cases.append(("positions with 2 micrometre noise, 12 slices (refused)", stack(12, dz=1.0, noise=0.002, rng=random.Random(1)), "UNEVEN"))
    cases.append(("positions rounded to 3 decimals, 200 slices (refused: the safe direction)",
                  [gen.make(2, 3, pos=(round(-91.5 + 0.99999 * k * 0.0 , 3), round(-149.25 + 0.0, 3), round(60.5 + 1.00037 * k, 3)),
                            iop=(1, 0, 0, 0, 1, 0), spacing=(1, 1), uid=b"1.2", inst=k) for k in range(200)], "UNEVEN"))

    # ---- every error class
    desc = stack(6, "axial", descending=True)[::-1]
    cases.append(("sent in descending order", desc, "NOT_SORTED"))
    good = stack(8, "oblique 1")
    cases.append(("two slices swapped (the regular lattice is broken first)", good[:3] + [good[4], good[3]] + good[5:], "UNEVEN"))
    cases.append(("a duplicated slice", good[:4] + [good[3]] + good[4:], "NOT_SORTED"))
    cases.append(("a missing slice (gap)", good[:4] + good[5:], "UNEVEN"))
    cases.append(("positions with noise above the tolerance", stack(12, noise=0.4, rng=random.Random(2)), "UNEVEN"))
    drift = stack(6, "axial")
    d_sl = stack(6, "axial", origin=(-91.5 + 5.0, -149.25, 60.5))
    cases.append(("one slice shifted 5 mm sideways", drift[:3] + [d_sl[3]] + drift[4:], "UNEVEN"))
    cases.append(("series UID changes", stack(3, uid=b"1.2.3") [:2] + stack(3, uid=b"1.2.9")[2:], "SERIES"))
    cases.append(("series UID over 64 characters", stack(3, uid=b"1." * 40), "SERIES"))
    cases.append(("columns differ", stack(3, cols=3)[:2] + stack(3, cols=4)[2:], "MISMATCH"))
    cases.append(("rows differ", stack(3, rows=2)[:2] + stack(3, rows=5)[2:], "MISMATCH"))
    cases.append(("pixel spacing differs", stack(3)[:2] + stack(3, ps=(1.0, 1.0))[2:], "MISMATCH"))
    iop = ORIENT["oblique 1"]
    near = tuple(iop[0]) + tuple(iop[1])
    bump = lambda v: (near[0] + v,) + near[1:]
    cases.append(("orientation differs by 1e-5 (accepted)", stack(3, iop=near)[:2] + stack(3, iop=bump(1e-5), origin=(-91.5, -149.25, 60.5))[2:], None))
    cases.append(("orientation differs by 1e-3 (refused)", stack(3, iop=near)[:2] + stack(3, iop=bump(1e-3), origin=(-91.5, -149.25, 60.5))[2:], "MISMATCH"))
    cases.append(("direction cosines not unit length", stack(3, iop=(1.02, 0, 0, 0, 1, 0)), "ORIENTATION"))
    cases.append(("row and column not perpendicular", stack(3, iop=(1, 0, 0, 0.1, 0.995, 0)), "ORIENTATION"))
    cases.append(("column direction not unit length", stack(3, iop=(1, 0, 0, 0, 1.02, 0)), "ORIENTATION"))
    cases.append(("column direction slightly off unit length (accepted)", stack(3, iop=(1, 0, 0, 0, 1.0009, 0)), "OK"))
    cases.append(("row direction slightly off unit length (accepted)", stack(3, iop=(1.0009, 0, 0, 0, 1, 0)), "OK"))
    cases.append(("seven orientation values", stack(3, iop=(1, 0, 0, 0, 1, 0, 0)), "NO_GEOMETRY"))
    cases.append(("four position values", [gen.make(2, 3, pos=(0, 0, k, 5), iop=(1, 0, 0, 0, 1, 0), uid=b"1") for k in range(3)], "NO_GEOMETRY"))
    cases.append(("one position value", [gen.make(2, 3, pos=(k,), iop=(1, 0, 0, 0, 1, 0), uid=b"1") for k in range(3)], "NO_GEOMETRY"))
    cases.append(("no series UID on any slice", [gen.make(2, 3, pos=(0, 0, k), iop=(1, 0, 0, 0, 1, 0)).replace(b"1.2.3.4", b"") for k in range(3)], None))
    # gaps found by mutation testing: each of these separates a correct implementation from a plausible bug
    cases.append(("round numbers (decimals with positive exponents: 100, 30, 20)", stack(4, origin=(100, -200, 30), dz=10, ps=(20, 5)), "OK"))
    cases.append(("column direction 0.5% too long (refused)", stack(3, iop=(1, 0, 0, 0, 1.005, 0)), "ORIENTATION"))
    cases.append(("row direction 0.5% too long (refused)", stack(3, iop=(1.005, 0, 0, 0, 1, 0)), "ORIENTATION"))
    cases.append(("row and column 0.005 away from perpendicular (refused)", stack(3, iop=(1, 0, 0, 0.005, 1, 0)), "ORIENTATION"))
    cases.append(("series UID: a later slice has one more character", stack(3, uid=b"1.2.3")[:2] + stack(3, uid=b"1.2.34")[2:], "SERIES"))
    cases.append(("series UID: a later slice has one character less", stack(3, uid=b"1.2.34")[:2] + stack(3, uid=b"1.2.3")[2:], "SERIES"))
    cases.append(("no slice thickness tag, 6 slices (no mismatch flag)", stack(6, drop=("SliceThickness",)), "OK"))
    cases.append(("no slice thickness tag, single slice (assumed 1 mm)", stack(1, drop=("SliceThickness",)), "OK"))
    cases.append(("only the row spacing differs", stack(3, ps=(0.9765625, 1.0))[:2] + stack(3, ps=(1.0, 1.0))[2:], "MISMATCH"))
    cases.append(("only the column spacing differs", stack(3, ps=(1.0, 0.9765625))[:2] + stack(3, ps=(1.0, 1.0))[2:], "MISMATCH"))
    cases.append(("orientation differs by 5e-4 (refused at 1e-4)", stack(3, iop=near)[:2] + stack(3, iop=bump(5e-4), origin=(-91.5, -149.25, 60.5))[2:], "MISMATCH"))
    for comp in range(6):
        bumped = tuple(near[q] + (1e-3 if q == comp else 0.0) for q in range(6))
        cases.append((f"orientation component {comp} differs by 1e-3", stack(3, iop=near)[:2] + stack(3, iop=bumped, origin=(-91.5, -149.25, 60.5))[2:], "MISMATCH"))
    cases.append(("zero pixel spacing", stack(3, ps=(0.0, 1.0)), "NO_GEOMETRY"))
    cases.append(("negative pixel spacing", stack(3, ps=(1.0, -1.0)), "NO_GEOMETRY"))
    cases.append(("three pixel spacing values", stack(3, ps=(1.0, 1.0, 1.0)), "NO_GEOMETRY"))
    cases.append(("five orientation values", [gen.make(2, 3, pos=(0, 0, k), iop=(1, 0, 0, 0, 1), uid=b"1") for k in range(3)], "NO_GEOMETRY"))
    cases.append(("no position tag", [gen.make(2, 3, pos=None, iop=(1, 0, 0, 0, 1, 0), uid=b"1") for _ in range(3)], "NO_GEOMETRY"))
    cases.append(("no orientation tag", [gen.make(2, 3, pos=(0, 0, k), iop=None, uid=b"1") for k in range(3)], "NO_GEOMETRY"))
    cases.append(("no slices at all", [], "NO_GEOMETRY"))
    cases.append(("an unreadable file in the stack", good[:3] + [b"not dicom"] + good[3:], "PARSE"))
    cases.append(("a file that fails the pixel checks", good[:3] + [gen.make(2, 3, spp=3, pos=(0, 0, 4), iop=(1, 0, 0, 0, 1, 0))] + good[3:], "CHECK"))

    # ---- random stacks: random orientation, spacing, tilt, noise, size
    rng = random.Random(2024)
    for i in range(300):
        a, b, c = rng.uniform(-3, 3), rng.uniform(-3, 3), rng.uniform(-3, 3)
        r, cc = rotation(a, b, c)
        n = rng.choice([1, 2, 3, 4, 5, 8, 13, 30])
        cases.append((f"random stack {i}", stack(n, iop=tuple(r) + tuple(cc), dz=rng.uniform(0.3, 6), tilt=rng.uniform(-0.5, 0.5),
                                                  origin=(rng.uniform(-150, 150), rng.uniform(-150, 150), rng.uniform(-150, 150)),
                                                  ps=(rng.choice([0.5, 0.75, 0.9375, 1.0, 1.25]), rng.choice([0.5, 0.75, 0.9375, 1.0])),
                                                  noise=rng.choice([0, 0, 0.0005, 0.02, 0.3]), rng=rng, descending=rng.random() < 0.3,
                                                  ts=rng.choice([gen.EXPL, gen.IMPL])), None))

    # ---- the real series, in order, reversed and shuffled
    root = os.path.join(ROOT, "data", "dicom")
    if os.path.isdir(root):
        for d in sorted(os.listdir(root)):
            p = os.path.join(root, d)
            if not os.path.isdir(p):
                continue
            imgs, _ = ds.load_series(p)
            ordered, _, _, _ = ds.order_slices(imgs)
            files = [open(i.path, "rb").read() for i in ordered]
            cases.append((f"real series {d} in order", files, "OK"))
            cases.append((f"real series {d} reversed", files[::-1], "NOT_SORTED"))
            sh = files[:]
            random.Random(5).shuffle(sh)
            cases.append((f"real series {d} shuffled", sh, {"NOT_SORTED", "UNEVEN"}))
    else:
        print("SKIP  real series: data/dicom/ not present")

    # ---------------------------------------------------------------- run both and compare
    c_res = run_c([f for _, f, _ in cases])
    bad, reached, ok_checked, worst_truth, worst_strict, worst_real = [], set(), 0, 0.0, 0.0, 0.0
    expect_bad, truth_bad = [], []
    for (label, files, expect), c in zip(cases, c_res):
        p = py_run(files)
        if p != c:
            diff = sorted(k for k in set(p) | set(c) if p.get(k) != c.get(k))
            bad.append((label, [(k, p.get(k), c.get(k)) for k in diff[:2]]))
        status = p.get("RESULT") if p.get("RESULT") else next((v for k, v in p.items() if v.startswith("ERR:")), "?")
        status = "OK" if status == "OK" else status.replace("ERR:", "")
        if status != "OK":
            reached.add(status)
        if expect is not None and status not in (expect if isinstance(expect, set) else {expect}):
            expect_bad.append((label, expect, status))
        if status == "OK":
            # An accepted stack may differ from a perfect lattice by the tolerance it was accepted under (1% of the
            # step plus 0.1 micrometre), and orientation noise below 1e-4 moves far pixels a little: exact stacks
            # must match to 1e-5 mm, perturbed ones to those bounds.
            exact = not any(w in label for w in ("noise", "random", "differs", "precise", "oblique 1 ", "real series")) or "precise to 1e-7" in label
            if "real series" in label:
                worst_real = max(worst_real, truth_check(files, p, tol=1.0)[1])
            step = unhex(p["pixdim"].split(",")[2])
            ok, w = truth_check(files, p, tol=1e-5 if exact else 0.01 * step + 1e-4 + 1e-3)
            worst_truth = max(worst_truth, w)
            if exact:
                worst_strict = max(worst_strict, w)
            ok_checked += 1
            if not ok:
                truth_bad.append((label, w))
    check(f"{len(cases)} series: C and Python agree on every status and every double, to the last bit", not bad, str(bad[:2]))
    check("every series ends the way its construction says it must", not expect_bad, str(expect_bad[:3]))
    check(f"the matrix maps voxels to where DICOM says they are, for all {ok_checked} accepted series "
          f"(exact stacks within {worst_strict:.1e} mm, real files within {worst_real:.1e} mm, noisy ones within their tolerance)", not truth_bad, str(truth_bad[:3]))
    # Known answers worked out by hand from the two standards (DICOM: x to the patient's left, y posterior, z superior;
    # NIfTI: x right, y anterior, z superior), independent of the code: the direction each voxel axis points to.
    #   axial r=(1,0,0) c=(0,1,0): columns run Left, rows run Posterior, slices run Superior
    #   flipped r=(-1,0,0): columns run Right, and the normal (0,0,-1) makes slices run Inferior
    #   sagittal r=(0,1,0) c=(0,0,-1): columns Posterior, rows Inferior, normal (-1,0,0) -> slices run Right
    #   coronal r=(1,0,0) c=(0,0,-1): columns Left, rows Inferior, normal (0,1,0) -> slices run Posterior
    known = {"axial": "LPS", "axial flipped": "RPI", "sagittal": "PIR", "coronal": "LIP"}
    got = {}
    for name in known:
        r = run_c([stack(4, name)])[0]
        A = [unhex(x) for x in r["affine"].split(",")]
        codes = ""
        for col in range(3):
            v = (A[col], A[4 + col], A[8 + col])
            a = max(range(3), key=lambda q: abs(v[q]))
            codes += ("RAS"[a] if v[a] > 0 else "LPI"[a])
        got[name] = codes
    check("voxel axes of the standard orientations point where the DICOM and NIfTI definitions say (L P S, R P I, P I R, L I P)",
          got == known, str(got))
    want = set(ds.GEOM_ERRORS) | {"PARSE", "CHECK"}
    check("the series reach every geometry error class", reached >= want, "never reached: " + ", ".join(sorted(want - reached)))
    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    sys.exit(1 if failures else 0)


main()
