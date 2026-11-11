#!/usr/bin/env python3
"""Tests for the viewer (pico/tools/pico_viewer.py) without hardware and without a display: the board is the protocol under
qemu-riscv32 (build32/proto_test) and the window runs on Qt's offscreen platform.

ViewerCore (no GUI):
  * the first frame arrives row by row, rows 0..h-1 in order, then the frame is complete and bit-exact against the verified
    harness / model (the same references as tests/test_resident.py)
  * a burst of requests while a frame is in flight: only the latest is kept; every frame that completes is complete (all rows, in
    order), equals the reference of one of the requests, frames never go back to an older request, and the last frame is the
    last request
  * a request the board refuses (unknown filter, window width 0, sigma 0) is reported and the core carries on
  * a dead link ends in an error, not a hang; close() never hangs
The window (needs PySide6; skipped otherwise):
  * the picture filled progressively and equal to the reference when the frame is done
  * moving the sigma slider, choosing another filter, the window sliders and the auto-window button each give the right frame
  * the controls follow the filter (the sigma slider is only enabled for the Gaussian); the status line reports the frame
"""
import importlib.util, os, sys, threading, time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for sub in ("scripts", os.path.join("pico", "tools"), "tests"):
    sys.path.insert(0, os.path.join(ROOT, sub))
import gauss_weights as gw
import pico_filter as pf
import pico_viewer as pv
import test_resident as tr          # the reference frames (harness / model) and the emulator command

pf.TIMEOUT = float(os.environ.get("VIEWER_TEST_TIMEOUT", 20))
failures = 0


def check(name, ok, detail=""):
    global failures
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))
    failures += not ok


def reference(W, H, maxval, samples, p):
    """The expected frame (as a PGM) for Params p."""
    win = (p.center, p.width)
    if p.kind == "gauss":
        r = pv.auto_radius(p.sigma) if p.radius is None else p.radius
        return tr.ref_frame(W, H, maxval, samples, ("gauss", gw.gaussian(p.sigma, r), p.passes), win)
    return tr.ref_frame(W, H, maxval, samples, ("3x3", p.kind, p.passes), win)


class Recorder:
    """Collects a core's callbacks; frames[n] = {'rows': [...], 'info': ..., 'dt': ...}."""

    def __init__(self, row_delay=0.0):
        self.loaded = []
        self.frames = {}
        self.errors = []
        self.order = []
        self.row_delay = row_delay
        self.lock = threading.Lock()

    def on_loaded(self, *a):
        self.loaded.append(a)

    def on_row(self, n, i, row):
        with self.lock:
            self.frames.setdefault(n, {"rows": []})["rows"].append((i, bytes(row)))
        if self.row_delay:
            time.sleep(self.row_delay)

    def on_done(self, n, info, dt):
        with self.lock:
            self.frames.setdefault(n, {"rows": []}).update(info=info, dt=dt)
            self.order.append(n)

    def on_error(self, text):
        self.errors.append(text)

    def frame_pgm(self, n, W, H):
        return pf.pgm_bytes(W, H, 255, b"".join(r for _, r in self.frames[n]["rows"]))


def wait(cond, timeout=8.0, step=0.002):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return False


def new_core(rec, W, H, maxval, samples):
    link = pf.PipeLink(tr.DEVICE)
    core = pv.ViewerCore(link, W, H, maxval, samples, rec.on_loaded, rec.on_row, rec.on_done, rec.on_error)
    core.start()
    return core, link


def helper_tests():
    # percentile_window against a known set: samples 0..1000 -> quantiles 1% = 10 and 99.5% = 995
    c, w = pv.percentile_window(list(range(1001)), 4095)
    check("auto window of the samples 0..1000: centre 502, width 985 (the 1% and 99.5% quantiles)", (c, w) == (502, 985), str((c, w)))
    check("auto window of a constant image has width 1 (never 0)", pv.percentile_window([7] * 50, 4095) == (7, 1))
    check("auto window never exceeds the device's width limit 65535", pv.percentile_window([0] * 10 + [65535] * 990, 65535)[1] == 65535)
    check("auto radius: ceil(3 sigma), at least 1, at most 15",
          [pv.auto_radius(x) for x in (0.3, 0.5, 1.0, 1.5, 2.0, 5.0, 8.0)] == [1, 2, 3, 5, 6, 15, 15],
          str([pv.auto_radius(x) for x in (0.3, 0.5, 1.0, 1.5, 2.0, 5.0, 8.0)]))
    import re
    names = re.findall(r'^n_[a-z0-9_]*: *\.asciz "([a-z0-9_]*)"', open(os.path.join(ROOT, "src", "kernel.s")).read(), re.M)
    check("the filter list is the Gaussian plus exactly the device's 3x3 filters (read from kernel.s)",
          sorted(pv.FILTERS) == sorted(["gauss"] + names), f"{sorted(pv.FILTERS)} vs {sorted(names)}")
    Viewer = pv.make_app_classes() if importlib.util.find_spec("PySide6") else None
    if Viewer:
        check("zoom: 240 -> 3, 512 -> 1, 64 -> 4 (at most 4), 1000 -> 1 (at least 1)",
              [Viewer._zoom_for(a, a) for a in (240, 512, 64, 1000)] == [3, 1, 4, 1], str([Viewer._zoom_for(a, a) for a in (240, 512, 64, 1000)]))


