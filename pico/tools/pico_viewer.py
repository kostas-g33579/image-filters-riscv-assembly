#!/usr/bin/env python3
"""A window on the board's resident image: change the filter, the smoothing or the window with a slider and watch the result
being drawn row by row as it arrives from the board (protocol "RES1", pico/src/resident.h, docs/resident_mode.md).

    pico_viewer.py IMAGE.pgm [--port P | --exec CMD] [--filter gauss|blur|sobel|...] [--sigma S] [--window CENTER WIDTH] [--zoom N]

The image is uploaded once. Every change of a control asks the board for a new display frame; the board does all the
filtering (this window only draws), and the frame comes back over USB at about 700 KB/s, so a 240 x 240 frame takes about 85
ms whatever the filter. A change made while a frame is still coming is not sent separately: when the frame is complete the
latest settings are sent, so dragging a slider always ends on the final value and the board never falls behind.

This is a stand-in for the display that will be on the board (HDMI): only `Display` knows about the window, and `ViewerCore`
(no GUI) is everything else. Needs PySide6 (Qt 6); tests/test_viewer.py runs both parts without a display.
"""
import argparse, math, os, sys, threading, time
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
import pico_filter as pf
import pico_resident as pr
import gauss_weights as gw

FILTERS = ["gauss", "identity", "blur", "gaussian", "sharpen", "edge", "emboss", "softemboss", "sobel", "sobelx", "sobely"]


@dataclass(frozen=True)
class Params:
    """Everything that decides a frame. `kind` is "gauss" (the separable Gaussian: sigma in pixels, radius ceil(3 sigma) unless
    given) or the name of a 3x3 filter (passes None = the filter's default)."""
    kind: str = "gauss"
    sigma: float = 1.0
    radius: int = None
    passes: int = None
    center: int = 0
    width: int = 1


def auto_radius(sigma):
    return min(gw.MAX_RADIUS, max(1, math.ceil(3 * sigma - 1e-9)))


def apply_args(p):
    """Params -> the keyword arguments of ResidentSession.apply."""
    if p.kind == "gauss":
        r = auto_radius(p.sigma) if p.radius is None else p.radius
        return dict(name=None, passes=p.passes, window=(p.center, p.width), kernel=gw.gaussian(p.sigma, r))
    return dict(name=p.kind, passes=p.passes, window=(p.center, p.width), kernel=None)


class ViewerCore:
    """Owns the connection and one worker thread. request() may be called from any thread as often as you like: only the latest
    request that arrives while a frame is in flight is kept (the frames in between are skipped). The callbacks are called from
    the worker thread:
        on_loaded(w, h, maxval)                 after the upload
        on_row(frame_no, index, row_bytes)      for every row of every frame as it arrives
        on_done(frame_no, info, seconds)        a frame is complete (info: the device's final response)
        on_error(text)                          a request failed; the core keeps running, except when the link is gone
    """

    def __init__(self, link, w, h, maxval, samples, on_loaded=None, on_row=None, on_done=None, on_error=None):
        self.session = pr.ResidentSession(link)
        self.link, self.w, self.h, self.maxval, self.samples = link, w, h, maxval, samples
        self.on_loaded, self.on_row, self.on_done, self.on_error = on_loaded, on_row, on_done, on_error
        self._cv = threading.Condition()
        self._pending, self._stop, self._busy = None, False, False
        self.frames = self.skipped = 0
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def request(self, params):
        with self._cv:
            if self._pending is not None:
                self.skipped += 1
            self._pending = params
            self._cv.notify()

    def idle(self):
        with self._cv:
            return self._pending is None and not self._busy

    def close(self):
        with self._cv:
            self._stop = True
            self._cv.notify()
        self._thread.join(timeout=pf.TIMEOUT + 5)

    def _run(self):
        try:
            self.session.load(self.w, self.h, self.maxval, self.samples)
        except Exception as e:                           # noqa: BLE001 - reported to the window
            self._error("could not upload the image: %s" % e)
            return
        if self.on_loaded:
            self.on_loaded(self.w, self.h, self.maxval)
        while True:
            with self._cv:
                while self._pending is None and not self._stop:
                    self._cv.wait()
                if self._stop:
                    return
                params, self._pending, self._busy = self._pending, None, True
            self.frames += 1
            n = self.frames
            t0 = time.monotonic()
            try:
                kw = apply_args(params)
                info, _ = self.session.apply(kw["name"], kw["passes"], kw["window"], kw["kernel"],
                                             on_row=(lambda i, row, n=n: self.on_row(n, i, row)) if self.on_row else None)
            except (pf.ProtoError, ValueError) as e:      # the board refused, or the request is wrong: carry on
                self._error(str(e))
                continue
            except (TimeoutError, EOFError, OSError, RuntimeError) as e:      # the link is gone or out of step
                self._error("lost the board: %s" % e)
                return
            finally:
                with self._cv:
                    self._busy = False
            if self.on_done:
                self.on_done(n, info, time.monotonic() - t0)

    def _error(self, text):
        if self.on_error:
            self.on_error(text)


