#!/usr/bin/env python3
"""Tests for the host client pico/tools/pico_anonymize.py, against the real protocol code running under qemu-riscv32
(build32/anon_test) and against fake devices that misbehave.

What is checked:
  * a folder in, exactly one .nii and one .json out (nothing else in the output tree, no temporary folder left)
  * the .nii header and the JSON equal those of the verified harnesses; the voxels equal an independent re-pack; the
    repo's strict NIfTI reader (scripts/nifti_slice.py) reads the file back with the right size and voxels
  * files in a wrong order (shuffled names, descending positions) are sorted by the client
  * a folder with several series: refused without --series, each series selectable; non-DICOM files skipped
  * an existing result folder is refused unless --overwrite, and then only the two files are replaced
  * invalid labels are refused by the client: nothing is created and not one byte reaches the device
  * a bad file in the middle, a dead device, a device that does not speak the protocol, and devices that send an
    inconsistent reply (voxel count, header magic, non-ASCII JSON, no JSON): exit with an error and leave NO output
  * under sudo the results belong to the user who ran sudo
  * none of the planted identifying values appears anywhere in the output tree
"""
import importlib.util, io, json, os, shlex, struct, subprocess, sys, tempfile, shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


cl = load("pico_anonymize", os.path.join(ROOT, "pico", "tools", "pico_anonymize.py"))
ds, gen, pf = cl.ds, load("dicomgen", os.path.join(ROOT, "tests", "dicomgen.py")), cl.pf
nifti = load("nifti_slice", os.path.join(ROOT, "scripts", "nifti_slice.py"))
EMU = "qemu-riscv32 " + os.path.join(ROOT, "build32", "anon_test")
failures = 0


def check(name, ok, detail=""):
    global failures
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))
    failures += not ok


def tree(path):
    out = []
    for dp, dn, fn in os.walk(path):
        out += [os.path.relpath(os.path.join(dp, x), path) + "/" for x in dn] + [os.path.relpath(os.path.join(dp, x), path) for x in fn]
    return sorted(out)


def write_series(folder, files, names=None):
    os.makedirs(folder, exist_ok=True)
    for k, f in enumerate(files):
        open(os.path.join(folder, names[k] if names else "IM%04d.dcm" % k), "wb").write(f)


def job(folder, out, link_cmd=EMU, subject="P017", sequence="T1c", site="S02", scanner="C01", **kw):
    link = pf.PipeLink(link_cmd)
    try:
        return cl.anonymize(link, folder, subject, sequence, site, scanner, out, settle=0, **kw)
    finally:
        link.close()


