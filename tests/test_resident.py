#!/usr/bin/env python3
"""Tests for the resident-image protocol (RES1: pico/src/resident.c, pico/tools/pico_resident.py) without hardware:
build32/proto_test is the same protocol, pipeline and assembly the firmware uses, run under qemu-riscv32.

Expected frames come from sources independent of the resident code: the verified Linux harness (build32/filter) for the
3x3 filters with a window, and a model written here (the symmetric kernel laid out in full, edges repeated, integer
arithmetic with the same rounding) for the Gaussian, then windowed by the same harness. Checked:
  * bit-exact display frames for many images x filters x passes x windows, all starting from the ORIGINAL image
    (the same step twice, or after others, gives the same frame)
  * GET returns the front frame; a frame computed without SEND is the one GET returns; RAW16 gives the exact 16-bit
    rows and leaves the display frames and the frame counter alone
  * any other request (STR1, IMG1) drops the image (PS_NO_IMAGE) and a new LOAD works afterwards
  * every bad request: status and the device still in step afterwards (bad command / reserved / flags, bad dimensions,
    maxval, sample, filter, passes, window, kernel, memory, APPLY / GET with nothing loaded, a host that stops mid-upload)
  * the memory the acknowledgement reports equals the layout (source + two display frames, each rounded to 16 bytes)
(The cycle and instruction counters read 0 under the emulator; they are checked on the board.)
"""
import array, os, random, re, struct, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "pico", "tools"))
import gauss_weights as gw
import pico_filter as pf
import pico_resident as pr
pf.TIMEOUT = float(os.environ.get("RESIDENT_TEST_TIMEOUT", 20))

DEVICE = f"qemu-riscv32 {os.path.join(ROOT, 'build32', 'proto_test')}"
HARNESS = os.path.join(ROOT, "build32", "filter")
ARENA = int(re.search(r"#define PROTO_ARENA_BYTES (\d+)u", open(os.path.join(ROOT, "pico", "src", "proto.h")).read()).group(1))
MAXPX = int(re.search(r"#define PROTO_MAX_PIXELS (\d+)u", open(os.path.join(ROOT, "pico", "src", "proto.h")).read()).group(1))
(PS_OK, PS_BAD_FILTER, PS_BAD_DIMS, PS_BAD_MAXVAL, PS_BAD_PASSES, PS_BAD_WINDOW, PS_BAD_SAMPLE, PS_TIMEOUT, PS_NO_MEMORY,
 PS_BAD_KERNEL, PS_NO_IMAGE, PS_BAD_CMD) = 0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12
failures = 0


def check(name, ok, detail=""):
    global failures
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))
    failures += not ok


def up16(n):
    return (n + 15) & ~15


# ------------------------------------------------------------------ references
def model(samples, W, H, w, passes):
    r, K = len(w) - 1, w[:0:-1] + w
    rows = [list(samples[y * W:(y + 1) * W]) for y in range(H)]
    for _ in range(passes):
        mid = [[(sum(K[j] * ([row[0]] * r + row + [row[-1]] * r)[x + j] for j in range(2 * r + 1)) + 8192) >> 14 for x in range(W)] for row in rows]
        rows = [[(sum(K[j] * mid[min(max(y + j - r, 0), H - 1)][x] for j in range(2 * r + 1)) + 8192) >> 14 for x in range(W)] for y in range(H)]
    return [v for row in rows for v in row]


def harness(W, H, maxval, values, name, passes, window):
    """The verified harness on a job; returns the output PGM bytes."""
    with tempfile.NamedTemporaryFile(suffix=".pgm") as f:
        f.write(pf.pgm_bytes(W, H, maxval, array.array("H", values))); f.flush()
        return pf.harness_output(f.name, name, passes, window, HARNESS)


def ref_frame(W, H, maxval, samples, step, window):
    """Expected display frame (as a PGM) of one step: ('3x3', name, passes) or ('gauss', kernel, passes)."""
    kind, what, passes = step
    if kind == "gauss":
        vals = model(list(samples), W, H, what, 1 if passes is None else passes)
        return harness(W, H, maxval, vals, "identity", 1, window)
    return harness(W, H, maxval, samples, what, passes, window)


def ref_raw(W, H, maxval, samples, step):
    kind, what, passes = step
    if kind == "gauss":
        return pf.pgm_bytes(W, H, maxval, array.array("H", model(list(samples), W, H, what, 1 if passes is None else passes)))
    return harness(W, H, maxval, samples, what, passes, None)


def frame_pgm(W, H, frame):
    return pf.pgm_bytes(W, H, 255, frame)


