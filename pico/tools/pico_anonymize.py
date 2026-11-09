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

Wire format: pico/src/anon.h.
"""
import argparse, importlib.util, json, os, re, shutil, struct, sys, tempfile, threading, time

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
            raise AnonError("the device did not answer like the anonymiser (is the right firmware flashed?)")
        t, p = read_frame(link)
    except (TimeoutError, EOFError, OSError) as e:
        raise AnonError("lost the device: %s" % (e or type(e).__name__))
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
        raise AnonError("lost the device: %s" % (e or type(e).__name__))
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


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("series_dir")
    ap.add_argument("--subject", required=True)
    ap.add_argument("--sequence", required=True)
    ap.add_argument("--site", default="NA")
    ap.add_argument("--scanner", default="NA")
    ap.add_argument("--series", type=int, default=None)
    ap.add_argument("--out", default=os.path.join(ROOT, "output", "anonymized"))
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--port")
    ap.add_argument("--exec", dest="exec_cmd", help="run the protocol layer as a local process instead of a serial device")
    ap.add_argument("--timing", action="store_true", help="print, about once a second, how much has been sent and received")
    ap.add_argument("--timeout", type=float, default=30.0, help="seconds to wait for the device between frames")
    a = ap.parse_args(argv)
    pf.TIMEOUT = a.timeout
    link = None
    try:
        for what, v in (("subject", a.subject), ("sequence", a.sequence), ("site", a.site), ("scanner", a.scanner)):
            if not valid_label(v):
                raise AnonError("--%s %r is not 1 to 32 characters of A-Z a-z 0-9 _ -" % (what, v))
        if a.exec_cmd:
            link = pf.PipeLink(a.exec_cmd)
        else:
            port = a.port or pf.autodetect_port()
            print("board:", pf.describe_device(port), file=sys.stderr)
            link = pf.SerialLink(port)
        t0 = time.monotonic()
        timeline = [] if a.timing else None
        nii, js, info = anonymize(link, a.series_dir, a.subject, a.sequence, a.site, a.scanner, a.out, a.overwrite, a.series,
                                  log=lambda s: print(s, file=sys.stderr, end="\r" if s.startswith("sending") else "\n"),
                                  timeline=timeline)
    except AnonError as e:
        print("\nerror: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted; nothing was written", file=sys.stderr)
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
