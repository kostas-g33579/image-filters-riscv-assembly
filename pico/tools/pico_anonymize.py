#!/usr/bin/env python3
"""Host client for the on-device DICOM -> NIfTI anonymiser (pico/src/anon.c).

Sends a folder of DICOM files to the Pico and writes what comes back: one NIfTI-1 file and one JSON sidecar. The
device is the trust boundary: it receives the raw DICOM files and returns only voxel values, a header built from
constants and validated numbers, and a JSON built the same way (see docs/dicom.md); nothing the files say about
a person or a site can reach the output. This script never reads a patient value into the output either: the only
names in it are the labels you give.

    pico_anonymize.py SERIES_DIR --subject P017 --sequence T1c [--site S02] [--scanner C01]
                      [--series K] [--out output/anonymized] [--overwrite]
                      [--port /dev/ttyACM1 | --exec "qemu-riscv32 build32/anon_test"]

Result: OUT/<subject>_<sequence>/<subject>_<sequence>.nii and .json, and nothing else. The files are written to a
temporary folder and moved into place only when the device says it is done and the host-side checks agree, so a
failed run leaves no output. An existing result folder is never overwritten unless --overwrite is given. When run
with sudo the results belong to the user who ran sudo.

Labels are 1 to 32 characters of A-Z a-z 0-9 _ - (they become the folder and file names and appear in the JSON).
Site and scanner default to NA. A folder with several series needs --series K (1 = the most slices).

Batch: `--batch ROOT` anonymises every sub-folder of ROOT named <subject>_<sequence> (the labels come from the folder
name; names that do not fit are skipped and reported), on ONE connection, one after the other:

    pico_anonymize.py --batch data/dicom [--only '*_T1c'] [--manifest FILE.csv] [--site S02] [--scanner C01]
                      [--out DIR] [--overwrite] [--stop-on-error] [--report FILE.csv]

A manifest (CSV with the header folder,subject,sequence and optionally site,scanner,series) names exactly the folders
to do and their labels, for folders that are not called <subject>_<sequence>. Cases that are already done (both
result files exist) are skipped, so an interrupted batch is simply started again; a case whose result folder is
missing a file is done again. A case that fails does not stop the others (use --stop-on-error to stop); losing the
device does. At the end a table lists every case; the exit code is 0 when nothing failed, 2 when something did.
The labels are yours: use study codes, not patient identifiers, for real data. The client cannot tell the difference.

Wire format: pico/src/anon.h.
"""
import argparse, collections, csv, fnmatch, importlib.util, json, os, re, shutil, struct, sys, tempfile, threading, time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REQ_MAGIC, REPLY_MAGIC = b"DCM1", b"NII1"
NO_FILE = 0xFFFFFFFF
HEADER_BYTES = 352
DRAIN_SETTLE = 6.0          # the device gives up on a silent host after 5 s (ANON_IO_TIMEOUT_MS); wait longer than that
LABEL_RE = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


pf = _load("pico_filter", os.path.join(ROOT, "pico", "tools", "pico_filter.py"))
ds = _load("dicom_slice", os.path.join(ROOT, "scripts", "dicom_slice.py"))

