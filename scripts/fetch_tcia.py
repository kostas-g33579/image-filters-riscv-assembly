#!/usr/bin/env python3
"""Fetch DICOM series of the TCIA collection UPENN-GBM (CC BY 4.0, public, no login) into data/fetched/.

    fetch_tcia.py plan --border              print the plan for the border-case set (nothing is downloaded)
    fetch_tcia.py plan --patients 00001 00002 --kinds T1 T1c T2 FLAIR
    fetch_tcia.py get PLAN.csv [--out data/fetched] [--yes]

A plan is a CSV with the columns folder,series_uid,patient,description,images,bytes (the `plan` command writes one to
stdout). `get` downloads each series as a zip from the TCIA NBIA API and unpacks it into OUT/<folder>/. The archive is
UNTRUSTED input and is checked before anything is written into data/: no absolute or ".." paths, no sub-folders, no
symbolic links, no member larger than 256 MB, a total no larger than the catalogue's byte count plus 10 MB (a zip
bomb), exactly the catalogue's number of images plus the licence file, and every .dcm-like member must start with the
DICOM preamble. It unpacks into a temporary folder and renames it into place only when all checks pass; an existing
folder is never touched. Everything downloaded is appended to OUT/FETCHED.csv. Standard library only.

The default folder is data/fetched, NOT data/dicom: data/dicom is the fixed set of 12 series the regression tests are
written for (data/dicom/MANIFEST.txt); fetched series, which include deliberately odd ones, must not change it.

The series catalogue (a list of every series with its size) is fetched once from the NBIA API and cached in
data/tcia_catalogue.json; delete the file to refresh it.

Labels in the folder names come from the series description by simple keyword rules (see classify()); `plan` prints
them so you can check them before downloading. They are only labels for naming and batching: the anonymiser never
reads a description.
"""
import argparse, collections, csv, datetime, io, json, os, re, shutil, sys, tempfile, time, urllib.parse, urllib.request, zipfile, zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = "https://services.cancerimagingarchive.net/nbia-api/services/v1"
COLLECTION = "UPENN-GBM"
UA = "tcia-fetch-research-script/1.0"
MAX_MEMBER = 256 * 1024 ** 2
SLACK = 10 * 1024 ** 2
FIELDS = ["folder", "series_uid", "patient", "description", "images", "bytes"]


def http_get(url, timeout=120, retries=3):
    last = None
    for k in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:                                   # noqa: BLE001 - retried, then reported
            last = e
            time.sleep(2 * (k + 1))
    raise SystemExit("download failed: %s: %s" % (url.split("?")[0], last))


def catalogue(path=os.path.join(ROOT, "data", "tcia_catalogue.json")):
    if not os.path.exists(path):
        print("fetching the series catalogue from TCIA ...", file=sys.stderr)
        data = http_get(API + "/getSeries?" + urllib.parse.urlencode({"Collection": COLLECTION, "format": "json"}), timeout=300)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "wb").write(data)
    return json.load(open(path))


def is_processed(x):
    return "Processed_CaPTk" in (x.get("SeriesDescription") or "")


def classify(x):
    """A short label for a series from its description. Deliberately simple and printed in the plan."""
    s = (x.get("SeriesDescription") or "").lower()
    if not is_processed(x):
        if re.search(r"perf|bolus|dsc", s):
            return "PERF"
        if re.search(r"dti|diff|dwi|mddw|trace|adc", s):
            return "DTI"
        return "RAW"
    if "flair" in s:
        return "FLAIR"
    if "t1" in s or "mprage" in s:
        return "T1c" if re.search(r"post|c\+|gad|\bce\b|contrast|t1ce", s) else "T1"
    if "t2" in s:
        return "T2"
    return "OTHER"


def plan_rows(chosen):
    """Rows with unique folder names <patient>_<label>[-2, -3 ...]."""
    seen = collections.Counter()
    rows = []
    for x in sorted(chosen, key=lambda x: (x["PatientID"], classify(x), x["SeriesInstanceUID"])):
        base = "%s_%s" % (x["PatientID"], classify(x))
        seen[base] += 1
        folder = base if seen[base] == 1 else "%s-%d" % (base, seen[base])
        rows.append({"folder": folder, "series_uid": x["SeriesInstanceUID"], "patient": x["PatientID"],
                     "description": (x.get("SeriesDescription") or "").replace(",", ";").replace("\n", " "),
                     "images": x["ImageCount"], "bytes": x["FileSize"]})
    return rows


