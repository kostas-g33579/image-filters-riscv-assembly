#!/usr/bin/env python3
"""Tests for the anonymiser protocol (pico/src/anon.c), run under qemu-riscv32 in two builds:

  anon_test       the real sizes (128 KB head buffer, 4 KB chunks)
  anon_test_tiny  a 1023-byte head buffer (an ODD size, so a cut can fall inside a 16-bit sample), 64-byte chunks,
                  and reads of 1..13 bytes at a time

What is checked, against oracles that do not share code with the device:
  * the voxel bytes: an independent Python re-pack of the pixel data (shift, mask, sign-extend, little endian), or,
    for the real series (16 bits stored in 16, so the output is the raw bytes), the raw bytes themselves
  * the 352-byte header and the JSON: equal to what the already verified nifti_test and sidecar_test harnesses
    give for the same files and labels
  * the shape of the reply: magic, 'A', 'V' frames of at most one chunk, 'H' of 352 bytes, 'J', 'D', nothing else
  * every error: its stage, its name and the index of the file; the frames before it are exactly the voxels of the
    slices before it; and the NEXT request, sent right behind the broken one, is answered correctly
  * a request that is cut off, noise before the magic, request-level errors, files longer than the head buffer,
    padding after the pixels, headers that do not end in the head buffer
  * privacy: none of the planted identifying values, and none of the real files' identifying values, anywhere in a reply
"""
import importlib.util, json, os, re, struct, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QEMU = os.environ.get("QEMU", "qemu-riscv32").split()
B32 = os.path.join(ROOT, "build32")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ds = load("dicom_slice", os.path.join(ROOT, "scripts", "dicom_slice.py"))
gen = load("dicomgen", os.path.join(ROOT, "tests", "dicomgen.py"))
failures = 0
LAB = (b"P017", b"T1c", b"S02", b"C01")
NO_FILE = 0xFFFFFFFF
VARIANTS = {"real sizes": ("anon_test", 131072, 4096), "tiny": ("anon_test_tiny", 1023, 64)}
FIRST = {"real sizes": 4096, "tiny": 200}          # ANON_HEAD_FIRST of each build: the header is first parsed after this many bytes


def check(name, ok, detail=""):
    global failures
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))
    failures += not ok


# ------------------------------------------------------------------ talking to the device
def request(files, labels=LAB, flags=0, nfiles=None):
    r = b"DCM1" + struct.pack("<II", flags, len(files) if nfiles is None else nfiles)
    r += b"".join(struct.pack("B", len(x)) + x for x in labels)
    return r + b"".join(struct.pack("<I", len(f)) + f for f in files)


def run(binary, data, timeout=600):
    r = subprocess.run(QEMU + [os.path.join(B32, binary)], input=data, capture_output=True, timeout=timeout)
    if r.returncode != 0 or r.stderr:
        raise RuntimeError("anon harness failed: rc=%s %s" % (r.returncode, r.stderr[:200]))
    return r.stdout


def parse_replies(out):
    """Strictly: a sequence of replies, each 'NII1' + frames up to and including a 'D' or an 'E' frame.
    Returns a list of dicts {frames: [(type, payload)]}; raises AssertionError on any stray byte."""
    res, p = [], 0
    while p < len(out):
        assert out[p:p + 4] == b"NII1", "reply does not start with NII1 at %d: %r" % (p, out[p:p + 8])
        p += 4
        frames = []
        while True:
            assert p + 5 <= len(out), "frame header cut off"
            t, n = out[p:p + 1], struct.unpack_from("<I", out, p + 1)[0]
            assert p + 5 + n <= len(out), "frame payload cut off"
            frames.append((t, out[p + 5:p + 5 + n]))
            p += 5 + n
            if t in (b"D", b"E"):
                break
        res.append(frames)
    return res


def describe(frames):
    """(status, voxels, header, json, error) of one reply, checking its shape."""
    types = [t for t, _ in frames]
    err = None
    if types[-1] == b"E":
        idx = struct.unpack_from("<I", frames[-1][1])[0]
        err = (idx, frames[-1][1][4:].decode("latin-1"))
    vox = b"".join(p for t, p in frames if t == b"V")
    hdr = next((p for t, p in frames if t == b"H"), None)
    js = next((p for t, p in frames if t == b"J"), None)
    return types, vox, hdr, js, err