def expect_error(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except cl.AnonError as e:
        return str(e)
    return None


def repack(f):
    img = ds.parse(f, "f")
    t = img.tags
    rows, cols, ba = t["Rows"], t["Columns"], t["BitsAllocated"]
    bs, sb = t.get("BitsStored", ba), ba // 8
    hb, signed = t.get("HighBit", bs - 1), bool(t.get("PixelRepresentation", 0))
    out = bytearray()
    for i in range(rows * cols):
        v = (int.from_bytes(f[img.pix_off + i * sb:img.pix_off + (i + 1) * sb], "little") >> (hb + 1 - bs)) & ((1 << bs) - 1)
        out += (v & ((1 << ba) - 1)).to_bytes(sb, "little")
    return bytes(out)


def vals(rows, cols, k):
    return [((i * 31 + k * 977) * 2654435761 >> 5) % 4000 for i in range(rows * cols)]


def main():
    if not os.path.exists(os.path.join(ROOT, "build32", "anon_test")):
        sys.exit("build the emulator protocol first: make build32/anon_test (or make test-anon-c)")
    pf.TIMEOUT = 5.0                                  # a fake device that goes quiet must not stall the test for 30 s
    work = tempfile.mkdtemp(prefix="anonymize-test-")
    try:
        run(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    sys.exit(1 if failures else 0)


def run(work):
    # ---------------------------------------------------------------- a synthetic series, end to end
    files = gen.stack(6, rows=20, cols=24, uid=b"1.2.3.4", vals=lambda k: vals(20, 24, k), slope=2.5, inter=-100)
    s1 = os.path.join(work, "ser1")
    write_series(s1, files)
    out = os.path.join(work, "out")
    nii, js, info = job(s1, out)
    check("a folder in: exactly P017_T1c/P017_T1c.nii and .json out, nothing else (no temporary folder left)",
          tree(out) == ["P017_T1c/", "P017_T1c/P017_T1c.json", "P017_T1c/P017_T1c.nii"], str(tree(out)))
    blob = open(nii, "rb").read()
    jtxt = open(js, "rb").read()
    blob_files = struct.pack("<I", len(files)) + b"".join(struct.pack("<I", len(f)) + f for f in files)
    r = subprocess.run(["qemu-riscv32", os.path.join(ROOT, "build32", "nifti_test")], input=blob_files, capture_output=True)
    want_hdr = bytes.fromhex([l for l in r.stdout.decode().split("\n") if l.startswith("HEADER=")][0][7:])
    lab = b"".join(struct.pack("B", len(x)) + x for x in (b"P017", b"T1c", b"S02", b"C01"))
    r = subprocess.run(["qemu-riscv32", os.path.join(ROOT, "build32", "sidecar_test")], input=lab + blob_files, capture_output=True)
    want_json = r.stdout.decode().split("JSON_BEGIN\n")[1].split("JSON_END")[0].encode()
    check("the header and the JSON equal those of the verified nifti_test and sidecar_test", blob[:352] == want_hdr and jtxt == want_json)
    check("the voxels equal an independent re-pack of the pixel data", blob[352:] == b"".join(repack(f) for f in files))
    h = nifti.read_header(io.BytesIO(blob[:348]))
    vol = nifti.read_channel(io.BytesIO(blob[348:]), h, 0)
    check("the repo's strict NIfTI reader reads it back: 24 x 20 x 6, 16 bits, the right voxels, slope 2.5, intercept -100",
          h["dims"][:3] == [24, 20, 6] and h["slope"] == 2.5 and h["inter"] == -100 and vol.tobytes()[:4] == blob[352:356], str(h["dims"]))
    check("the JSON parses and carries the labels", json.loads(jtxt)["labels"] == {"subject": "P017", "sequence": "T1c", "site": "S02", "scanner": "C01"})
    sentinels = [v for v in gen.SENT.values()]
    check("no planted identifying value anywhere in the output tree", not any(s in blob or s in jtxt for s in sentinels))

    # ---------------------------------------------------------------- order: shuffled names, descending positions
    import random
    rng = random.Random(5)
    names = ["IM%04d.dcm" % k for k in range(6)]
    rng.shuffle(names)
    s2 = os.path.join(work, "ser2")
    write_series(s2, files, names)
    nii2, js2, _ = job(s2, os.path.join(work, "out2"))
    check("files whose names are in a different order from the slices are sorted by position",
          open(nii2, "rb").read()[352:] == blob[352:] and open(js2, "rb").read() == jtxt)
    s3 = os.path.join(work, "ser3")
    write_series(s3, list(reversed(files)))          # IM0000 is the LAST slice: stored in descending order
    nii3, _, _ = job(s3, os.path.join(work, "out3"))
    check("a series stored in descending order is sent in ascending order", open(nii3, "rb").read()[352:] == blob[352:])

    # ---------------------------------------------------------------- several series in one folder; non-DICOM files
    a = gen.stack(4, rows=8, cols=9, uid=b"7.7.1", vals=lambda k: vals(8, 9, k))
    b = gen.stack(3, rows=8, cols=9, uid=b"7.7.2", vals=lambda k: vals(8, 9, k + 50), origin=(0, 0, 200))
    s4 = os.path.join(work, "ser4")
    write_series(s4, a, ["a%d.dcm" % k for k in range(4)])
    write_series(s4, b, ["b%d.dcm" % k for k in range(3)])
    open(os.path.join(s4, "LICENSE"), "w").write("not a dicom file\n")
    msg = expect_error(job, s4, os.path.join(work, "out4"))
    check("a folder with two series is refused without --series and the message lists them",
          msg is not None and "--series 1 : 4 slices" in msg and "--series 2 : 3 slices" in msg and not os.path.exists(os.path.join(work, "out4")), str(msg))
    n1, _, i1 = job(s4, os.path.join(work, "out4"), which=1)
    n2, _, i2 = job(s4, os.path.join(work, "out4b"), which=2, sequence="T2")
    check("--series 1 and --series 2 each send their own series (the LICENSE file is skipped)",
          i1["dim"] == (9, 8, 4) and i2["dim"] == (9, 8, 3) and open(n2, "rb").read()[352:] == b"".join(repack(f) for f in b))
    check("--series 3 is out of range", "out of range" in (expect_error(job, s4, os.path.join(work, "out4c"), which=3) or ""))

    # ---------------------------------------------------------------- existing results
    msg = expect_error(job, s1, out)
    check("an existing result folder is refused without --overwrite and left untouched", msg is not None and "already exists" in msg
          and open(nii, "rb").read() == blob)
    foreign = os.path.join(out, "P017_T1c", "notes.txt")
    open(foreign, "w").write("mine")
    job(s1, out, overwrite=True)
    check("--overwrite replaces the .nii and .json and leaves other files in the folder alone",
          open(foreign).read() == "mine" and open(nii, "rb").read() == blob and tree(out) == ["P017_T1c/", "P017_T1c/P017_T1c.json", "P017_T1c/P017_T1c.nii", "P017_T1c/notes.txt"])

    # ---------------------------------------------------------------- invalid labels
    bad = []
    marker = os.path.join(work, "device_was_spoken_to")
    spy = os.path.join(work, "spy.py")
    open(spy, "w").write("import sys\nif sys.stdin.buffer.read(1):\n    open(%r, 'w').write('x')\n" % marker)
    spy_cmd = "%s %s" % (shlex.quote(sys.executable), shlex.quote(spy))
    for kw in ({"subject": "bad label"}, {"sequence": "a/b"}, {"site": "s" * 33}, {"scanner": ""}, {"subject": "../x"}, {"sequence": "café"}, {"subject": "a.b"}, {"sequence": "x y"}, {"site": "s;t"}, {"scanner": "a\nb"}):
        o = os.path.join(work, "outbad")
        if os.path.exists(marker):
            os.unlink(marker)
        if expect_error(job, s1, o, link_cmd=spy_cmd, **kw) is None or os.path.exists(o) or os.path.exists(marker):
            bad.append(kw)
    check("invalid labels are refused by the client: nothing is created and not one byte reaches the device", not bad, str(bad))

    # ---------------------------------------------------------------- a series the device refuses part-way (a missing slice)
    gap = files[:3] + files[4:]
    s5 = os.path.join(work, "ser5")
    write_series(s5, gap, ["IM%04d.dcm" % k for k in range(5)])
    o5 = os.path.join(work, "out5")
    msg = expect_error(job, s5, o5)
    check("a series the device refuses (a missing slice): the error names a file and the reason, and no output or temporary folder is left",
          msg is not None and "GEOM:UNEVEN" in msg and "IM000" in msg and "evenly spaced" in msg and (not os.path.exists(o5) or tree(o5) == []),
          str(msg) + str(tree(o5) if os.path.exists(o5) else ""))

    # ---------------------------------------------------------------- devices that fail or misbehave
    def fake(reply_fn, drain=True):
        """A --exec command that plays back a fixed reply and then (unless drain is False) reads its input to the end."""
        script = os.path.join(work, "fake_%d.py" % len(os.listdir(work)))
        open(script, "w").write("import sys\n" + reply_fn + "\nsys.stdout.buffer.flush()\n" + ("sys.stdin.buffer.read()\n" if drain else ""))
        return "%s %s" % (shlex.quote(sys.executable), shlex.quote(script))

    def frames(*fr):
        return b"NII1" + b"".join(struct.pack("<cI", t, len(p)) + p for t, p in fr)

    hdr, goodjs, nv = blob[:352], jtxt, len(blob) - 352
    cases = {
        "a device that is not there (the process ends at once)": "true",
        "a device that does not speak the protocol": fake("sys.stdout.buffer.write(b'hello there, I am a filter')"),
        "too few voxel bytes": fake("sys.stdout.buffer.write(%r)" % frames((b"A", b""), (b"V", b"\0" * (nv - 2)), (b"H", hdr), (b"J", goodjs), (b"D", b""))),
        "a header that is not NIfTI": fake("sys.stdout.buffer.write(%r)" % frames((b"A", b""), (b"V", b"\0" * nv), (b"H", b"\0" * 352), (b"J", goodjs), (b"D", b""))),
        "a header of the wrong size": fake("sys.stdout.buffer.write(%r)" % frames((b"A", b""), (b"V", b"\0" * nv), (b"H", hdr[:300]), (b"J", goodjs), (b"D", b""))),
        # headers that break exactly ONE property each (so each check has to catch its own)
        "a header whose first int is 349, not 348": fake("sys.stdout.buffer.write(%r)" % frames((b"A", b""), (b"V", b"\0" * nv), (b"H", struct.pack("<i", 349) + hdr[4:]), (b"J", goodjs), (b"D", b""))),
        "a header with magic n+2": fake("sys.stdout.buffer.write(%r)" % frames((b"A", b""), (b"V", b"\0" * nv), (b"H", hdr[:344] + b"n+2\0" + hdr[348:]), (b"J", goodjs), (b"D", b""))),
        "a header with a voxel offset of 348": fake("sys.stdout.buffer.write(%r)" % frames((b"A", b""), (b"V", b"\0" * nv), (b"H", hdr[:108] + struct.pack("<f", 348.0) + hdr[112:]), (b"J", goodjs), (b"D", b""))),
        "a header with 2 dimensions": fake("sys.stdout.buffer.write(%r)" % frames((b"A", b""), (b"V", b"\0" * nv), (b"H", hdr[:40] + struct.pack("<h", 2) + hdr[42:]), (b"J", goodjs), (b"D", b""))),
        "a header with a zero dimension": fake("sys.stdout.buffer.write(%r)" % frames((b"A", b""), (b"V", b""), (b"H", hdr[:42] + struct.pack("<h", 0) + hdr[44:]), (b"J", goodjs), (b"D", b""))),
        "a header with 32 bits per voxel and the voxel count to match": fake("sys.stdout.buffer.write(%r)" % frames((b"A", b""), (b"V", b"\0" * (nv * 2)), (b"H", hdr[:72] + struct.pack("<h", 32) + hdr[74:]), (b"J", goodjs), (b"D", b""))),
        "a header one byte too long": fake("sys.stdout.buffer.write(%r)" % frames((b"A", b""), (b"V", b"\0" * nv), (b"H", hdr + b"\0"), (b"J", goodjs), (b"D", b""))),
        "JSON with a non-ASCII byte": fake("sys.stdout.buffer.write(%r)" % frames((b"A", b""), (b"V", b"\0" * nv), (b"H", hdr), (b"J", goodjs.replace(b"P017", b"P\xc3\xa9017")), (b"D", b""))),
        "JSON that does not parse": fake("sys.stdout.buffer.write(%r)" % frames((b"A", b""), (b"V", b"\0" * nv), (b"H", hdr), (b"J", b"{not json"), (b"D", b""))),
        "done without a JSON": fake("sys.stdout.buffer.write(%r)" % frames((b"A", b""), (b"V", b"\0" * nv), (b"H", hdr), (b"D", b""))),
        "an unknown frame": fake("sys.stdout.buffer.write(%r)" % frames((b"A", b""), (b"Z", b"abc"))),
        "an error frame": fake("sys.stdout.buffer.write(%r)" % frames((b"A", b""), (b"E", struct.pack("<I", 2) + b"GEOM:UNEVEN"))),
        "the device stops in the middle (frame cut off)": fake("sys.stdout.buffer.write(%r)" % (frames((b"A", b""), (b"V", b"\0" * 100))[:-50]), drain=False),
    }
    bad = []
    for label, cmd in cases.items():
        o = os.path.join(work, "outf")
        shutil.rmtree(o, ignore_errors=True)
        try:
            msg = expect_error(job, s1, o, link_cmd=cmd)
        except Exception as e:                                    # noqa: BLE001 - any other exception type is a client bug
            msg = None
            label += " (raised %r)" % e
        leftovers = tree(o) if os.path.exists(o) else []
        if msg is None or leftovers:
            bad.append((label, msg, leftovers))
    check(f"{len(cases)} faulty devices: each ends in a clear error and leaves no output", not bad, str(bad[:3]))
    msg = expect_error(job, s1, os.path.join(work, "outf2"), link_cmd=cases["an error frame"])
    check("an error frame from the device is explained with the file name", msg is not None and "IM0002.dcm" in msg and "GEOM:UNEVEN" in msg and "evenly spaced" in msg, str(msg))

    # ---------------------------------------------------------------- ownership under sudo
    calls = []
    real_euid, real_chown, env = os.geteuid, os.chown, dict(os.environ)
    try:
        os.geteuid = lambda: 0
        os.chown = lambda p, u, g: calls.append((os.stat(p).st_ino, u, g))      # by inode: the files are chowned before they are moved
        os.environ["SUDO_UID"], os.environ["SUDO_GID"] = "1234", "5678"
        job(s1, os.path.join(work, "own", "deeper", "out"))
    finally:
        os.geteuid, os.chown = real_euid, real_chown
        os.environ.clear()
        os.environ.update(env)
    base = os.path.join(work, "own", "deeper", "out")
    want = {os.stat(x).st_ino for x in (os.path.join(work, "own"), os.path.join(work, "own", "deeper"), base, os.path.join(base, "P017_T1c"),
                                         os.path.join(base, "P017_T1c", "P017_T1c.nii"), os.path.join(base, "P017_T1c", "P017_T1c.json"))}
    check("run as root through sudo: the folders it created and both result files are handed to $SUDO_UID:$SUDO_GID, and nothing else",
          {c[0] for c in calls} == want and all(c[1:] == (1234, 5678) for c in calls), str(len(calls)))

    # ---------------------------------------------------------------- the command line
    def cli(*args, cwd=None):
        r = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "pico", "tools", "pico_anonymize.py")] + list(args),
                           capture_output=True, text=True, cwd=cwd, timeout=300)
        return r.returncode, r.stdout, r.stderr

    oc = os.path.join(work, "outc")
    rc, so, se = cli(s1, "--subject", "P1", "--sequence", "T1", "--out", oc, "--exec", EMU)
    check("command line: exit 0, names the two files, 24 x 20 x 6 voxels", rc == 0 and "P1_T1.nii" in so and "24 x 20 x 6 voxels, 16 bits" in so, so + se)
    rc, so, se = cli(s1, "--subject", "P1", "--sequence", "T1", "--out", oc, "--exec", EMU)
    check("command line: a second run is refused with exit 1 and a clear message", rc == 1 and "already exists" in se and "--overwrite" in se, se)
    rc, so, se = cli(s1, "--subject", "bad name", "--sequence", "T1", "--out", oc, "--exec", EMU)
    check("command line: a bad label is exit 1", rc == 1 and "not 1 to 32 characters" in se, se)
    rc, so, se = cli(s4, "--subject", "P2", "--sequence", "T1", "--out", oc, "--exec", EMU)
    check("command line: several series without --series is exit 1 and lists them", rc == 1 and "--series 1" in se, se)
    rc, so, se = cli(os.path.join(work, "nothing_here"), "--subject", "P2", "--sequence", "T1", "--out", oc, "--exec", EMU)
    check("command line: a missing folder is exit 1, not a traceback", rc == 1 and "Traceback" not in se and "error:" in se, se)

    # ---------------------------------------------------------------- one real series, end to end
    root = os.path.join(ROOT, "data", "dicom")
    if os.path.isdir(root):
        d = sorted(x for x in os.listdir(root) if os.path.isdir(os.path.join(root, x)))[0]
        nii_r, js_r, inf = job(os.path.join(root, d), os.path.join(work, "outr"), subject="R1", sequence="real")
        imgs, _ = ds.load_series(os.path.join(root, d))
        ordered, _, _, _ = ds.order_slices(imgs)
        raw = b"".join(open(i.path, "rb").read()[i.pix_off:i.pix_off + i.tags["Rows"] * i.tags["Columns"] * 2] for i in ordered)
        body = open(nii_r, "rb").read()
        secrets = set()
        for i in ordered[:3]:
            f = open(i.path, "rb").read()
            for (g, e) in ((0x10, 0x10), (0x10, 0x20), (0x08, 0x20), (0x10, 0x1010)):
                q = f.find(struct.pack("<HH", g, e), 132)
                if q > 0:
                    v = f[q + 8:q + 8 + struct.unpack_from("<I", f, q + 4)[0]].strip(b"\0 ")
                    if len(v) >= 3:
                        secrets.add(v)
        text = body[:352] + open(js_r, "rb").read()
        check(f"a real series ({d}, {len(ordered)} slices): the voxels are the raw pixel bytes, and none of its real identifying values is in the header or JSON",
              body[352:] == raw and len(secrets) >= 3 and not any(s in text for s in secrets), f"secrets={len(secrets)}")
    else:
        print("SKIP  real series: data/dicom/ not present")


main()