def border_set(cat, have):
    """The extremes of the collection plus two unusual patients (see docs/dicom.md)."""
    P = [x for x in cat if is_processed(x) and x["PatientID"] not in have]
    R = [x for x in cat if not is_processed(x) and x["PatientID"] not in have]
    pat = collections.defaultdict(list)
    for x in cat:
        pat[x["PatientID"]].append(x)
    chosen = {}

    def add(x):
        chosen[x["SeriesInstanceUID"]] = x
    add(max(P, key=lambda x: x["FileSize"]))                                          # largest series
    add(max(P, key=lambda x: x["FileSize"] / x["ImageCount"]))                        # largest file
    add(min(P, key=lambda x: (x["ImageCount"], -x["FileSize"])))                      # fewest slices
    add(min([x for x in P if "GE" in (x.get("Manufacturer") or "") and x["ImageCount"] <= 64], key=lambda x: x["FileSize"]))
    add(min([x for x in P if "Vida" in (x.get("ManufacturerModelName") or "")], key=lambda x: x["FileSize"]))
    add(min(R, key=lambda x: x["FileSize"]))                                          # smallest raw series
    add(min([x for x in R if re.search(r"dti", (x.get("SeriesDescription") or "").lower())], key=lambda x: x["FileSize"]))
    for k in (min((k for k, v in pat.items() if sum(1 for x in v if is_processed(x)) >= 8 and k not in have),
                  key=lambda k: sum(x["FileSize"] for x in pat[k] if is_processed(x))),
              min((k for k, v in pat.items() if sum(1 for x in v if is_processed(x)) == 2 and k not in have),
                  key=lambda k: sum(x["FileSize"] for x in pat[k] if is_processed(x)))):
        for x in pat[k]:
            if is_processed(x):
                add(x)
    return list(chosen.values())


# ------------------------------------------------------------------ safe unpacking
SAFE_NAME = re.compile(r"[A-Za-z0-9_.-]{1,80}\Z")


def unpack_checked(blob, dest, want_images, want_bytes):
    """Unpack the zip `blob` into the existing empty folder `dest`, or raise ValueError. Nothing is written until every member
    has been checked, and nothing outside dest is ever touched. A genuine archive holds numbered .dcm files and one file
    called exactly LICENSE (27 of 27 seen); anything else is refused."""
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as e:
        raise ValueError("not a zip file: %s" % e)
    try:
        bad = z.testzip()
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, zlib.error, EOFError) as e:
        raise ValueError("cannot read the archive (corrupt, encrypted or an unsupported compression method): %s" % e)
    if bad:
        raise ValueError("corrupt member %r" % bad)
    infos = z.infolist()
    total = 0
    names = set()
    for i in infos:
        n = i.filename
        # zipfile cuts a name at a NUL, so what is checked here is what would be written
        if i.is_dir() or not SAFE_NAME.match(n) or set(n) <= {"."}:
            raise ValueError("unsafe member name %r (only plain file names are accepted: no folders, paths or special characters)" % n)
        if (i.external_attr >> 16) & 0o170000 not in (0, 0o100000):
            raise ValueError("member %r is not a regular file (symbolic link?)" % n)
        if i.file_size > MAX_MEMBER:
            raise ValueError("member %r is larger than %d MB" % (n, MAX_MEMBER >> 20))
        if n.lower() in names:
            raise ValueError("duplicate member name %r" % n)
        names.add(n.lower())
        total += i.file_size
    if total > want_bytes + SLACK:
        raise ValueError("the archive unpacks to %d bytes, the catalogue says %d (zip bomb?)" % (total, want_bytes))
    dcm = [i for i in infos if i.filename.lower().endswith(".dcm")]
    others = [i for i in infos if i not in dcm]
    if len(dcm) != want_images or any(i.filename != "LICENSE" for i in others) or len(others) > 1:
        raise ValueError("expected %d .dcm files and at most one file called LICENSE, found %d .dcm and %s"
                         % (want_images, len(dcm), [i.filename for i in others] or "no others"))
    data = {}
    for i in infos:                                                   # read and check everything first
        try:
            data[i.filename] = z.read(i)
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError, zlib.error, EOFError) as e:
            raise ValueError("cannot read member %r: %s" % (i.filename, e))
        if i in dcm and data[i.filename][128:132] != b"DICM":
            raise ValueError("%r does not start with the DICOM preamble" % i.filename)
    for name, content in data.items():                                # only now write
        with open(os.path.join(dest, name), "wb") as f:
            f.write(content)
    return len(dcm)


