#!/usr/bin/env python3
"""Tests for scripts/fetch_tcia.py without any network: the archive checks, the naming and the plan handling.

The downloaded archives are untrusted, so every way a zip can try to write outside its folder or exhaust the disk is tried:
absolute and ".." paths, sub-folders, symbolic links, an oversized member, a zip bomb, duplicate names, the wrong
number of images, a file that is not DICOM, a corrupt archive, a name with a NUL or a colon. After every refusal
nothing may be left in the target folder. The HTTP download is replaced by a function that serves prepared bytes.
"""
import csv, importlib.util, io, os, shutil, stat, sys, tempfile, zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("fetch_tcia", os.path.join(ROOT, "scripts", "fetch_tcia.py"))
ft = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ft)
failures = 0


def check(name, ok, detail=""):
    global failures
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))
    failures += not ok


DCM = b"\0" * 128 + b"DICM" + b"payload"


def make_zip(members, symlink=None, compress=zipfile.ZIP_DEFLATED):
    """members: list of (name, bytes). symlink: names that are stored as symbolic links."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compress) as z:
        for name, data in members:
            zi = zipfile.ZipInfo(name)
            zi.compress_type = compress
            zi.external_attr = ((stat.S_IFLNK | 0o777) << 16) if symlink and name in symlink else ((stat.S_IFREG | 0o644) << 16)
            z.writestr(zi, data)
    return buf.getvalue()


def good(n=3, extra=True):
    return [("%08d.dcm" % k, DCM + bytes([k])) for k in range(n)] + ([("LICENSE", b"CC BY 4.0")] if extra else [])


def unpack(blob, n=3, size=None):
    dest = tempfile.mkdtemp(prefix="fetch-test-")
    try:
        try:
            ft.unpack_checked(blob, dest, n, size if size is not None else sum(len(d) for _, d in good(n)))
            return None, sorted(os.listdir(dest))
        except ValueError as e:
            return str(e), sorted(os.listdir(dest))
    finally:
        shutil.rmtree(dest, ignore_errors=True)


def main():
    # ---- an honest archive
    err, files = unpack(make_zip(good()))
    check("an honest archive (3 images and a licence file) unpacks, and the files are what was in it", err is None and files == ["00000000.dcm", "00000001.dcm", "00000002.dcm", "LICENSE"], str((err, files)))
    err, files = unpack(make_zip(good(extra=False)))
    check("an archive without a licence file is fine too", err is None and len(files) == 3, str(err))
    err, files = unpack(make_zip(good(3), compress=zipfile.ZIP_STORED))
    check("a stored (uncompressed) archive is fine", err is None)

    # ---- hostile archives: each must be refused, and leave nothing behind
    evil = {
        "an absolute path": make_zip(good() + [("/etc/cron.d/x", b"x")]),
        "a parent-folder path": make_zip(good() + [("../escape.dcm", DCM)]),
        "a nested parent path": make_zip(good() + [("a/../../escape.dcm", DCM)]),
        "a sub-folder member": make_zip(good() + [("sub/00000009.dcm", DCM)]),
        "a Windows path": make_zip(good() + [("..\\escape.dcm", DCM)]),
        "a drive letter": make_zip(good() + [("C:evil.dcm", DCM)]),
        "a NUL in the name": make_zip(good() + [("evil\0.dcm", DCM)]),
        "a symbolic link": make_zip(good(2) + [("00000002.dcm", b"/etc/passwd")], symlink={"00000002.dcm"}),
        "a folder entry": make_zip(good() + [("folder/", b"")]),
        "duplicate names": make_zip(good(3) + [("00000001.DCM", DCM)]),
        "a not-DICOM image file": make_zip(good(2) + [("00000002.dcm", b"this is not dicom" * 20)]),
        "too few images": make_zip(good(2)),
        "too many images": make_zip(good(4)),
        "another file besides LICENSE": make_zip(good() + [("a.txt", b"1")]),
        "a second licence-like file": make_zip(good() + [("LICENSE.txt", b"1")]),
        "a file called only dots": make_zip(good() + [("..", b"1")]),
        "a space in a name": make_zip(good() + [("00000009 .dcm", DCM)]),
        "not a zip at all": b"PK this is not a zip file" * 10,
        "an empty body": b"",
    }
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")                                            # zipfile warns about the duplicate name it is told to write
        evil["the same image name twice (the image count still right)"] = make_zip([good()[0], good()[1], good()[1], good()[3]])
        evil["the licence file twice"] = make_zip(good() + [("LICENSE", b"again")])
    stored = make_zip(good(), compress=zipfile.ZIP_STORED)
    k = stored.index(b"payload")
    evil["a member whose data is corrupt (checksum mismatch)"] = stored[:k] + b"X" + stored[k + 1:]
    def patch(blob, at_local, at_central, value):
        """Set a 2-byte field in the first local file header and the first central directory entry."""
        b = bytearray(blob)
        for sig, off in ((b"PK\x03\x04", at_local), (b"PK\x01\x02", at_central)):
            k0 = b.index(sig) + off
            b[k0:k0 + 2] = value
        return bytes(b)
    evil["encrypted members"] = patch(stored, 6, 8, b"\x01\x00")                  # the 'encrypted' bit of the general purpose flags
    odd = patch(stored, 8, 10, b"\x63\x00")                                       # compression method 99
    evil["an unsupported compression method"] = odd
    bad = []
    for label, blob in evil.items():
        err, files = unpack(blob)
        if err is None or files:
            bad.append((label, err, files))
    check(f"{len(evil)} hostile or wrong archives: each is refused with a reason and nothing is written to the folder", not bad, str(bad[:3]))
    why = {k: (unpack(v)[0] or "") for k, v in evil.items()}
    check("corrupt, encrypted and unsupported-compression archives are refused as ValueError with a reason (never an uncaught zipfile exception)",
          all(why2 and ("cannot read" in why2 or "corrupt" in why2) for why2 in (unpack(evil[k])[0] for k in ("a member whose data is corrupt (checksum mismatch)", "encrypted members", "an unsupported compression method"))),
          str([unpack(evil[k])[0] for k in ("a member whose data is corrupt (checksum mismatch)", "encrypted members", "an unsupported compression method")]))
    check("duplicate names are refused as such even when the image count is right", "duplicate" in (unpack(evil["the same image name twice (the image count still right)"])[0] or "") and "duplicate" in (unpack(evil["the licence file twice"])[0] or ""))
    check("the reasons are specific (unsafe name for paths, spaces and dots; a NUL-cut name seen as the stray file 'evil'; symbolic link; preamble; expected 3 .dcm files; not a zip)",
          all("unsafe member name" in why[k] for k in ("a parent-folder path", "a space in a name", "a file called only dots", "an absolute path", "a sub-folder member"))
          and "symbolic" in why["a symbolic link"] and "preamble" in why["a not-DICOM image file"] and "expected 3" in why["too few images"]
          and "expected 3" in why["another file besides LICENSE"] and "'evil'" in why["a NUL in the name"] and "not a zip" in why["not a zip at all"], str(why))

    # ---- sizes
    bomb = make_zip([("%08d.dcm" % k, DCM + b"\0" * (30 * 1024 ** 2)) for k in range(3)])         # tiny archive, 90 MB unpacked
    err, files = unpack(bomb, n=3, size=1024)
    check("a zip bomb (a small archive that unpacks to far more than the catalogue says) is refused before anything is written",
          err is not None and "zip bomb" in err and not files, str((err, files)))
    old = ft.MAX_MEMBER
    ft.MAX_MEMBER = 1000
    err, files = unpack(make_zip([("00000000.dcm", DCM + b"\0" * 2000)]), n=1, size=5000)
    ft.MAX_MEMBER = old
    check("a member larger than the member limit is refused", err is not None and "larger than" in err and not files, str(err))
    total = sum(len(d) for _, d in good())
    old_slack = ft.SLACK
    ft.SLACK = 100
    ok_edge = unpack(make_zip(good()), size=total - 100)[0]
    over_edge = unpack(make_zip(good()), size=total - 101)
    ft.SLACK = old_slack
    check("the byte total may exceed the catalogue's by exactly the slack and not by one byte more (boundary)", ok_edge is None and over_edge[0] is not None and "zip bomb" in over_edge[0] and not over_edge[1],
          str((ok_edge, over_edge)))

    # ---- naming and classification
    def x(desc, **kw):
        return dict({"SeriesDescription": desc, "PatientID": "UPENN-GBM-00001", "SeriesInstanceUID": "1.2.3", "ImageCount": 1, "FileSize": 1}, **kw)
    cases = [("AXIAL T2 FLAIR: Processed_CaPTk", "FLAIR"), ("T2 AXIAL FLAIR POST : Processed_CaPTk", "FLAIR"), ("AX T1 SE C+ : Processed_CaPTk", "T1c"),
             ("AX T1 MPRAGE STEALTH POST : Processed_CaPTk", "T1c"), ("AX T1 3D PRE: Processed_CaPTk", "T1"), ("T1 AXIAL SE : Processed_CaPTk", "T1"),
             ("AX T1 MPRAGE ISOTROPIC: Processed_CaPTk", "T1"), ("T2 AXIAL: Processed_CaPTk", "T2"), ("Axial T2 tse: Processed_CaPTk", "T2"),
             ("AX T1 FLOW COMP POST : Processed_CaPTk", "T1c"), ("something else: Processed_CaPTk", "OTHER"), ("PERFUSION GRE_TERA", "PERF"),
             ("DTI", "DTI"), ("ep2d_diff_MDDW_IPAT", "DTI"), ("AX DTI 3O DIRECTION", "DTI"), ("localizer", "RAW"), ("", "RAW")]
    wrong = [(d, want, ft.classify(x(d))) for d, want in cases if ft.classify(x(d)) != want]
    check("the labels follow the keyword rules for %d real description styles" % len(cases), not wrong, str(wrong[:3]))
    rows = ft.plan_rows([x("T1 AXIAL: Processed_CaPTk", SeriesInstanceUID="1.%d" % k) for k in range(3)] + [x("T2 AXIAL: Processed_CaPTk", SeriesInstanceUID="2.1")])
    check("duplicate labels in one patient get -2, -3 (T1, T1-2, T1-3, T2) and the names are valid for the anonymiser's labels",
          sorted(r["folder"] for r in rows) == ["UPENN-GBM-00001_T1", "UPENN-GBM-00001_T1-2", "UPENN-GBM-00001_T1-3", "UPENN-GBM-00001_T2"]
          and all(__import__("re").fullmatch(r"([A-Za-z0-9-]{1,32})_([A-Za-z0-9_-]{1,32})", r["folder"]) for r in rows))
    check("the plan is stable: the same input gives the same names in the same order", [r["folder"] for r in rows] == [r["folder"] for r in ft.plan_rows(list(reversed([x("T1 AXIAL: Processed_CaPTk", SeriesInstanceUID="1.%d" % k) for k in range(3)] + [x("T2 AXIAL: Processed_CaPTk", SeriesInstanceUID="2.1")])))])

    # ---- get(): plan validation, atomic folders, skipping what is there, the log
    work = tempfile.mkdtemp(prefix="fetch-get-")
    try:
        out = os.path.join(work, "out")
        served = {}

        def fake_get(url, timeout=0, retries=0):
            uid = url.split("SeriesInstanceUID=")[1]
            if uid not in served:
                raise SystemExit("no such series")
            return served[uid]
        ft.http_get = fake_get

        def plan(rows):
            p = os.path.join(work, "plan.csv")
            with open(p, "w", newline="") as f:
                w = csv.DictWriter(f, ft.FIELDS, lineterminator="\n")
                w.writeheader()
                w.writerows(rows)
            return p
        total = sum(len(d) for _, d in good(3))
        R = lambda folder, uid, n=3, size=None: {"folder": folder, "series_uid": uid, "patient": "P", "description": "d", "images": n, "bytes": size or total}
        U1, U2, U3 = "1.3.6.1.4.1.14519.5.2.1.1", "1.3.6.1.4.1.14519.5.2.1.2", "1.3.6.1.4.1.14519.5.2.1.3"
        served[U1], served[U2], served[U3] = make_zip(good()), make_zip(good(2)), make_zip(good() + [("../x.dcm", DCM)])
        rc = ft.get(plan([R("P_T1", U1), R("P_T2", U2), R("P_T1c", U3)]), out, True)
        check("get: a good series is placed in its folder, a series whose archive is wrong and one that is hostile are refused, the exit code says so",
              rc == 1 and sorted(d for d in os.listdir(out) if d != "FETCHED.csv") == ["P_T1"] and len(os.listdir(os.path.join(out, "P_T1"))) == 4)
        check("get: no temporary folder is left behind after refusals", not [d for d in os.listdir(out) if d.startswith(".fetch")], str(os.listdir(out)))
        log = list(csv.reader(open(os.path.join(out, "FETCHED.csv"))))
        check("get: only the series that arrived are in FETCHED.csv", len(log) == 2 and log[1][0] == "P_T1" and log[0][-1] == "fetched", str(log))
        mark = os.path.join(out, "P_T1", "00000000.dcm")
        open(mark, "wb").write(b"mine")
        rc = ft.get(plan([R("P_T1", U1)]), out, True)
        check("get: a folder that exists is never touched (skipped, not overwritten)", rc == 0 and open(mark, "rb").read() == b"mine")
        for label, row in [("a folder name with a slash", R("../evil", U1)), ("a folder name with a dot-dot", R("a..b/c", U1)), ("a series UID with a quote", R("ok", "1.3.6.1.4.1.14519.5.2.1.1'; rm -rf")),
                           ("a series UID with a path", R("ok", "../../../../../x"))]:
            try:
                ft.get(plan([row]), os.path.join(work, "out2"), True)
                check("get: a plan with " + label + " is refused", False)
            except SystemExit as e:
                check("get: a plan with " + label + " is refused before any download", "unsafe" in str(e) and not os.path.exists(os.path.join(work, "out2")), str(e))
        bad_plan = os.path.join(work, "bad.csv")
        open(bad_plan, "w").write("a,b\n1,2\n")
        try:
            ft.get(bad_plan, out, True)
            check("get: a file that is not a plan is refused", False)
        except SystemExit as e:
            check("get: a file that is not a plan is refused", "not a plan" in str(e))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    sys.exit(1 if failures else 0)


main()