def percentile_window(samples, maxval, lo=0.01, hi=0.995):
    """A window (centre, width) that shows the samples between the lo and hi quantiles: a good first view of an MR slice."""
    s = sorted(samples)
    a, b = s[int(lo * (len(s) - 1))], s[int(hi * (len(s) - 1))]
    return (a + b) // 2, max(1, min(65535, b - a))


def open_link(a):
    if a.exec:
        return pf.PipeLink(a.exec), f"NOT hardware: {a.exec}"
    port = a.port or pf.autodetect_port()
    return pf.SerialLink(port), f"the board at {pf.describe_device(port)}"


# ---------------------------------------------------------------------------------------------------------- the window
def make_app_classes():
    """The Qt classes, built on demand so that importing this module for the core alone needs no Qt."""
    from PySide6 import QtCore, QtGui, QtWidgets

    class Bridge(QtCore.QObject):
        """Carries the worker thread's callbacks into the GUI thread (queued signals)."""
        loaded = QtCore.Signal(int, int, int)
        row = QtCore.Signal(int, int, bytes)
        done = QtCore.Signal(int, dict, float)
        error = QtCore.Signal(str)

    class Display(QtWidgets.QWidget):
        """The picture: an 8-bit gray image that rows are copied into as they arrive. Replace this class (and nothing else)
        to send the frames to another kind of display."""

        def __init__(self, w, h, zoom):
            super().__init__()
            self.w, self.h, self.zoom = w, h, zoom
            self.buf = bytearray(w * h)
            self.img = QtGui.QImage(self.buf, w, h, w, QtGui.QImage.Format_Grayscale8)
            self.rows_seen = 0
            self.dirty = False
            self.paints = 0
            self.setFixedSize(w * zoom, h * zoom)
            self.timer = QtCore.QTimer(self)
            self.timer.timeout.connect(self._tick)
            self.timer.start(16)

        def put_row(self, index, row):
            if 0 <= index < self.h and len(row) == self.w:
                self.buf[index * self.w:(index + 1) * self.w] = row
                self.rows_seen += 1
                self.dirty = True

        def _tick(self):
            if self.dirty:
                self.dirty = False
                self.update()

        def paintEvent(self, _):
            self.paints += 1
            p = QtGui.QPainter(self)
            p.drawImage(QtCore.QRect(0, 0, self.w * self.zoom, self.h * self.zoom), self.img)

    class Viewer(QtWidgets.QWidget):
        def __init__(self, core_factory, w, h, maxval, samples, where, first):
            super().__init__()
            self.maxval, self.where = maxval, where
            self.setWindowTitle(f"{w}x{h} on {where}")
            self.bridge = Bridge()
            self.display = Display(w, h, self._zoom_for(w, h))
            self.status = QtWidgets.QLabel("uploading the image ...")
            self.status.setMinimumWidth(360)
            self.done_times = []
            self.frame_count = 0
            self.last_error = ""

            form = QtWidgets.QFormLayout()
            self.kind = QtWidgets.QComboBox()
            self.kind.addItems(FILTERS)
            self.kind.setCurrentText(first.kind)
            form.addRow("filter", self.kind)

            self.sigma = QtWidgets.QSlider(QtCore.Qt.Horizontal)
            self.sigma.setRange(3, 80)                    # tenths of a pixel
            self.sigma.setValue(int(round(first.sigma * 10)))
            self.sigma_label = QtWidgets.QLabel()
            row = QtWidgets.QHBoxLayout()
            row.addWidget(self.sigma, 1)
            row.addWidget(self.sigma_label)
            self.sigma_row = row
            self.sigma_caption = QtWidgets.QLabel("sigma (pixels)")
            form.addRow(self.sigma_caption, row)

            self.passes = QtWidgets.QSpinBox()
            self.passes.setRange(0, 255)
            self.passes.setSpecialValueText("default")
            self.passes.setValue(0 if first.passes is None else first.passes)
            form.addRow("passes", self.passes)

            self.center = QtWidgets.QSlider(QtCore.Qt.Horizontal)
            self.center.setRange(0, maxval)
            self.center.setValue(first.center)
            self.width = QtWidgets.QSlider(QtCore.Qt.Horizontal)
            self.width.setRange(1, min(maxval + 1, 65535))
            self.width.setValue(first.width)
            self.center_label, self.width_label = QtWidgets.QLabel(), QtWidgets.QLabel()
            for caption, slider, label in (("window centre", self.center, self.center_label),
                                           ("window width", self.width, self.width_label)):
                r = QtWidgets.QHBoxLayout()
                r.addWidget(slider, 1)
                r.addWidget(label)
                form.addRow(caption, r)
            auto = QtWidgets.QPushButton("auto window (1% to 99.5%)")
            form.addRow("", auto)

            side = QtWidgets.QVBoxLayout()
            side.addLayout(form)
            side.addWidget(self.status)
            side.addStretch(1)
            outer = QtWidgets.QHBoxLayout(self)
            outer.addWidget(self.display)
            outer.addLayout(side)

            self.samples = samples
            self.auto_window = lambda: percentile_window(samples, maxval)
            auto.clicked.connect(self._auto)
            for sig in (self.kind.currentTextChanged, self.sigma.valueChanged, self.passes.valueChanged,
                        self.center.valueChanged, self.width.valueChanged):
                sig.connect(self._changed)

            b = self.bridge
            b.loaded.connect(self._loaded)
            b.row.connect(self._row)
            b.done.connect(self._done)
            b.error.connect(self._error)
            self.core = core_factory(
                on_loaded=b.loaded.emit, on_row=lambda n, i, row: b.row.emit(n, i, bytes(row)),
                on_done=lambda n, info, dt: b.done.emit(n, dict(info), dt), on_error=b.error.emit)
            self._refresh_labels()

        @staticmethod
        def _zoom_for(w, h):
            return max(1, min(4, 720 // max(w, h)))

        def params(self):
            kind = self.kind.currentText()
            return Params(kind=kind, sigma=self.sigma.value() / 10.0, radius=None,
                          passes=(self.passes.value() or None), center=self.center.value(), width=self.width.value())

        def _refresh_labels(self):
            p = self.params()
            gauss = p.kind == "gauss"
            self.sigma.setEnabled(gauss)
            self.sigma_caption.setEnabled(gauss)
            self.sigma_label.setText(f"{p.sigma:.1f}  (radius {auto_radius(p.sigma)})" if gauss else "")
            self.center_label.setText(str(p.center))
            self.width_label.setText(str(p.width))

        def _changed(self, *_):
            self._refresh_labels()
            self.core.request(self.params())

        def _auto(self):
            c, w = self.auto_window()
            self.center.blockSignals(True)
            self.width.blockSignals(True)
            self.center.setValue(c)
            self.width.setValue(w)
            self.center.blockSignals(False)
            self.width.blockSignals(False)
            self._changed()

        def _loaded(self, w, h, maxval):
            self.status.setText("image uploaded; asking for the first frame ...")
            self.core.request(self.params())

        def _row(self, n, i, row):
            self.display.put_row(i, row)

        def _done(self, n, info, dt):
            self.frame_count = n
            now = time.monotonic()
            self.done_times = [t for t in self.done_times if now - t < 3.0] + [now]
            fps = (len(self.done_times) - 1) / (now - self.done_times[0]) if len(self.done_times) > 1 else 0.0
            cyc = (f"; core {info['cycles'] / 1e6:.2f} M cycles ({info['cycles'] / (self.display.w * self.display.h):.0f}/pixel)"
                   if info["cycles"] else "")
            self.status.setText(f"frame {n}: {dt * 1000:.0f} ms round trip{cyc}\n"
                                f"{fps:.1f} frames/s, {self.core.skipped} requests merged into the next frame" +
                                (f"\n{self.last_error}" if self.last_error else ""))

        def _error(self, text):
            self.last_error = text
            self.status.setText(text)

        def closeEvent(self, ev):
            self.core.close()
            self.link_close()
            super().closeEvent(ev)

        def link_close(self):
            try:
                self.core.link.close()
            except Exception:                             # noqa: BLE001
                pass

    return Viewer


def build_viewer(link, where, w, h, maxval, samples, first):
    Viewer = make_app_classes()

    def factory(**cb):
        return ViewerCore(link, w, h, maxval, samples, **cb)
    v = Viewer(factory, w, h, maxval, samples, where, first)
    v.core.start()
    return v


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("--port")
    ap.add_argument("--exec")
    ap.add_argument("--filter", default="gauss", choices=FILTERS)
    ap.add_argument("--sigma", type=float, default=1.0)
    ap.add_argument("--window", nargs=2, type=int, metavar=("CENTER", "WIDTH"))
    ap.add_argument("--zoom", type=int)
    ap.add_argument("--timeout", type=float, default=pf.TIMEOUT)
    a = ap.parse_args()
    pf.TIMEOUT = a.timeout
    w, h, maxval, samples = pf.read_pgm(a.input)
    if not 0.3 <= a.sigma <= 8.0:
        sys.exit("the sigma must be between 0.3 and 8")
    c, ww = a.window if a.window else percentile_window(samples, maxval)
    first = Params(kind=a.filter, sigma=a.sigma, center=c, width=max(1, min(ww, 65535)))
    try:
        from PySide6 import QtWidgets
    except ImportError:
        sys.exit("this needs PySide6 (Qt 6): pacman -S pyside6")
    app = QtWidgets.QApplication(sys.argv[:1])
    link, where = open_link(a)
    viewer = build_viewer(link, where, w, h, maxval, samples, first)
    if a.zoom:
        viewer.display.zoom = a.zoom
        viewer.display.setFixedSize(w * a.zoom, h * a.zoom)
    viewer.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