def get(plan, out, yes):
    rows = list(csv.DictReader(open(plan)))
    if not rows or list(rows[0].keys()) != FIELDS:
        raise SystemExit("%s is not a plan file (columns must be %s)" % (plan, ",".join(FIELDS)))
    for r in rows:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", r["folder"]) or not re.fullmatch(r"[0-9.]{10,128}", r["series_uid"]):
            raise SystemExit("unsafe folder name or series UID in the plan: %r" % r)
    todo = [r for r in rows if not os.path.exists(os.path.join(out, r["folder"]))]
    mb = sum(int(r["bytes"]) for r in todo) / 1e6
    print("%d series in the plan, %d to download (%.0f MB), %d already present" % (len(rows), len(todo), mb, len(rows) - len(todo)))
    if todo and not yes:
        if input("download? [y/N] ").strip().lower() != "y":
            return 1
    os.makedirs(out, exist_ok=True)
    log = os.path.join(out, "FETCHED.csv")
    new_log = not os.path.exists(log)
    failed = 0
    for k, r in enumerate(todo, 1):
        t0 = time.monotonic()
        url = API + "/getImage?" + urllib.parse.urlencode({"SeriesInstanceUID": r["series_uid"]})
        tmp = tempfile.mkdtemp(prefix=".fetch-", dir=out)
        try:
            blob = http_get(url, timeout=600)
            unpack_checked(blob, tmp, int(r["images"]), int(r["bytes"]))
            os.rename(tmp, os.path.join(out, r["folder"]))
            with open(log, "a", newline="") as f:
                w = csv.writer(f)
                if new_log:
                    w.writerow(FIELDS + ["fetched"])
                    new_log = False
                w.writerow([r[c] for c in FIELDS] + [datetime.date.today().isoformat()])
            print("[%d/%d] %-28s %4s images %7.1f MB  %5.1f s  ok" % (k, len(todo), r["folder"], r["images"], len(blob) / 1e6, time.monotonic() - t0))
        except (ValueError, SystemExit, OSError) as e:
            failed += 1
            print("[%d/%d] %-28s FAILED: %s" % (k, len(todo), r["folder"], e))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return 1 if failed else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--border", action="store_true", help="the border-case set")
    p.add_argument("--patients", nargs="*", default=[], help="patient numbers, e.g. 00130 (the UPENN-GBM- prefix is added)")
    p.add_argument("--kinds", nargs="*", default=["T1", "T1c", "T2", "FLAIR"], help="labels to keep for --patients")
    p.add_argument("--out", default=os.path.join(ROOT, "data", "fetched"))
    g = sub.add_parser("get")
    g.add_argument("plan")
    g.add_argument("--out", default=os.path.join(ROOT, "data", "fetched"))
    g.add_argument("--yes", action="store_true")
    a = ap.parse_args()
    if a.cmd == "get":
        return get(a.plan, a.out, a.yes)
    cat = catalogue()
    # patients of the fixed regression set (data/dicom) are never chosen, wherever the fetched series are going
    reg = os.path.join(ROOT, "data", "dicom")
    have = {d.split("_")[0] for d in os.listdir(reg)} if os.path.isdir(reg) else set()
    chosen = border_set(cat, have) if a.border else []
    for n in a.patients:
        pid = n if n.startswith("UPENN-GBM-") else "UPENN-GBM-" + n
        chosen += [x for x in cat if x["PatientID"] == pid and classify(x) in a.kinds]
    if not chosen:
        raise SystemExit("nothing selected (use --border or --patients)")
    rows = plan_rows({x["SeriesInstanceUID"]: x for x in chosen}.values())
    w = csv.DictWriter(sys.stdout, FIELDS, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    print("# %d series, %.0f MB" % (len(rows), sum(int(r["bytes"]) for r in rows) / 1e6), file=sys.stderr)
    for r in rows:
        print("#   %-28s %4s images %7.1f MB  %s" % (r["folder"], r["images"], int(r["bytes"]) / 1e6, r["description"][:44]), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