def rand_samples(n, maxval, rng):
    return array.array("H", [rng.randrange(maxval + 1) for _ in range(n)])


# ------------------------------------------------------------------ talking to the device
class Dev:
    def __init__(self):
        self.link = pf.PipeLink(DEVICE)
        self.s = pr.ResidentSession(self.link)

    def close(self):
        self.link.close()

    def apply_step(self, step, window, **kw):
        kind, what, passes = step
        if kind == "gauss":
            return self.s.apply(None, passes, window, what, **kw)
        return self.s.apply(what, passes, window, None, **kw)

    def status_of(self, fn):
        """Run fn(); return the status of the ProtoError it raises (or 0 if it succeeds)."""
        try:
            fn()
            return 0
        except pf.ProtoError as e:
            return e.status

    def raw(self, cmd, *, w=0, h=0, maxval=0, flags=0, passes=pf.PASSES_DEFAULT, center=0, width=0, name="", extra=b"",
            reserved=(0, 0, 0)):
        """Send an arbitrary header (and bytes after it); return the first response."""
        hdr = struct.pack(pr.HDR_FMT, pr.RES_MAGIC, cmd, w, h, maxval, flags, passes, center, width, reserved[0],
                          name.encode()[:16], reserved[1], reserved[2])
        self.link.write(hdr + extra)
        return pf.read_response(self.link, "raw")

    def good_after(self, label):
        """A complete LOAD + APPLY + GET right after: proves the device is back at a message boundary."""
        rng = random.Random(7)
        try:
            smp = rand_samples(9 * 5, 255, rng)
            self.s.load(9, 5, 255, smp)
            res, fr = self.s.apply("blur", 1, (128, 256))
            _, fr2 = self.s.get()
            ok = fr == fr2 and frame_pgm(9, 5, fr) == ref_frame(9, 5, 255, smp, ("3x3", "blur", 1), (128, 256))
            return ok, f"{label}: the next jobs gave a wrong frame"
        except Exception as e:                       # noqa: BLE001 - reported as a failure
            return False, f"{label}: the next jobs failed ({e})"