def good_shape(frames, chunk):
    types = [t for t, _ in frames]
    n = len(types)
    ok = types[0] == b"A" and types[-1] == b"D" and types[-2] == b"J" and types[-3] == b"H"
    ok = ok and all(t == b"V" for t in types[1:-3]) and n >= 5
    ok = ok and all(0 < len(p) <= chunk for t, p in frames if t == b"V")
    ok = ok and len(frames[-3][1]) == 352 and len(frames[-1][1]) == 0 and len(frames[0][1]) == 0
    return ok


# ------------------------------------------------------------------ the oracles
def repack(f):
    """Independent re-pack of one file's pixel samples into the NIfTI type, from the file's own tags."""
    img = ds.parse(f, "f")
    t = img.tags
    rows, cols, ba = t["Rows"], t["Columns"], t["BitsAllocated"]
    bs = t.get("BitsStored", ba)
    hb = t.get("HighBit", bs - 1)
    signed = bool(t.get("PixelRepresentation", 0))
    sb = ba // 8
    raw = f[img.pix_off:img.pix_off + rows * cols * sb]
    out = bytearray()
    for i in range(rows * cols):
        v = int.from_bytes(raw[i * sb:(i + 1) * sb], "little")
        v = (v >> (hb + 1 - bs)) & ((1 << bs) - 1)
        if signed and v >> (bs - 1):
            v -= 1 << bs
        out += (v & ((1 << ba) - 1)).to_bytes(sb, "little")
    return bytes(out)


def harness_header_and_json(files, labels=LAB):
    """Header (hex) from nifti_test and the JSON text from sidecar_test, for the same files: both verified elsewhere."""
    blob = struct.pack("<I", len(files)) + b"".join(struct.pack("<I", len(f)) + f for f in files)
    r = subprocess.run(QEMU + [os.path.join(B32, "nifti_test")], input=blob, capture_output=True, timeout=600)
    m = re.search(r"^HEADER=([0-9a-f]+)$", r.stdout.decode("latin-1"), re.M)
    hdr = bytes.fromhex(m.group(1)) if m else None
    lb = b"".join(struct.pack("B", len(x)) + x for x in labels)
    r = subprocess.run(QEMU + [os.path.join(B32, "sidecar_test")], input=lb + blob, capture_output=True, timeout=600)
    m = re.search(r"JSON_BEGIN\n(.*)JSON_END\n", r.stdout.decode("latin-1"), re.S)
    return hdr, (m.group(1).encode("latin-1") if m else None)


# ------------------------------------------------------------------ series
def vals_fn(rows, cols, stored, seed):
    return lambda k: [((i * 31 + k * 977 + seed * 7919) * 2654435761 >> 5) % (1 << stored) for i in range(rows * cols)]


def series(n, rows, cols, alloc, stored, high, signed, seed=0, orient="axial", **kw):
    return gen.stack(n, orient=orient, rows=rows, cols=cols, alloc=alloc, stored=stored, high=high, signed=signed,
                     vals=vals_fn(rows, cols, stored, seed), **kw)


FORMATS = [(16, 16, 15, False), (16, 12, 11, True), (16, 16, 15, True), (8, 8, 7, False), (8, 8, 7, True), (16, 12, 15, False)]
SIZES = [(2, 3), (5, 7), (16, 16), (33, 35)]