EXPLAIN = {
    "REQUEST:BAD_LABELS": "a label is not 1 to 32 characters of A-Z a-z 0-9 _ -",
    "REQUEST:BAD_COUNT": "the number of files is not between 1 and 4096",
    "REQUEST:BAD_FLAGS": "the device does not know a flag that was sent (different firmware version?)",
    "REQUEST:TIMEOUT": "the request header did not arrive in full",
    "FILE:TIMEOUT": "the data stopped arriving",
    "FILE:TOO_LARGE": "the file is larger than 64 MiB",
    "PARSE:PREAMBLE": "not a DICOM file (no DICM at byte 128)",
    "PARSE:NO_TS": "no transfer syntax in the file meta information",
    "PARSE:TS": "a transfer syntax that is not supported (compressed, deflated or big endian)",
    "PARSE:ENCAPSULATED": "the pixel data is compressed",
    "PARSE:NO_PIXEL": "no pixel data",
    "PARSE:PIXEL_TRUNC": "the pixel data runs past the end of the file (truncated file?)",
    "PARSE:HEADER_TOO_LARGE": "the header does not end within the first 128 KB of the file",
    "PARSE:TOO_MANY": "too many elements",
    "PARSE:DEPTH": "sequences nested too deeply",
    "PARSE:MALFORMED": "a value the reader needs is malformed",
    "PARSE:VR_MISMATCH": "a value has the wrong value representation",
    "CHECK:MISSING_TAG": "a required image tag is missing",
    "CHECK:COLOUR": "a colour image (only greyscale is supported)",
    "CHECK:PHOTOMETRIC": "an unsupported photometric interpretation",
    "CHECK:MULTIFRAME": "several frames in one file (not supported)",
    "CHECK:SIZE": "an invalid image size",
    "CHECK:BITS_ALLOC": "bits allocated is not 8 or 16",
    "CHECK:BITS_INCONSISTENT": "bits stored / high bit are inconsistent",
    "CHECK:PIXEL_REP": "an invalid pixel representation",
    "CHECK:PIXEL_SHORT": "less pixel data than rows x columns",
    "GEOM:NO_GEOMETRY": "no position / orientation / spacing in the file",
    "GEOM:ORIENTATION": "the orientation vectors are not a valid rotation",
    "GEOM:MISMATCH": "this slice does not match the first one (size, spacing or orientation)",
    "GEOM:SERIES": "this file belongs to a different series",
    "GEOM:NOT_SORTED": "the slices are not in increasing order along the slice normal",
    "GEOM:UNEVEN": "the slices are not evenly spaced (a gap, a missing slice or duplicates)",
    "NII:PHOTOMETRIC": "MONOCHROME1 (inverted) images are not converted",
    "NII:FORMAT": "this slice's bit layout or signedness differs from the first slice's",
    "NII:RESCALE": "the rescale slope or intercept differs between slices (or the slope is zero)",
    "NII:TOO_LARGE": "a dimension above 32767 does not fit a NIfTI-1 header",
    "SIDECAR:JSON_TOO_LONG": "the JSON sidecar would be too long",
}


class AnonError(Exception):
    fatal = False               # True when the connection itself is gone: the rest of a batch cannot be done either
    device_idle = False         # True when the device is known to be waiting for a new request (nothing left to discard)


def valid_label(s):
    return bool(LABEL_RE.match(s))


# ------------------------------------------------------------------ choosing and ordering the series (host side)
def collect_series(path, which=None):
    """Paths of the files of one series in slice order, and the notes to show. The device re-checks the order, so a
    wrong sort here can only cause a refusal, never a wrong volume."""
    try:
        images, skipped = ds.load_series(path)
        groups = {}
        for im in images:
            groups.setdefault(im.tags.get("SeriesInstanceUID") or "(none)", []).append(im)
        glist = sorted(groups.values(), key=lambda g: -len(g))
        if len(glist) > 1:
            if which is None:
                lines = ["%s holds %d series; choose one with --series K:" % (path, len(glist))]
                for k, g in enumerate(glist, 1):
                    t = g[0].tags
                    lines.append("  --series %d : %d slices, %s x %s, %s" % (k, len(g), t.get("Columns"), t.get("Rows"), t.get("Modality")))
                raise AnonError("\n".join(lines))
            if not 1 <= which <= len(glist):
                raise AnonError("--series %d is out of range 1..%d" % (which, len(glist)))
        series = glist[(which or 1) - 1]
        ordered, method, _, warns = ds.order_slices(series)
    except ds.DicomError as e:
        raise AnonError(str(e))
    except OSError as e:
        raise AnonError("cannot read %s: %s" % (path, e))
    notes = ["%d file(s) in %s are not DICOM and were skipped" % (skipped, path)] if skipped else []
    notes += warns
    return [i.path for i in ordered], method, notes


# ------------------------------------------------------------------ the conversation with the device
def request_bytes(n, labels):
    r = REQ_MAGIC + struct.pack("<II", 0, n)
    return r + b"".join(struct.pack("B", len(x)) + x for x in (s.encode("ascii") for s in labels))


def read_frame(link):
    t = link.read_exact(1)
    (n,) = struct.unpack("<I", link.read_exact(4))
    return t, (link.read_exact(n) if n else b"")