def main():
    for p in (os.path.join(ROOT, "build32", "proto_test"), HARNESS):
        if not os.path.exists(p):
            sys.exit("build the emulator programs first: make test-resident")
    rng = random.Random(20261111)
    d = Dev()

    # ---------------------------------------------------------------- frames
    steps = [("3x3", "identity", None), ("3x3", "blur", 2), ("3x3", "sobel", None), ("3x3", "gaussian", 3),
             ("3x3", "blur", 0), ("gauss", gw.gaussian(1.0, 3), None), ("gauss", gw.gaussian(0.7, 2), 2),
             ("gauss", gw.box(1), None), ("gauss", gw.gaussian(2.0, 6), None), ("gauss", [gw.TOTAL], None),
             ("gauss", gw.gaussian(1.3, 4), 1)]
    bad = []
    n_frames = 0
    sizes = [(1, 1, 255), (5, 3, 255), (16, 12, 1023), (31, 17, 4095), (64, 40, 4095), (37, 29, 65535), (100, 9, 255)]
    for (W, H, maxval) in sizes:
        smp = rand_samples(W * H, maxval, rng)
        d.s.load(W, H, maxval, smp)
        order = steps[:]
        rng.shuffle(order)
        for step in order:
            win = [(maxval // 2, min(maxval + 1, 65535)), (maxval // 3, max(1, maxval // 2)), (0, 1)][rng.randrange(3)]
            res, fr = d.apply_step(step, win)
            n_frames += 1
            if frame_pgm(W, H, fr) != ref_frame(W, H, maxval, smp, step, win):
                bad.append((W, H, maxval, step[0], step[2], win))
    check(f"display frames: {n_frames} frames ({len(sizes)} images x {len(steps)} filters in random order, 3x3 filters and Gaussians, "
          f"random windows) equal the harness / model bit for bit", not bad, str(bad[:3]))

    # the default window is the whole range
    W, H, maxval = 24, 10, 4095
    smp = rand_samples(W * H, maxval, rng)
    d.s.load(W, H, maxval, smp)
    _, fr = d.s.apply("identity", 1, None)
    check("no window given: the whole range (centre maxval/2, width maxval+1)",
          frame_pgm(W, H, fr) == harness(W, H, maxval, smp, "identity", 1, (maxval // 2, min(maxval + 1, 65535))))

    # every step starts from the original image
    W, H, maxval = 33, 21, 4095
    smp = rand_samples(W * H, maxval, rng)
    d.s.load(W, H, maxval, smp)
    seq = [("3x3", "blur", 3), ("gauss", gw.gaussian(1.5, 5), None), ("3x3", "sobel", None), ("3x3", "blur", 3)]
    frames = [d.apply_step(s, (2000, 3000))[1] for s in seq]
    check("every step starts from the original image: blur, gauss, sobel, blur again gives the first frame again",
          frames[0] == frames[3] and frames[0] != frames[1] and frames[1] != frames[2])

    # ---------------------------------------------------------------- GET, SEND, RAW16, the counter
    d.s.load(W, H, maxval, smp)
    err = d.status_of(d.s.get)
    check("GET before any frame: PS_NO_IMAGE", err == PS_NO_IMAGE, str(err))
    step = ("gauss", gw.gaussian(1.0, 3), None)
    res, none = d.apply_step(step, (2000, 3000), send=False)
    check("APPLY without SEND sends no rows and counts the frame", none is None and res["reserved"] == 1 and res["payload"] == 0, str(res))
    res, fr = d.s.get()
    check("GET returns the frame computed without SEND, exactly", frame_pgm(W, H, fr) == ref_frame(W, H, maxval, smp, step, (2000, 3000))
          and res["reserved"] == 1 and res["payload"] == W * H)
    _, fr_b = d.apply_step(("3x3", "blur", 1), (2000, 3000))
    res, fr_get = d.s.get()
    check("GET after another APPLY returns that newer frame", fr_get == fr_b and res["reserved"] == 2)
    res, raw = d.apply_step(step, None, raw16=True)
    check("RAW16: the 16-bit rows are the exact filter output",
          pf.pgm_bytes(W, H, maxval, array.array("H", struct.unpack("<%dH" % (W * H), raw))) == ref_raw(W, H, maxval, smp, step))
    res2, fr_after = d.s.get()
    check("RAW16 leaves the front frame and the frame counter alone", fr_after == fr_b and res2["reserved"] == 2 and res["reserved"] == 2)
    res, nothing = d.apply_step(step, None, raw16=True, send=False)
    check("RAW16 without SEND: nothing is sent, nothing changes", nothing is None and res["reserved"] == 2)
    raw3 = d.apply_step(("3x3", "sobel", None), None, raw16=True)[1]
    check("RAW16 of a 3x3 filter equals the harness's 16-bit output",
          pf.pgm_bytes(W, H, maxval, array.array("H", struct.unpack("<%dH" % (W * H), raw3))) == ref_raw(W, H, maxval, smp, ("3x3", "sobel", None)))

    # ---------------------------------------------------------------- the memory report
    px = W * H
    want_rest = ARENA - (up16(px * 2) + 2 * up16(px))
    ack = d.raw(pr.RES_APPLY, flags=pr.F_SEND, center=2000, width=3000, name="identity")
    rows = 0
    while True:                                                    # read the rest of this request
        head = d.link.read_exact(4)
        if head == pf.ROW_MAGIC:
            d.link.read_exact(4 + ack["payload"]); rows += 1
        else:
            d.link.read_exact(pf.RESP_SIZE - 4); break
    check("the acknowledgement of an APPLY reports the bytes left for the pipeline", ack["reserved"] == want_rest and rows == H,
          f"{ack['reserved']} vs {want_rest}")

    # ---------------------------------------------------------------- the pipeline filling all the memory left
    W, H, maxval = 1000, 3, 4095
    smp = rand_samples(W * H, maxval, rng)
    d.s.load(W, H, maxval, smp)
    win = (2000, 3000)
    most = 0
    for p in range(1, 256):
        if d.status_of(lambda: d.apply_step(("3x3", "blur", p), win, send=False)) == PS_NO_MEMORY:
            break
        most = p
    d.s.load(W, H, maxval, smp)
    _, fr = d.apply_step(("3x3", "blur", most), win)
    _, same = d.s.get()
    _, ident = d.apply_step(("3x3", "identity", 1), win)
    check(f"the most passes that fit ({most}, of 255; the next gets PS_NO_MEMORY) exactly fill the pipeline region: that frame, the source "
          f"image and the next frame are all intact",
          0 < most < 255 and frame_pgm(W, H, fr) == ref_frame(W, H, maxval, smp, ("3x3", "blur", most), win) and same == fr
          and frame_pgm(W, H, ident) == ref_frame(W, H, maxval, smp, ("3x3", "identity", 1), win))

    # ---------------------------------------------------------------- bigger image, the limit
    W2, H2 = 300, 300
    big = rand_samples(W2 * H2, 4095, rng)
    d.s.load(W2, H2, 4095, big)
    _, fr = d.apply_step(("3x3", "blur", 1), (2000, 3000))
    check("a 300 x 300 image (90,000 pixels, the whole-image limit) loads and filters exactly",
          frame_pgm(W2, H2, fr) == ref_frame(W2, H2, 4095, big, ("3x3", "blur", 1), (2000, 3000)))
    err = d.status_of(lambda: d.s.load(90001, 1, 255, array.array("H", [0] * 90001)))
    check("90,001 pixels: PS_BAD_DIMS", err == PS_BAD_DIMS, str(err))
    d.s.load(MAXPX, 1, 255, array.array("H", [7] * MAXPX))
    err = d.status_of(lambda: d.apply_step(("3x3", "blur", 1), (128, 256)))
    check("a row of 90,000 samples: the pipeline does not fit the memory left -> PS_NO_MEMORY, and the device stays in step",
          err == PS_NO_MEMORY and d.good_after("no memory")[0], str(err))
    d.s.load(2000, 20, 255, rand_samples(40000, 255, rng))
    err = d.status_of(lambda: d.apply_step(("gauss", gw.gaussian(5.0, 15), 255), (128, 256)))
    check("255 passes of a radius-15 kernel on a 2000-wide image: PS_NO_MEMORY", err == PS_NO_MEMORY, str(err))

    # ---------------------------------------------------------------- other requests drop the image
    smp = rand_samples(40 * 30, 255, rng)
    d.s.load(40, 30, 255, smp)
    pf.process_stream(d.link, "blur", 8, 8, 255, rand_samples(64, 255, rng))
    check("after an STR1 job: APPLY answers PS_NO_IMAGE", d.status_of(lambda: d.apply_step(("3x3", "blur", 1), (128, 256))) == PS_NO_IMAGE)
    check("after an STR1 job: GET answers PS_NO_IMAGE", d.status_of(d.s.get) == PS_NO_IMAGE)
    d.s.load(40, 30, 255, smp)
    pf.process(d.link, "blur", 8, 8, 255, rand_samples(64, 255, rng), 1)
    check("after an IMG1 job: APPLY answers PS_NO_IMAGE", d.status_of(lambda: d.apply_step(("3x3", "blur", 1), (128, 256))) == PS_NO_IMAGE)
    d.s.load(40, 30, 255, smp)
    _, fr = d.apply_step(("3x3", "blur", 1), (128, 256))
    check("a new LOAD after that works and is exact", frame_pgm(40, 30, fr) == ref_frame(40, 30, 255, smp, ("3x3", "blur", 1), (128, 256)))
    d.s.load(40, 30, 255, smp)
    d.apply_step(("3x3", "blur", 1), (128, 256))
    d.s.load(7, 7, 255, rand_samples(49, 255, rng))
    check("a new LOAD resets the frame counter and the front frame (GET: PS_NO_IMAGE)", d.status_of(d.s.get) == PS_NO_IMAGE)

    # ---------------------------------------------------------------- bad requests
    d2 = Dev()
    check("APPLY with nothing ever loaded: PS_NO_IMAGE", d2.status_of(lambda: d2.apply_step(("3x3", "blur", 1), (1, 2))) == PS_NO_IMAGE)
    check("GET with nothing ever loaded: PS_NO_IMAGE", d2.status_of(d2.s.get) == PS_NO_IMAGE)
    ok, why = d2.good_after("nothing loaded"); check("... and the device is in step afterwards", ok, why)

    def expect(label, status, resp):
        ok, why = d.good_after(label)
        check(f"{label}: status {status}, and the device is in step afterwards", resp["status"] == status and ok,
              f"got {resp['status']}; {why if not ok else ''}")

    W, H, maxval = 12, 8, 255
    smp = rand_samples(W * H, maxval, rng)
    d.s.load(W, H, maxval, smp)
    expect("unknown command 0", PS_BAD_CMD, d.raw(0))
    expect("unknown command 4", PS_BAD_CMD, d.raw(4))
    expect("reserved word 9 not zero", PS_BAD_CMD, d.raw(pr.RES_APPLY, reserved=(1, 0, 0), name="blur", center=1, width=2))
    expect("reserved word 14 not zero", PS_BAD_CMD, d.raw(pr.RES_APPLY, reserved=(0, 1, 0), name="blur", center=1, width=2))
    expect("reserved word 15 not zero", PS_BAD_CMD, d.raw(pr.RES_APPLY, reserved=(0, 0, 1), name="blur", center=1, width=2))
    expect("APPLY with a width", PS_BAD_CMD, d.raw(pr.RES_APPLY, w=12, name="blur", center=1, width=2))
    expect("APPLY with a maxval", PS_BAD_CMD, d.raw(pr.RES_APPLY, maxval=255, name="blur", center=1, width=2))
    expect("GET with a height", PS_BAD_CMD, d.raw(pr.RES_GET, h=8))
    expect("GET with flags", PS_BAD_CMD, d.raw(pr.RES_GET, flags=1))
    expect("LOAD with flags", PS_BAD_CMD, d.raw(pr.RES_LOAD, w=4, h=4, maxval=255, flags=1))
    expect("APPLY with an unknown flag bit", PS_BAD_CMD, d.raw(pr.RES_APPLY, flags=8, name="blur", center=1, width=2))
    d.s.load(W, H, maxval, smp)
    expect("unknown filter", PS_BAD_FILTER, d.raw(pr.RES_APPLY, name="nosuchfilter", center=1, width=2))
    d.s.load(W, H, maxval, smp)
    expect("256 passes", PS_BAD_PASSES, d.raw(pr.RES_APPLY, name="blur", passes=256, center=1, width=2))
    d.s.load(W, H, maxval, smp)
    expect("window width 0", PS_BAD_WINDOW, d.raw(pr.RES_APPLY, name="blur", center=1, width=0))
    d.s.load(W, H, maxval, smp)
    expect("window width 65536", PS_BAD_WINDOW, d.raw(pr.RES_APPLY, name="blur", center=1, width=65536))
    d.s.load(W, H, maxval, smp)
    expect("window centre 65536", PS_BAD_WINDOW, d.raw(pr.RES_APPLY, name="blur", center=65536, width=2))
    for label, kernel in (("weights that do not sum to 2^14", struct.pack("<I2i", 1, 100, 100)),
                          ("a negative weight (the sum is right)", struct.pack("<I3i", 2, 22384, -2000, -1000)),
                          ("a radius above 15", struct.pack("<I", 16))):
        d.s.load(W, H, maxval, smp)
        expect(label, PS_BAD_KERNEL, d.raw(pr.RES_APPLY, flags=pr.F_SEP, center=1, width=2, extra=kernel))
    d.s.load(W, H, maxval, smp)
    expect("RAW16 needs no window (width 0 is fine) - but an unknown filter still fails", PS_BAD_FILTER,
           d.raw(pr.RES_APPLY, flags=pr.F_RAW16, name="nosuchfilter"))
    expect("LOAD: width 0", PS_BAD_DIMS, d.raw(pr.RES_LOAD, w=0, h=4, maxval=255))
    expect("LOAD: height 0", PS_BAD_DIMS, d.raw(pr.RES_LOAD, w=4, h=0, maxval=255))
    expect("LOAD: more than 90,000 pixels", PS_BAD_DIMS, d.raw(pr.RES_LOAD, w=1000, h=1000, maxval=255))
    expect("LOAD: maxval 0", PS_BAD_MAXVAL, d.raw(pr.RES_LOAD, w=4, h=4, maxval=0))
    expect("LOAD: maxval 65536", PS_BAD_MAXVAL, d.raw(pr.RES_LOAD, w=4, h=4, maxval=65536))
    # a sample above maxval, in the first, a middle and the last position
    for pos in (0, 7, 15):
        vals = [3] * 16
        vals[pos] = 256
        ack = d.raw(pr.RES_LOAD, w=4, h=4, maxval=255)
        d.link.write(pf.to_le_bytes(vals))
        res = pf.read_response(d.link, "samples")
        ok, why = d.good_after("bad sample")
        check(f"LOAD with a sample above maxval at position {pos}: PS_BAD_SAMPLE, no resident image, device in step",
              ack["status"] == 0 and res["status"] == PS_BAD_SAMPLE and ok, f"{res['status']} {why}")
    d.s.load(W, H, maxval, smp)
    d.raw(pr.RES_LOAD, w=4, h=4, maxval=255)
    d.link.write(pf.to_le_bytes([3] * 15 + [256]))
    pf.read_response(d.link, "samples")
    check("a failed LOAD leaves no resident image behind (the earlier one is gone too)", d.status_of(lambda: d.apply_step(("3x3", "blur", 1), (1, 2))) == PS_NO_IMAGE)
    d.close()

    # a host that stops in the middle of the upload
    d3 = Dev()
    ack = d3.raw(pr.RES_LOAD, w=8, h=8, maxval=255)
    d3.link.write(pf.to_le_bytes([1] * 20))
    d3.link.close_stdin()
    res = pf.read_response(d3.link, "samples")
    check("a host that stops mid-upload gets PS_TIMEOUT", ack["status"] == 0 and res["status"] == PS_TIMEOUT, str(res))
    d3.close()
    d2.close()

    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