def main():
    for b in ("anon_test", "anon_test_tiny", "nifti_test", "sidecar_test"):
        if not os.path.exists(os.path.join(B32, b)):
            sys.exit(f"build the harnesses first: make test-anon-c (missing build32/{b})")

    good = []                                           # (label, files)
    for (rows, cols) in SIZES:
        for k, (alloc, stored, high, signed) in enumerate(FORMATS):
            good.append((f"{rows}x{cols}, {alloc} bits ({stored} stored, high bit {high}), {'signed' if signed else 'unsigned'}",
                         series(3 + k % 3, rows, cols, alloc, stored, high, signed, seed=k + rows,
                                orient=("axial", "sagittal")[k % 2], slope=2.5 if k == 1 else None, inter=-100 if k == 1 else None)))
    two = series(2, 20, 24, 16, 16, 15, False)
    good.append(("two slices (the smallest series with a slice step)", two))
    good.append(("a single slice", series(1, 8, 9, 16, 16, 15, False, seed=4)))
    good.append(("padding after the pixel data", [gen.make(rows=9, cols=11, pos=(0, 0, k * 1.5), iop=(1, 0, 0, 0, 1, 0), spacing=(1, 1),
                                                           inst=k, uid=b"9.9", thick=1.5, extra_after_px=b"\x5a" * (3 + 7 * k)) for k in range(4)]))
    # pixel data element longer than rows*cols*2: the extra bytes are dropped
    ex = []
    for k in range(4):
        px = gen.pack_pixels([(i * 3 + k) % 4000 for i in range(70)], 16, 16, 15, False) + b"\xee" * 20
        ex.append(gen.make(rows=7, cols=10, pix_override=px, pos=(0, 0, k * 2.0), iop=(1, 0, 0, 0, 1, 0), spacing=(1, 1), inst=k,
                           uid=b"9.8", thick=2.0))
    good.append(("pixel data longer than the image (extra bytes dropped)", ex))
    good.append(("140 KB of padding after the pixel data (longer than the head buffer)", [gen.make(
        rows=6, cols=6, vals=[(i * 5 + k) % 900 for i in range(36)], pos=(0, 0, k * 1.0), iop=(1, 0, 0, 0, 1, 0), spacing=(1, 1), inst=k,
        uid=b"9.3", thick=1.0, extra_after_px=b"\x5a" * 140000) for k in range(3)]))
    big = []                                            # headers of different lengths: the pixel data starts at different offsets
    for k in range(5):
        big.append(gen.make(rows=30, cols=30, vals=[(i * 13 + k) % 3000 for i in range(900)], pos=(0, 0, k * 1.0),
                            iop=(1, 0, 0, 0, 1, 0), spacing=(1, 1), inst=k, uid=b"9.7", thick=1.0,
                            extra_before=gen.enc(0x0009, 0x1001, b"OB", b"\x77" * (2 * (k * 5 + 1)), True)))
    good.append(("headers of different lengths (the pixel data starts at different offsets)", big))
    real = []
    root = os.path.join(ROOT, "data", "dicom")
    if os.path.isdir(root):
        for d in sorted(os.listdir(root)):
            p = os.path.join(root, d)
            if os.path.isdir(p):
                imgs, _ = ds.load_series(p)
                ordered, _, _, _ = ds.order_slices(imgs)
                real.append((d, [open(i.path, "rb").read() for i in ordered]))

    for vname, (binary, hdr_max, chunk) in VARIANTS.items():
        print(f"\n--- build: {vname} (head buffer {hdr_max} bytes, chunk {chunk} bytes)")
        data = b"".join(request(f) for _, f in good)
        replies = parse_replies(run(binary, data))
        check(f"{len(good)} good series: one reply each", len(replies) == len(good), str(len(replies)))
        bad_shape, bad_vox, bad_hj, leaks = [], [], [], []
        sentinels = [v for v in gen.SENT.values()]
        for (label, files), fr in zip(good, replies):
            if not good_shape(fr, chunk):
                bad_shape.append((label, [t for t, _ in fr][:6]))
                continue
            _, vox, hdr, js, err = describe(fr)
            if vox != b"".join(repack(f) for f in files):
                bad_vox.append(label)
            h2, j2 = harness_header_and_json(files)
            if hdr != h2 or js != j2:
                bad_hj.append((label, hdr == h2, js == j2))
            if any(s in b"".join(p for _, p in fr) for s in sentinels):
                leaks.append(label)
        check("every reply has the right shape: A, V frames of at most one chunk, H (352 bytes), J, D", not bad_shape, str(bad_shape[:2]))
        check("the voxel bytes equal an independent re-pack of the pixel data, slice after slice", not bad_vox, str(bad_vox[:3]))
        check("the header and the JSON equal those of the verified nifti_test and sidecar_test", not bad_hj, str(bad_hj[:3]))
        check("none of the planted identifying values (name, ID, birth date, institution, ...) is anywhere in a reply", not leaks, str(leaks[:3]))
        if vname == "real sizes" and real:
            bad_real, leaked = [], []
            for d, files in real:
                fr = parse_replies(run(binary, request(files)))[0]
                _, vox, hdr, js, err = describe(fr)
                if not good_shape(fr, chunk) or vox != b"".join(f[ds.parse(f, "f").pix_off:][:len(vox) // len(files)] for f in files):
                    bad_real.append(d)
                    continue
                secrets = set()
                for f in files[:3]:
                    for (g, e) in ((0x10, 0x10), (0x10, 0x20), (0x08, 0x20), (0x10, 0x1010)):
                        q = f.find(struct.pack("<HH", g, e), 132)
                        if q > 0:
                            v = f[q + 8:q + 8 + struct.unpack_from("<I", f, q + 4)[0]].strip(b"\0 ")
                            if len(v) >= 3:
                                secrets.add(v)
                body = b"".join(p for _, p in fr)
                if len(secrets) < 3 or any(s in body for s in secrets):
                    leaked.append(d)
            check(f"the {len(real)} real series: right shape, the voxels are the raw pixel bytes, none of their real identifying values in the reply",
                  not bad_real and not leaked, f"{bad_real[:2]} {leaked[:2]}")
        elif vname == "tiny" and real:
            d, files = real[0]
            fr = parse_replies(run(binary, request(files[:6])))[0]
            err = describe(fr)[4]
            check("a real header (about 2.9 KB) does not fit the tiny 1023-byte head buffer: refused at file 0 as PARSE:HEADER_TOO_LARGE",
                  err == (0, "PARSE:HEADER_TOO_LARGE") and [t for t, _ in fr] == [b"A", b"E"], str(err))

    # ---------------------------------------------------------------- errors, and the request right behind them
    follow = series(3, 6, 6, 16, 16, 15, False, seed=99)
    follow_expect = b"".join(repack(f) for f in follow)

    base = lambda n, **kw: series(n, 6, 6, 16, 16, 15, False, seed=5, **kw)
    cases = []                                          # (label, files, (stage:name), index, number of slices fully sent before)
    f4 = base(4)
    cases.append(("not a DICOM file", f4[:2] + [b"just text" * 20] + f4[3:], "PARSE:PREAMBLE", 2, 2))
    cases.append(("a file with no pixel data", f4[:1] + [f4[1][:f4[1].index(struct.pack("<HH", 0x7FE0, 0x10))]] + f4[2:], "PARSE:NO_PIXEL", 1, 1))
    cases.append(("pixel data runs past the end of the file", f4[:3] + [f4[3][:-5]], "PARSE:PIXEL_TRUNC", 3, 3))
    cases.append(("colour image", f4[:1] + [gen.make(rows=6, cols=6, spp=3, pos=(0, 0, 1.0), iop=(1, 0, 0, 0, 1, 0), spacing=(1, 1), inst=1, uid=b"9.5",
                                                      thick=1.0)] + f4[2:], "CHECK:COLOUR", 1, 1))
    cases.append(("two frames in one file", f4[:2] + [gen.make(rows=6, cols=6, nframes=2, pos=(0, 0, 2.0), iop=(1, 0, 0, 0, 1, 0), spacing=(1, 1), inst=2,
                                                                uid=b"9.5", thick=1.0)], "CHECK:MULTIFRAME", 2, 2))
    cases.append(("slices out of order", [f4[0], f4[2], f4[1], f4[3]], None, 2, 2))
    cases.append(("MONOCHROME1", base(4, photometric=lambda k: b"MONOCHROME1" if k == 1 else b"MONOCHROME2"), "NII:PHOTOMETRIC", 1, 1))
    cases.append(("a slice of a different size", f4[:2] + [series(1, 7, 6, 16, 16, 15, False, seed=5)[0]] + f4[3:], None, 2, 2))
    cases.append(("a header that does not end in the head buffer", f4[:1] + [gen.make(rows=6, cols=6, pos=(0, 0, 1.0), iop=(1, 0, 0, 0, 1, 0),
                                                                                       spacing=(1, 1), inst=1, uid=b"9.6", thick=1.0,
                                                                                       extra_before=gen.enc(0x0009, 0x1001, b"OB", b"\x01" * 200000, True))]
                  + f4[2:], "PARSE:HEADER_TOO_LARGE", 1, 1))
    cases.append(("an empty file", f4[:3] + [b""], "PARSE:PREAMBLE", 3, 3))
    cases.append(("the very first file is bad", [b"x" * 500] + f4[1:], "PARSE:PREAMBLE", 0, 0))

    for vname, (binary, hdr_max, chunk) in VARIANTS.items():
        print(f"\n--- errors, build: {vname}")
        bad = []
        for label, files, want, idx, nsent in cases:
            # the failing request, then (no gap) a good one: the second must be answered correctly
            if want is None and idx is not None:
                pass
            rep = parse_replies(run(binary, request(files) + request(follow), timeout=300))
            if len(rep) != 2:
                bad.append((label, "replies", len(rep)))
                continue
            types, vox, hdr, js, err = describe(rep[0])
            if want is None and idx is None:                       # single slice: whatever the device decides, it must be well-formed
                ok1 = good_shape(rep[0], chunk) or err is not None
            else:
                ok1 = err is not None and err[0] == idx and (want is None or err[1] == want)
                ok1 = ok1 and vox == b"".join(repack(f) for f in files[:nsent])
                ok1 = ok1 and b"H" not in types and b"J" not in types
            t2, v2, h2, j2, e2 = describe(rep[1])
            ok2 = good_shape(rep[1], chunk) and v2 == follow_expect and e2 is None
            if not (ok1 and ok2):
                bad.append((label, err, ok1, ok2))
        check(f"{len(cases)} error cases: the right file, a fixed stage and name, only the earlier slices' voxels, and the next request answered", not bad, str(bad[:3]))
        names = {}
        for label, files, want, idx, nsent in cases:
            if want is None and idx is not None:
                rep = parse_replies(run(binary, request(files)))[0]
                names[label] = describe(rep)[4]
        print("  (names reported where the test does not fix them:", {k: v for k, v in names.items()}, ")")
        check("an out-of-order series is refused by the geometry check and a slice of another size by the NIfTI check",
              names.get("slices out of order", (0, ""))[1].startswith("GEOM:") and names.get("a slice of a different size", (0, ""))[1].startswith(("NII:", "GEOM:")),
              str(names))

        # ---- an error found only when the series is complete (nii_header: a dimension above 32767), named as the verified harness names it.
        # Each slice is 132 KB, longer than the real build's head buffer, so this also streams pixels that never sat in memory.
        tall = series(2, 33000, 2, 16, 16, 15, False, seed=3)
        blob1 = struct.pack("<I", 2) + b"".join(struct.pack("<I", len(f)) + f for f in tall)
        m = re.search(r"^RESULT=ERR:(\w+)$", subprocess.run(QEMU + [os.path.join(B32, "nifti_test")], input=blob1, capture_output=True).stdout.decode(), re.M)
        rep = parse_replies(run(binary, request(tall) + request(follow)))
        e = describe(rep[0])[4]
        check("a series whose dimension is above 32767: all its voxels are sent, then the end-of-series error the verified harness gives (%s)" % (m.group(1) if m else "none"),
              m is not None and e is not None and e[0] == NO_FILE and e[1].split(":")[1] == m.group(1) and describe(rep[0])[1] == b"".join(repack(f) for f in tall)
              and good_shape(rep[1], chunk), str(e))
        # ---- a file that claims to be longer than the sanity bound: refused, and the request behind it is still answered
        big_len = request(follow[:1], nfiles=2)[:-len(follow[0]) - 4] + struct.pack("<I", 70 << 20)
        rep = parse_replies(run(binary, big_len + b"\x00" * 50 + request(follow), timeout=300))
        e = describe(rep[0])[4] if rep else None
        check("a file of 70 MiB is refused as FILE:TOO_LARGE at once (no head buffer is filled)", bool(rep) and e == (0, "FILE:TOO_LARGE"), str(e))

        # ---- the header size limit: a header ending exactly at the head buffer is accepted, two bytes more is refused
        limit = hdr_max if hdr_max % 2 == 0 else hdr_max - 1
        def with_header_end(target):
            mk = lambda x: gen.make(rows=6, cols=6, pos=(0, 0, 0.0), iop=(1, 0, 0, 0, 1, 0), spacing=(1, 1), inst=0, uid=b"9.4", thick=1.0,
                                    extra_before=gen.enc(0x0009, 0x1001, b"OB", b"\x01" * x, True))
            return mk(target - ds.parse(mk(0), "f").pix_off)
        def with_header_end_probe():
            return gen.make(rows=6, cols=6, pos=(0, 0, 0.0), iop=(1, 0, 0, 0, 1, 0), spacing=(1, 1), inst=0, uid=b"9.4", thick=1.0,
                            extra_before=gen.enc(0x0009, 0x1001, b"OB", b"", True))
        at_limit, past_limit = with_header_end(limit), with_header_end(limit + 2)
        r1 = describe(parse_replies(run(binary, request([at_limit])))[0])
        r2 = describe(parse_replies(run(binary, request([past_limit])))[0])
        check(f"a header that ends exactly at byte {limit} is accepted, one that ends at {limit + 2} is refused as HEADER_TOO_LARGE",
              ds.parse(at_limit, "f").pix_off == limit and r1[4] is None and r1[1] == repack(at_limit) and r2[4] == (0, "PARSE:HEADER_TOO_LARGE"), str((r1[4], r2[4])))
        # ---- the header is parsed after a small first piece and the piece doubles until the header has ended: a header that ends
        # just before, at, and just after each step must give the same answer (the good series, or the same error), in either build
        first = FIRST[vname]
        m_min = ds.parse(with_header_end_probe(), "f").pix_off                 # the shortest header the generator can write
        steps = sorted({first // 2, first - 2, first, first + 2, 2 * first - 2, 2 * first, 2 * first + 2, 4 * first + 2, 8 * first + 2, limit - 2, limit})
        steps = [t for t in steps if t % 2 == 0 and t >= m_min and t <= limit]
        bad_grow = []
        for t in steps:
            f1 = with_header_end(t)
            if ds.parse(f1, "f").pix_off != t:
                bad_grow.append(("pix_off", t))
                continue
            rr = describe(parse_replies(run(binary, request([f1])))[0])
            if rr[4] is not None or rr[1] != repack(f1):
                bad_grow.append((t, rr[4]))
        check("headers ending at %d byte offsets around the first piece (%d) and each doubling up to the limit (%d): all give the right voxels" % (len(steps), first, limit),
              len(steps) >= (6 if first >= 1000 else 2) and not bad_grow, str((steps, bad_grow[:3])))
        # a file just shorter than twice the bytes held when the header has still not ended: the next read must stop exactly at the
        # end of the file (a read two bytes too long would eat the start of the next request). Only the real build has room for it.
        if first >= 1000:
            bad_len = []
            for X in (first, 2 * first, 4 * first):
                tot = 2 * X - 2
                for t in (tot, tot + 2, tot - 2):
                    po_t = X + 100                                      # the header ends after the X bytes held and before the end of the file
                    f1 = with_header_end(po_t)
                    f1 = f1 + b"\x5a" * (t - len(f1))                    # padding after the pixel data brings the file to exactly t bytes
                    if len(f1) != t:
                        bad_len.append(("length", t, len(f1)))
                        continue
                    rp = parse_replies(run(binary, request([f1]) + request(follow)))
                    if len(rp) != 2 or describe(rp[0])[4] is not None or describe(rp[0])[1] != repack(f1) or not good_shape(rp[1], chunk):
                        bad_len.append((t, [describe(r)[4] for r in rp]))
            check("files of exactly 2X-2, 2X and 2X-4 bytes (X = %d, %d, %d) whose header ends after X bytes: read to their last byte and not beyond, and the next request is answered"
                  % (first, 2 * first, 4 * first), not bad_len, str(bad_len[:3]))
        # errors that are found only AFTER the first piece must be the same errors as with the whole head buffer
        pad = min(2 * first + 100, hdr_max - m_min - 40) // 2 * 2            # the header ends after the first piece but inside the head buffer
        assert pad > 0 and m_min + pad > first, (pad, m_min, first)

        def big_header(**kw):
            return gen.make(rows=6, cols=6, pos=(0, 0, 0.0), iop=(1, 0, 0, 0, 1, 0), spacing=(1, 1), inst=0, uid=b"9.2", thick=1.0,
                            extra_before=gen.enc(0x0009, 0x1001, b"OB", b"\x01" * pad, True), **kw)
        late = {
            "pixel data runs past the end of the file": (big_header()[:-5], "PARSE:PIXEL_TRUNC"),
            "colour image": (big_header(spp=3), "CHECK:COLOUR"),
            "two frames": (big_header(nframes=2), "CHECK:MULTIFRAME"),
            "MONOCHROME1": (big_header(photometric=b"MONOCHROME1"), "NII:PHOTOMETRIC"),
        }
        nopix = big_header()
        nopix = nopix[:nopix.index(struct.pack("<HH", 0x7FE0, 0x10))]
        late["no pixel data at all"] = (nopix, "PARSE:NO_PIXEL")
        late["a header that does not end in the head buffer"] = (gen.make(rows=6, cols=6, extra_before=gen.enc(0x0009, 0x1001, b"OB", b"\x01" * (hdr_max + 200), True)),
                                                                 "PARSE:HEADER_TOO_LARGE")
        bad_late = []
        for label, (f1, want) in late.items():
            rep1 = parse_replies(run(binary, request([f1]) + request(follow)))
            e1 = describe(rep1[0])[4] if len(rep1) == 2 else "replies=%d" % len(rep1)
            if e1 != (0, want) or not good_shape(rep1[-1], chunk):
                bad_late.append((label, e1))
        check("%d errors found only after the first piece are the same errors as with the whole head buffer, and the next request is answered" % len(late), not bad_late, str(bad_late[:3]))

        # ---- streaming starts from the first piece: when only part of a big file has arrived, almost everything that arrived has
        # already been converted and sent (a device that waited for the whole head buffer would have sent nothing yet). The
        # file has more pixel data than the head buffer holds, so a cut inside it leaves the device waiting for the rest.
        side = 200 if hdr_max < 2000 else 300
        f_big = gen.make(rows=side, cols=side, vals=[(i * 7) % 4000 for i in range(side * side)], pos=(0, 0, 0.0), iop=(1, 0, 0, 0, 1, 0),
                         spacing=(1, 1), inst=0, uid=b"9.1", thick=1.0)
        po = ds.parse(f_big, "f").pix_off
        slack = first + 2 * chunk + 4
        want_all = repack(f_big)
        cuts_px = {"inside the head buffer (the old design had sent nothing yet)": (hdr_max - po) // 2,
                   "beyond the head buffer": min(side * side * 2 - 1000, hdr_max + 8000)}
        bad_early = []
        for label, arrived_px in cuts_px.items():
            if arrived_px <= slack + 100:
                continue                                    # the tiny build has too little room for this cut to prove anything
            rq = request([f_big])
            rep_cut = parse_replies(run(binary, rq[:len(rq) - len(f_big) + po + arrived_px], timeout=300))
            vox = describe(rep_cut[-1])[1]
            if not (arrived_px - slack <= len(vox) <= arrived_px and vox == want_all[:len(vox)]):
                bad_early.append((label, arrived_px, len(vox)))
        check("streaming starts early: after %s pixel bytes of a %d-byte image have arrived, all but %d of them are already converted and sent, all right"
              % (" / ".join(str(v) for v in cuts_px.values()), side * side * 2, slack), not bad_early, str(bad_early))

        rep = parse_replies(run(binary, request([], nfiles=4096)))
        check("a request for exactly 4096 files is accepted (it then times out waiting for them); 4097 is refused above",
              describe(rep[0])[0][:1] == [b"A"] and describe(rep[0])[4] == (0, "FILE:TIMEOUT"), str(describe(rep[0])[4]))
        # a later file that contains a whole request: after an error it must be read and dropped, not answered
        trap = gen.make(rows=6, cols=6, pos=(0, 0, 1.0), iop=(1, 0, 0, 0, 1, 0), spacing=(1, 1), inst=1, uid=b"1.2.3.4", thick=1.0,
                        extra_before=gen.enc(0x0009, 0x1001, b"OB", request(follow) + b"\x00" * (len(follow) % 2), True))
        rep = parse_replies(run(binary, request([b"not dicom" * 20, trap, trap]) + request(follow)))
        check("after an error, the rest of the request is dropped even if a later file contains a whole request: exactly two replies",
              len(rep) == 2 and describe(rep[0])[4] == (0, "PARSE:PREAMBLE") and good_shape(rep[1], chunk) and describe(rep[1])[1] == follow_expect, str(len(rep)))

        # ---- labels of different lengths one after another: nothing of the longer one may survive in the shorter
        labs = [(b"SubjectNumberOne", b"T1c-post", b"SiteBerlin", b"Scanner-3T"), (b"P", b"T", b"S", b"C"), (b"Mid_1", b"Mid_2", b"Mid_3", b"Mid_4")]
        rep = parse_replies(run(binary, b"".join(request(follow, labels=l) for l in labs)))
        lab_ok = len(rep) == 3
        for l, fr in zip(labs, rep):
            lab_ok = lab_ok and describe(fr)[3] == harness_header_and_json(follow, l)[1] and good_shape(fr, chunk)
        check("labels of different lengths in consecutive requests: each JSON carries exactly its own labels (equal to the verified harness)", lab_ok, "")

        # ---- request-level errors (the host sends nothing more) and the request after them
        req_bad = []
        hdr_only = lambda **kw: request([], nfiles=kw.pop("nfiles", 3), **kw)       # the files are NOT sent after a request-level error
        for label, req, want in [
            ("flags set", hdr_only(flags=1), "REQUEST:BAD_FLAGS"),
            ("no files", hdr_only(nfiles=0), "REQUEST:BAD_COUNT"),
            ("4097 files", hdr_only(nfiles=4097), "REQUEST:BAD_COUNT"),
            ("empty subject", hdr_only(labels=(b"", b"T1c", b"S02", b"C01")), "REQUEST:BAD_LABELS"),
            ("empty scanner", hdr_only(labels=(b"P", b"T1c", b"S02", b"")), "REQUEST:BAD_LABELS"),
            ("33-character site", hdr_only(labels=(b"P", b"T1c", b"s" * 33, b"C01")), "REQUEST:BAD_LABELS"),
            ("space in sequence", hdr_only(labels=(b"P", b"T 1", b"S02", b"C01")), "REQUEST:BAD_LABELS"),
            ("NUL inside a label", hdr_only(labels=(b"ab\0cd", b"T1c", b"S02", b"C01")), "REQUEST:BAD_LABELS"),
            ("255-byte label", hdr_only(labels=(b"a" * 255, b"T1c", b"S02", b"C01")), "REQUEST:BAD_LABELS"),
            ("non-ASCII label", hdr_only(labels=(b"caf\xc3\xa9", b"T1c", b"S02", b"C01")), "REQUEST:BAD_LABELS")]:
            rep = parse_replies(run(binary, req + request(follow)))
            if len(rep) != 2:
                req_bad.append((label, "replies", len(rep)))
                continue
            e = describe(rep[0])[4]
            ok = e == (NO_FILE, want) and [t for t, _ in rep[0]] == [b"E"]
            ok = ok and good_shape(rep[1], chunk) and describe(rep[1])[1] == follow_expect
            if not ok:
                req_bad.append((label, e))
        check("10 request-level errors: reported with the fixed name, no files read, and the next request answered", not req_bad, str(req_bad[:3]))

        # ---- noise, and a request cut off at every kind of place
        noise = b"\x00\xffDCM" + b"DC" + b"junk\n" * 3
        rep = parse_replies(run(binary, noise + request(follow) + b"\x01\x02DCM2" + request(follow)))
        check("noise before and between requests is skipped (including a partial magic)", len(rep) == 2 and all(
            good_shape(r, chunk) and describe(r)[1] == follow_expect for r in rep), str(len(rep)))
        full = request(follow)
        H = 28                                              # request header: magic 4 + flags/count 8 + four short labels 16
        files_part = lambda fs: b"".join(struct.pack("<I", len(f)) + f for f in fs)
        bigreq = request(big)
        start1 = H + 4 + len(big[0])                        # where the second big file's length word starts
        tail_files = next(f for lab, f in good if lab == "padding after the pixel data")
        tailreq = request(tail_files)
        long_pad = [gen.make(rows=6, cols=6, pos=(0, 0, k * 1.0), iop=(1, 0, 0, 0, 1, 0), spacing=(1, 1), inst=k, uid=b"9.3", thick=1.0,
                             extra_after_px=b"\x5a" * 140000) for k in range(2)]          # longer than either head buffer: read and dropped
        longreq = request(long_pad)
        cuts = [("inside the flags", full[:6]), ("inside the labels", full[:17]), ("right after the labels", full[:H]),
                ("inside a file length", full[:H + 2]), ("inside a file header", full[:H + 4 + 30]),
                ("inside the pixel data of a file that is longer than the head buffer", bigreq[:start1 + 4 + 1023 + 100]),
                ("inside the pixel data, 1500 bytes into the second file", bigreq[:start1 + 4 + 1500]),
                ("inside the padding after the pixel data", tailreq[:len(tailreq) - 3]),
                ("inside a 140 KB padding that is read and dropped", longreq[:len(longreq) - 3]), ("one byte short", full[:-1])]
        srcs = {1023 + 100 + start1 + 4: big, 1500 + start1 + 4: big}

        def px_present(files, cut):
            """Upper bound of the pixel bytes of `files` that are inside the first `cut` bytes of their request."""
            tot, at = 0, H
            for f in files:
                got = min(cut - (at + 4), len(f))
                if got > 0:
                    img = ds.parse(f, "f")
                    tot += max(0, min(got - img.pix_off, len(repack(f))))
                at += 4 + len(f)
            return tot
        cut_bad = []
        for where, data in cuts:
            rep = parse_replies(run(binary, data, timeout=300))
            e = describe(rep[-1])[4] if rep else None
            if len(rep) != 1 or e is None or e[1] not in ("FILE:TIMEOUT", "REQUEST:TIMEOUT"):
                cut_bad.append((where, e))
                continue
            vox = describe(rep[-1])[1]               # only converted bytes of pixels that really arrived, and the right ones
            files = big if data[:len(bigreq)] == bigreq[:len(data)] and len(data) <= len(bigreq) and where.startswith("inside the pixel") else (
                tail_files if where == "inside the padding after the pixel data" else long_pad if "140 KB" in where else follow)
            want = b"".join(repack(f) for f in files)
            if vox != want[:len(vox)] or len(vox) > px_present(files, len(data)):
                cut_bad.append((where, "voxels", len(vox)))
        check(f"a request cut off at {len(cuts)} kinds of place is answered with a TIMEOUT error and the device does not hang", not cut_bad, str(cut_bad[:3]))

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    sys.exit(1 if failures else 0)


main()