def core_tests():
    import random
    rng = random.Random(20261112)
    W, H, maxval = 33, 21, 4095
    samples = tr.rand_samples(W * H, maxval, rng)
    base = pv.Params(kind="gauss", sigma=1.0, center=2000, width=3000)

    # ---- one frame, row by row
    rec = Recorder()
    core, link = new_core(rec, W, H, maxval, samples)
    core.request(base)
    ok = wait(lambda: rec.order)
    rows = rec.frames[1]["rows"] if ok else []
    check("the first frame arrives row by row: rows 0..h-1 in order, then the frame is done, once the image is loaded",
          ok and [i for i, _ in rows] == list(range(H)) and rec.loaded == [(W, H, maxval)] and not rec.errors, str(rec.errors))
    check("... and the frame equals the harness / model bit for bit", ok and rec.frame_pgm(1, W, H) == reference(W, H, maxval, samples, base))
    check("... the info of the frame is the device's final response (frame counter 1)", ok and rec.frames[1]["info"]["reserved"] == 1)

    # ---- a burst while a frame is in flight
    slow = Recorder(row_delay=0.002)                              # a frame takes ~40 ms, so the requests pile up behind it
    core2, link2 = new_core(slow, W, H, maxval, samples)
    reqs = [pv.Params(kind="gauss", sigma=0.5 + 0.1 * k, center=1000 + 50 * k, width=2500) for k in range(30)]
    wait(lambda: slow.loaded)
    for p in reqs:
        core2.request(p)
        time.sleep(0.0005)
    ok = wait(core2.idle) and wait(lambda: len(slow.order) >= 1)
    refs = [reference(W, H, maxval, samples, p) for p in reqs]
    done = slow.order
    complete = all([i for i, _ in slow.frames[n]["rows"]] == list(range(H)) for n in done)
    which = []
    for n in done:
        got = slow.frame_pgm(n, W, H)
        which.append(next((k for k, r in enumerate(refs) if r == got), None))
    check(f"a burst of 30 requests: {len(done)} frames completed (the rest merged), each complete and in order",
          ok and complete and 1 <= len(done) < 30 and core2.skipped > 0, f"done={done} skipped={core2.skipped}")
    check("... every frame equals the reference of one of the requests, none goes back to an older request, the last frame is the last request",
          None not in which and which == sorted(which) and which[-1] == 29, str(which))

    # ---- refused requests, then a good one
    for label, p in (("an unknown filter", pv.Params(kind="nosuchfilter", center=1, width=2)),
                     ("window width 0", pv.Params(kind="blur", center=1, width=0)),
                     ("sigma 0", pv.Params(kind="gauss", sigma=0.0, center=1, width=2))):
        before = len(rec.errors)
        core.request(p)
        got = wait(lambda: len(rec.errors) > before)
        core.request(base)
        n_before = len(rec.order)
        ok = wait(lambda: len(rec.order) > n_before)
        check(f"{label}: reported ({rec.errors[-1][:48] if rec.errors else ''}...), and the next good request still gives a frame",
              got and ok and rec.frame_pgm(rec.order[-1], W, H) == reference(W, H, maxval, samples, base))

    # ---- close, and a dead link
    t0 = time.monotonic()
    core.close(); core2.close()
    link.close(); link2.close()
    check("close() returns promptly (idle core, and a core that was busy)", time.monotonic() - t0 < 5.0)
    dead = Recorder()
    dlink = pf.PipeLink("false")
    time.sleep(0.2)
    dcore = pv.ViewerCore(dlink, W, H, maxval, samples, dead.on_loaded, dead.on_row, dead.on_done, dead.on_error)
    dcore.start()
    ok = wait(lambda: dead.errors, timeout=10)
    t0 = time.monotonic()
    dcore.close()
    check("a dead link: an error is reported (no frame, no hang) and close() returns", ok and not dead.order and time.monotonic() - t0 < 5.0,
          str(dead.errors))
    dlink.close()