def explain(code, names, idx):
    where = "the request" if idx == NO_FILE else ("file %d (%s)" % (idx, os.path.basename(names[idx])) if idx < len(names) else "file %d" % idx)
    return "the device refused %s: %s%s" % (where, code, " - " + EXPLAIN[code] if code in EXPLAIN else "")


def talk(link, paths, labels, nii_out, log=lambda s: None, timeline=None):
    """Send the series and receive the result. Writes the voxels to nii_out (after a 352-byte gap) and returns
    (header bytes, json bytes). Raises AnonError. `state['sent_all']` tells the caller whether the device is idle."""
    state = {"sent_all": False, "request_sent": False, "sent": 0, "recv": 0}
    t_start = time.monotonic()
    last_sample = [t_start]

    def sample(force=False):
        """timeline: (seconds, MB sent, MB received) about once a second, for --timing"""
        now = time.monotonic()
        if timeline is not None and (force or now - last_sample[0] >= 1.0):
            last_sample[0] = now
            timeline.append((now - t_start, state["sent"] / 1e6, state["recv"] / 1e6, state["sent_all"]))
    try:
        link.write(request_bytes(len(paths), labels))
        state["request_sent"] = True
        if link.read_exact(4) != REPLY_MAGIC:
            e = AnonError("the device did not answer like the anonymiser (is the right firmware flashed?)")
            e.fatal = True
            raise e
        t, p = read_frame(link)
    except (TimeoutError, EOFError, OSError) as e:
        e = AnonError("lost the device: %s" % (str(e) or type(e).__name__))
        e.fatal = True
        raise e
    if t == b"E":
        e = AnonError(explain(p[4:].decode("ascii", "replace"), paths, struct.unpack_from("<I", p)[0]))
        e.device_idle = True            # a refusal of the request itself: no files were sent, nothing to discard
        raise e
    if t != b"A":
        raise AnonError("unexpected reply from the device: %r" % t)

    stop = threading.Event()
    send_err = []
    total = sum(os.path.getsize(x) for x in paths)

    def sender():
        try:
            sent = 0
            for k, path in enumerate(paths):
                if stop.is_set():
                    return
                with open(path, "rb") as f:
                    b = f.read()
                link.write(struct.pack("<I", len(b)) + b)
                sent += len(b)
                state["sent"] = sent
                log("sending: file %d of %d (%d%%)" % (k + 1, len(paths), 100 * sent // max(total, 1)))
            state["sent_all"] = True
        except Exception as e:          # noqa: BLE001 - reported by the main thread
            send_err.append(e)

    th = threading.Thread(target=sender, daemon=True)
    th.start()
    header = js = None
    nvox = 0
    try:
        with open(nii_out, "wb") as out:
            out.write(b"\0" * HEADER_BYTES)
            while True:
                t, p = read_frame(link)
                if t == b"V":
                    out.write(p)
                    nvox += len(p)
                    state["recv"] = nvox
                    sample()
                elif t == b"H":
                    header = p
                elif t == b"J":
                    js = p
                elif t == b"D":
                    sample(force=True)
                    break
                elif t == b"E":
                    e = AnonError(explain(p[4:].decode("ascii", "replace"), paths, struct.unpack_from("<I", p)[0]))
                    e.device_idle = state["sent_all"] or struct.unpack_from("<I", p)[0] == NO_FILE
                    raise e
                else:
                    raise AnonError("unexpected frame from the device: %r" % t)
            if header is None or js is None:
                raise AnonError("the device finished without a header or a JSON")
            out.seek(0)
            out.write(header)
    except (TimeoutError, EOFError, OSError) as e:
        e = AnonError("lost the device: %s" % (str(e) or type(e).__name__))
        e.fatal = True
        raise e
    finally:
        stop.set()
        th.join(timeout=pf.TIMEOUT)
    if send_err:
        raise AnonError("could not send the files: %s" % send_err[0])
    return header, js, nvox


# ------------------------------------------------------------------ checks on what came back (defence in depth)
def check_result(header, js, nvox):
    """The device guarantees all of this; a corrupted link or a wrong firmware would break it, so check anyway."""
    if len(header) != HEADER_BYTES:
        raise AnonError("header is %d bytes, not %d" % (len(header), HEADER_BYTES))
    if struct.unpack_from("<i", header, 0)[0] != 348 or header[344:348] != b"n+1\0":
        raise AnonError("the header is not a NIfTI-1 header")
    if struct.unpack_from("<f", header, 108)[0] != 352.0:
        raise AnonError("the header's voxel offset is not 352")
    dim = struct.unpack_from("<8h", header, 40)
    bitpix = struct.unpack_from("<h", header, 72)[0]
    if dim[0] != 3 or min(dim[1:4]) < 1:
        raise AnonError("the header does not describe a 3-D volume: dim %s" % (dim,))
    if bitpix not in (8, 16) or nvox != dim[1] * dim[2] * dim[3] * bitpix // 8:
        raise AnonError("the voxel bytes (%d) do not match the header (dim %s, %d bits)" % (nvox, dim[1:4], bitpix))
    if any(c not in (10,) and not 32 <= c < 127 for c in js):
        raise AnonError("the JSON contains bytes that are not plain ASCII")
    try:
        meta = json.loads(js)
    except ValueError as e:
        raise AnonError("the JSON does not parse: %s" % e)
    return dim[1:4], bitpix, meta


# ------------------------------------------------------------------ putting the results in place
def _own(path):
    """When run with sudo, give the results to the user who ran sudo."""
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if os.geteuid() == 0 and uid and gid:
        os.chown(path, int(uid), int(gid))


def _makedirs_owned(path):
    missing = []
    p = os.path.abspath(path)
    while not os.path.isdir(p):
        missing.append(p)
        p = os.path.dirname(p)
    os.makedirs(path, exist_ok=True)
    for d in missing:
        _own(d)


def anonymize(link, series_dir, subject, sequence, site, scanner, out_root, overwrite=False, which=None, log=lambda s: None,
              settle=DRAIN_SETTLE, timeline=None):
    """The whole job. Returns (nii path, json path, info dict). Raises AnonError."""
    for what, v in (("subject", subject), ("sequence", sequence), ("site", site), ("scanner", scanner)):
        if not valid_label(v):
            raise AnonError("--%s %r is not 1 to 32 characters of A-Z a-z 0-9 _ -" % (what, v))
    name = "%s_%s" % (subject, sequence)
    final_dir = os.path.join(out_root, name)
    if os.path.exists(final_dir) and not overwrite:
        raise AnonError("%s already exists (use --overwrite to replace it)" % final_dir)
    paths, method, notes = collect_series(series_dir, which)
    for n in notes:
        log("note: " + n)
    log("%d slice(s), ordered by %s" % (len(paths), method))
    _makedirs_owned(out_root)
    tmp = tempfile.mkdtemp(prefix=".anon-", dir=out_root)
    finished = False
    started = False
    try:
        nii_tmp, json_tmp = os.path.join(tmp, name + ".nii"), os.path.join(tmp, name + ".json")
        started = True
        header, js, nvox = talk(link, paths, (subject, sequence, site, scanner), nii_tmp, log, timeline)
        dims, bitpix, meta = check_result(header, js, nvox)
        with open(json_tmp, "wb") as f:
            f.write(js)
        finished = True
        for p in (nii_tmp, json_tmp):
            _own(p)
        if os.path.exists(final_dir):                       # --overwrite: replace our two files only, never other content
            for ext in (".nii", ".json"):
                try:
                    os.unlink(os.path.join(final_dir, name + ext))
                except FileNotFoundError:
                    pass
        else:
            os.mkdir(final_dir)
            _own(final_dir)
        os.replace(nii_tmp, os.path.join(final_dir, name + ".nii"))
        os.replace(json_tmp, os.path.join(final_dir, name + ".json"))
    except BaseException as e:
        if started and not finished and not getattr(e, "device_idle", False):
            time.sleep(settle)                              # let the device finish discarding what we sent before anything else is sent
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return (os.path.join(final_dir, name + ".nii"), os.path.join(final_dir, name + ".json"),
            {"dim": dims, "bitpix": bitpix, "slices": len(paths), "json": meta})


# ------------------------------------------------------------------ batch
NAME_RE = re.compile(r"([A-Za-z0-9-]{1,32})_([A-Za-z0-9_-]{1,32})\Z")


def discover(root, manifest=None, only=None, site="NA", scanner="NA"):
    """The cases of a batch: dicts with folder, subject, sequence, site, scanner, series (None = not a case: 'skip' holds why)."""
    cases = []
    if manifest:
        try:
            with open(manifest, newline="") as f:
                rd = csv.DictReader(f)
                need = {"folder", "subject", "sequence"}
                if not rd.fieldnames or not need <= set(rd.fieldnames):
                    raise AnonError("%s: the header must contain folder,subject,sequence (and may contain site,scanner,series)" % manifest)
                rows = list(rd)
        except OSError as e:
            raise AnonError("cannot read %s: %s" % (manifest, e))
        for r in rows:
            ser = (r.get("series") or "").strip()
            if ser and not ser.isdigit():
                ser = "bad"
            cases.append({"folder": r["folder"].strip(), "subject": r["subject"].strip(), "sequence": r["sequence"].strip(),
                          "site": (r.get("site") or "").strip() or site, "scanner": (r.get("scanner") or "").strip() or scanner,
                          "series": int(ser) if ser.isdigit() else ser or None})
    else:
        try:
            names = sorted(n for n in os.listdir(root) if os.path.isdir(os.path.join(root, n)) and not n.startswith("."))
        except OSError as e:
            raise AnonError("cannot read %s: %s" % (root, e))
        for n in names:
            m = NAME_RE.match(n)
            if m:
                cases.append({"folder": n, "subject": m.group(1), "sequence": m.group(2), "site": site, "scanner": scanner, "series": None})
            else:
                cases.append({"folder": n, "skip": "name is not <subject>_<sequence> (list it in a --manifest)"})
    if only:
        cases = [c for c in cases if fnmatch.fnmatch(c["folder"], only)]
    return cases


def case_done(out_root, c):
    d = os.path.join(out_root, "%s_%s" % (c["subject"], c["sequence"]))
    n = "%s_%s" % (c["subject"], c["sequence"])
    return os.path.isfile(os.path.join(d, n + ".nii")) and os.path.isfile(os.path.join(d, n + ".json")), os.path.exists(d)


def run_batch(link, root, out_root, cases, overwrite=False, stop_on_error=False, log=lambda s: None, settle=DRAIN_SETTLE):
    """Do the cases one after the other on one connection. Returns a list of result dicts (folder, subject, sequence, status,
    slices, seconds, detail); status is ok, skipped, failed or not run."""
    results = []
    stop = None
    for k, c in enumerate(cases, 1):
        r = {"folder": c["folder"], "subject": c.get("subject", ""), "sequence": c.get("sequence", ""), "status": "", "slices": "",
             "seconds": "", "detail": ""}
        results.append(r)
        if stop:
            r["status"], r["detail"] = "not run", stop
            continue
        if "skip" in c:
            r["status"], r["detail"] = "skipped", c["skip"]
            continue
        if c["series"] == "bad":
            r["status"], r["detail"] = "failed", "the series column of the manifest is not a number"
            continue
        done, exists = case_done(out_root, c)
        if done and not overwrite:
            r["status"], r["detail"] = "skipped", "already done"
            continue
        log("[%d/%d] %s" % (k, len(cases), c["folder"]))
        t0 = time.monotonic()
        try:
            nii, js, info = anonymize(link, os.path.join(root, c["folder"]), c["subject"], c["sequence"], c["site"], c["scanner"], out_root,
                                      overwrite=overwrite or exists, which=c["series"], settle=settle)
            r["status"], r["slices"] = "ok", info["slices"]
        except AnonError as e:
            r["status"], r["detail"] = "failed", " ".join(str(e).split("\n")[0].split())
            if e.fatal:
                stop = "stopped: " + r["detail"]
            elif stop_on_error:
                stop = "stopped after the first failure (--stop-on-error)"
        r["seconds"] = "%.1f" % (time.monotonic() - t0)
    return results


def summary(results):
    w = max([len(r["folder"]) for r in results] + [6])
    lines = ["%-*s  %-8s %6s %7s  %s" % (w, "case", "status", "slices", "seconds", "detail")]
    for r in results:
        lines.append("%-*s  %-8s %6s %7s  %s" % (w, r["folder"], r["status"], r["slices"], r["seconds"], r["detail"]))
    n = collections.Counter(r["status"] for r in results)
    lines.append("%d ok, %d skipped, %d failed, %d not run" % (n["ok"], n["skipped"], n["failed"], n["not run"]))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("series_dir", help="a folder of DICOM files (one series), or with --batch a folder of such folders")
    ap.add_argument("--subject")
    ap.add_argument("--sequence")
    ap.add_argument("--site", default="NA")
    ap.add_argument("--scanner", default="NA")
    ap.add_argument("--series", type=int, default=None)
    ap.add_argument("--out", default=os.path.join(ROOT, "output", "anonymized"))
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--batch", action="store_true", help="series_dir holds many series folders named <subject>_<sequence>")
    ap.add_argument("--manifest", help="batch: CSV (folder,subject,sequence[,site,scanner,series]) naming the folders to do")
    ap.add_argument("--only", help="batch: only the folders whose names match this pattern, e.g. '*_T1c'")
    ap.add_argument("--stop-on-error", action="store_true", help="batch: stop at the first failed case")
    ap.add_argument("--report", help="batch: also write the table as a CSV file")
    ap.add_argument("--port")
    ap.add_argument("--exec", dest="exec_cmd", help="run the protocol layer as a local process instead of a serial device")
    ap.add_argument("--timing", action="store_true", help="print, about once a second, how much has been sent and received")
    ap.add_argument("--timeout", type=float, default=30.0, help="seconds to wait for the device between frames")
    a = ap.parse_args(argv)
    pf.TIMEOUT = a.timeout
    link = None
    try:
        if a.batch:
            if a.subject or a.sequence or a.series is not None or a.timing:
                raise AnonError("--batch takes the labels from the folder names or a manifest: do not give --subject, --sequence, --series or --timing")
        else:
            if not a.subject or not a.sequence:
                raise AnonError("give --subject and --sequence (or use --batch)")
            if a.manifest or a.only or a.stop_on_error or a.report:
                raise AnonError("--manifest, --only, --stop-on-error and --report belong to --batch")
        for what, v in (("subject", a.subject), ("sequence", a.sequence), ("site", a.site), ("scanner", a.scanner)):
            if v is not None and not valid_label(v):
                raise AnonError("--%s %r is not 1 to 32 characters of A-Z a-z 0-9 _ -" % (what, v))
        cases = discover(a.series_dir, a.manifest, a.only, a.site, a.scanner) if a.batch else None
        if a.batch and not cases:
            raise AnonError("no cases found in %s" % a.series_dir)
        if a.exec_cmd:
            link = pf.PipeLink(a.exec_cmd)
        else:
            port = a.port or pf.autodetect_port()
            print("board:", pf.describe_device(port), file=sys.stderr)
            link = pf.SerialLink(port)
        t0 = time.monotonic()
        if a.batch:
            results = run_batch(link, a.series_dir, a.out, cases, a.overwrite, a.stop_on_error,
                                log=lambda s: print(s, file=sys.stderr, flush=True))
            print(summary(results))
            if a.report:
                with open(a.report, "w", newline="") as f:
                    w = csv.DictWriter(f, ["folder", "subject", "sequence", "status", "slices", "seconds", "detail"], lineterminator="\n")
                    w.writeheader()
                    w.writerows(results)
            print("%.0f s in all" % (time.monotonic() - t0))
            return 2 if any(r["status"] in ("failed", "not run") for r in results) else 0
        timeline = [] if a.timing else None
        nii, js, info = anonymize(link, a.series_dir, a.subject, a.sequence, a.site, a.scanner, a.out, a.overwrite, a.series,
                                  log=lambda s: print(s, file=sys.stderr, end="\r" if s.startswith("sending") else "\n"),
                                  timeline=timeline)
    except AnonError as e:
        print("\nerror: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted; nothing was written for the case in progress", file=sys.stderr)
        return 130
    finally:
        if link is not None:
            link.close()
    if a.timing and timeline:
        print("\n  time(s)  sent(MB)  received(MB)  [upload finished?]", file=sys.stderr)
        for t, sent, recv, done in timeline:
            print("  %7.1f  %8.2f  %12.2f  %s" % (t, sent, recv, "yes" if done else ""), file=sys.stderr)
    d = info["dim"]
    print("\nwrote %s\n      %s\n%d x %d x %d voxels, %d bits, %.1f s" % (nii, js, d[0], d[1], d[2], info["bitpix"], time.monotonic() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