def window_tests():
    try:
        from PySide6 import QtWidgets
    except ImportError:
        print("SKIP  the window tests (PySide6 is not installed)")
        return
    import random
    rng = random.Random(20261113)
    W, H, maxval = 40, 30, 4095
    samples = tr.rand_samples(W * H, maxval, rng)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    link = pf.PipeLink(tr.DEVICE)
    first = pv.Params(kind="gauss", sigma=1.5, center=2000, width=3000)
    v = pv.build_viewer(link, "test", W, H, maxval, samples, first)
    v.show()

    def pump(cond, timeout=8.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            app.processEvents()
            if cond():
                return True
            time.sleep(0.002)
        return False

    def picture():
        return pf.pgm_bytes(W, H, 255, bytes(v.display.buf))

    ok = pump(lambda: v.frame_count >= 1)
    check("the window: the first frame is drawn, row by row (every row arrived), and equals the reference",
          ok and v.display.rows_seen == H and picture() == reference(W, H, maxval, samples, first))
    check("... the status line reports the frame and the round trip", "frame 1" in v.status.text() and "ms round trip" in v.status.text(), v.status.text())
    check("... the sigma slider and its label are enabled for the Gaussian, the label shows sigma and the radius",
          v.sigma.isEnabled() and "1.5" in v.sigma_label.text() and "radius 5" in v.sigma_label.text(), v.sigma_label.text())

    def frames_now():
        return v.frame_count

    n0 = frames_now()
    paints0 = v.display.paints
    v.sigma.setValue(25)
    ok = pump(lambda: frames_now() > n0 and v.core.idle())
    repainted = v.display.paints > paints0
    want = pv.Params(kind="gauss", sigma=2.5, center=2000, width=3000)
    check("moving the sigma slider to 2.5 gives the radius-8 Gaussian frame", ok and picture() == reference(W, H, maxval, samples, want),
          v.status.text())

    n0 = frames_now()
    v.kind.setCurrentText("sobel")
    ok = pump(lambda: frames_now() > n0 and v.core.idle())
    check("choosing sobel gives the Sobel frame and disables the sigma slider",
          ok and picture() == reference(W, H, maxval, samples, pv.Params(kind="sobel", center=2000, width=3000)) and not v.sigma.isEnabled())

    n0 = frames_now()
    v.center.setValue(1200)
    v.width.setValue(1800)
    ok = pump(lambda: frames_now() > n0 and v.core.idle())
    check("the window sliders give the frame with that window (two quick changes end on the final values)",
          ok and picture() == reference(W, H, maxval, samples, pv.Params(kind="sobel", center=1200, width=1800)))

    n0 = frames_now()
    v.kind.setCurrentText("blur")
    v.passes.setValue(2)
    ok = pump(lambda: frames_now() > n0 and v.core.idle())
    check("another 3x3 filter with a chosen number of passes", ok and
          picture() == reference(W, H, maxval, samples, pv.Params(kind="blur", passes=2, center=1200, width=1800)))

    n0 = frames_now()
    v.findChildren(QtWidgets.QPushButton)[0].click()
    ok = pump(lambda: frames_now() > n0 and v.core.idle())
    c, w = pv.percentile_window(samples, maxval)
    check("the auto-window button sets the window from the image and gives that frame",
          ok and (v.center.value(), v.width.value()) == (c, w) and
          picture() == reference(W, H, maxval, samples, pv.Params(kind="blur", passes=2, center=c, width=w)), f"{v.center.value()},{v.width.value()} vs {c},{w}")

    if os.environ.get("VIEWER_SHOT"):
        v.grab().save(os.environ["VIEWER_SHOT"])
    check("the picture was repainted while a frame arrived", repainted, str(v.display.paints))
    pump(lambda: False, 0.15)                                       # let everything settle
    p0, r0 = v.display.paints, v.display.rows_seen
    v.display.put_row(0, bytes(W))
    pump(lambda: v.display.paints > p0, 1.0)
    check("a new row marks the picture as changed and the next timer tick repaints it (nothing else triggered it)",
          v.display.paints > p0 and v.display.rows_seen == r0 + 1, f"{p0} -> {v.display.paints}")
    p0 = v.display.paints
    pump(lambda: False, 0.15)
    check("... and with no new row there is no repaint", v.display.paints == p0, f"{p0} -> {v.display.paints}")
    v.close()
    check("closing the window stops the core", wait(lambda: not v.core._thread.is_alive(), 10))


def cli_test():
    import subprocess, tempfile
    if not importlib.util.find_spec("PySide6"):
        return
    with tempfile.TemporaryDirectory() as d:
        pgm = os.path.join(d, "t.pgm")
        open(pgm, "wb").write(pf.pgm_bytes(16, 12, 255, bytes(range(192))))
        help_ = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "pico", "tools", "pico_viewer.py"), "--help"], capture_output=True, text=True)
        check("pico_viewer.py --help works", help_.returncode == 0 and "--exec" in help_.stdout)
        bad = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "pico", "tools", "pico_viewer.py"), pgm, "--sigma", "50", "--exec", "true"],
                             capture_output=True, text=True, env=dict(os.environ))
        check("a sigma outside 0.3..8 is refused before anything starts", bad.returncode != 0 and "sigma" in bad.stderr, bad.stderr[-120:])
        p = subprocess.Popen([sys.executable, "-I", os.path.join(ROOT, "pico", "tools", "pico_viewer.py"), pgm, "--exec", tr.DEVICE],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=dict(os.environ))
        time.sleep(4)
        alive = p.poll() is None
        p.terminate()
        try:
            _, err = p.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill(); _, err = p.communicate()
        check("started for real (offscreen, against the emulator): the window runs for 4 s without an error", alive and "Traceback" not in err and "Error" not in err, err[-200:])


def main():
    for p in (os.path.join(ROOT, "build32", "proto_test"), tr.HARNESS):
        if not os.path.exists(p):
            sys.exit("build the emulator programs first: make test-viewer")
    helper_tests()
    core_tests()
    window_tests()
    cli_test()
    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
