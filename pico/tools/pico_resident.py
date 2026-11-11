#!/usr/bin/env python3
"""Resident-image client: upload an image to the board ONCE, then change the filter or the window and get each result
without sending the image again (protocol "RES1", pico/src/resident.h).

    pico_resident.py IMAGE.pgm [--port P | --exec CMD] [--window CENTER WIDTH] [--out-prefix P] [--repeat N] STEP [STEP ...]

A STEP is one of
    NAME            a 3x3 filter with its default passes   (blur, gaussian, sobel, ...)
    NAME:PASSES     the same with a chosen number of passes
    gauss:SIGMA     the separable Gaussian, radius ceil(3 sigma)    (sigma in pixels)
    gauss:SIGMA:R   ... with radius R (0..15)
    get             send the current front frame again without computing (a pure download, to time the USB link; put it
                    after a filter step)

Every step starts from the ORIGINAL image on the board and produces an 8-bit display frame (the window/level default is
the whole range). With --out-prefix the frames are written as PREFIX_<n>.pgm. The time printed per step is the round trip
(request to the last row), with the number of cycles the core spent filtering. As a library, ResidentSession is what a
viewer would use:

    s = ResidentSession(link); s.load(w, h, maxval, samples)
    info, frame = s.apply("blur", window=(1000, 800))      # frame: w*h bytes, row-major
"""
import argparse, math, os, struct, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pico_filter as pf

RES_MAGIC = b"RES1"
RES_LOAD, RES_APPLY, RES_GET = 1, 2, 3
F_SEND, F_SEP, F_RAW16 = 1, 2, 4
HDR_FMT = "<4s9I16s2I"          # magic, cmd, w, h, maxval, flags, passes, center, width, reserved, name, 2 reserved
HDR_SIZE = struct.calcsize(HDR_FMT)
assert HDR_SIZE == 64
pf.STATUS.update({11: "no resident image (never loaded, or another request used the device memory), or no frame yet",
                  12: "bad resident-image command (unknown command, or a field that must be 0 is not)"})


def header(cmd, w=0, h=0, maxval=0, flags=0, passes=pf.PASSES_DEFAULT, center=0, width=0, name=""):
    return struct.pack(HDR_FMT, RES_MAGIC, cmd, w, h, maxval, flags, passes, center, width, 0, name.encode()[:16], 0, 0)


class ResidentSession:
    """One connection to a board that holds (at most) one resident image."""

    def __init__(self, link):
        self.link = link
        self.w = self.h = self.maxval = 0

    def _ack(self, where):
        r = pf.read_response(self.link, where)
        if r["status"] != 0:
            raise pf.ProtoError(r["status"], where)
        return r

    def _rows(self, ack, on_row=None):
        """The ROW1 records that follow an acknowledgement, then the final response. Returns (final, rows). on_row(index, row)
        is called for every row as soon as it has arrived (before the next one is read)."""
        rows = []
        keys = ("status", "w", "h", "bps", "maxval", "cycles", "instret", "payload", "reserved")
        while True:
            head = self.link.read_exact(4)
            if head == pf.ROW_MAGIC:
                (idx,) = struct.unpack("<I", self.link.read_exact(4))
                if idx != len(rows):
                    raise RuntimeError("device sent row %d, expected %d" % (idx, len(rows)))
                rows.append(self.link.read_exact(ack["payload"]))
                if on_row:
                    on_row(idx, rows[-1])
            elif head == pf.RESP_MAGIC:
                res = dict(zip(keys, struct.unpack("<9I", self.link.read_exact(pf.RESP_SIZE - 4))))
                break
            else:
                raise RuntimeError("unexpected bytes from the device: %r" % head)
        if res["status"] != 0:
            raise pf.ProtoError(res["status"], "resident")
        return res, rows

    def load(self, w, h, maxval, samples):
        """Upload the image; it replaces any resident image. Returns the final response."""
        self.w = self.h = self.maxval = 0
        self.link.write(header(RES_LOAD, w, h, maxval))
        self._ack("load header")
        self.link.write(pf.to_le_bytes(samples))
        res = pf.read_response(self.link, "load samples")
        if res["status"] != 0:
            raise pf.ProtoError(res["status"], "load samples")
        self.w, self.h, self.maxval = w, h, maxval
        return res

    def apply(self, name=None, passes=None, window=None, kernel=None, send=True, raw16=False, on_row=None):
        """Filter the resident image into the back display frame (or, with raw16, into 16-bit rows) and swap. Returns
        (final response, data): data is the frame as w*h bytes (raw16: w*h*2 bytes), or None when send is False. on_row(index,
        row) is called as each row arrives, for drawing while the frame is still coming."""
        flags = (F_SEND if send else 0) | (F_SEP if kernel is not None else 0) | (F_RAW16 if raw16 else 0)
        win = window or (0, 0)
        if not raw16 and window is None:
            win = (self.maxval // 2, min(self.maxval + 1, 65535))   # the whole range (the device takes widths up to 65535)
        self.link.write(header(RES_APPLY, flags=flags, passes=pf.PASSES_DEFAULT if passes is None else passes,
                               center=win[0], width=win[1], name=name or "")
                        + (pf.kernel_bytes(kernel) if kernel is not None else b""))
        ack = self._ack("apply")
        res, rows = self._rows(ack, on_row)
        if len(rows) != (self.h if send else 0):
            raise RuntimeError("device returned %d rows" % len(rows))
        return res, (b"".join(rows) if send else None)

    def get(self, on_row=None):
        """The front display frame again, without recomputing. Returns (final response, w*h bytes)."""
        self.link.write(header(RES_GET))
        ack = self._ack("get")
        res, rows = self._rows(ack, on_row)
        if len(rows) != self.h:
            raise RuntimeError("device returned %d rows" % len(rows))
        return res, b"".join(rows)


def parse_step(text):
    """-> (name or None, passes or None, kernel or None)"""
    parts = text.split(":")
    if text == "get":
        return "get", None, None
    if parts[0] == "gauss":
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
        import gauss_weights
        if len(parts) not in (2, 3):
            sys.exit("a gauss step is gauss:SIGMA or gauss:SIGMA:RADIUS")
        sigma = float(parts[1])
        if not sigma > 0:
            sys.exit("the sigma must be positive")
        radius = int(parts[2]) if len(parts) == 3 else min(gauss_weights.MAX_RADIUS, max(1, math.ceil(3 * sigma)))
        if not 0 <= radius <= gauss_weights.MAX_RADIUS:
            sys.exit("the radius must be 0..%d" % gauss_weights.MAX_RADIUS)
        return None, None, gauss_weights.gaussian(sigma, radius)
    if len(parts) > 2:
        sys.exit("a step is NAME or NAME:PASSES")
    return parts[0], (int(parts[1]) if len(parts) == 2 else None), None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("steps", nargs="+", metavar="STEP")
    ap.add_argument("--window", nargs=2, type=int, metavar=("CENTER", "WIDTH"))
    ap.add_argument("--port")
    ap.add_argument("--exec")
    ap.add_argument("--timeout", type=float, default=pf.TIMEOUT)
    ap.add_argument("--out-prefix", metavar="P", help="write each frame as P_<n>.pgm")
    ap.add_argument("--repeat", type=int, default=1, metavar="N", help="run the list of steps N times (for timing)")
    a = ap.parse_args()
    pf.TIMEOUT = a.timeout
    steps = [parse_step(s) for s in a.steps]
    w, h, maxval, samples = pf.read_pgm(a.input)

    if a.exec:
        link = pf.PipeLink(a.exec)
        where = f"NOT hardware: a local process ({a.exec}) standing in for the board."
    else:
        port = a.port or pf.autodetect_port()
        link = pf.SerialLink(port)
        where = f"the board at {pf.describe_device(port)}"
    try:
        s = ResidentSession(link)
        t0 = time.monotonic()
        s.load(w, h, maxval, samples)
        print(f"loaded {w}x{h} (maxval {maxval}) on {where}: {time.monotonic() - t0:.3f} s upload, once")
        n = 0
        for _ in range(a.repeat):
            for text, (name, passes, kernel) in zip(a.steps, steps):
                t0 = time.monotonic()
                res, frame = s.get() if name == "get" else s.apply(name, passes, a.window, kernel)
                dt = time.monotonic() - t0
                n += 1
                cyc = f", {res['cycles']} cycles on the core ({res['cycles'] / (w * h):.1f}/pixel)" if res["cycles"] else ""
                rate = f", {len(frame) / dt / 1000:.0f} KB/s" if name == "get" else ""
                print(f"step {n:3d} {text:<14} {dt * 1000:7.1f} ms round trip{cyc}{rate}; frame {res['reserved']}")
                if a.out_prefix:
                    with open(f"{a.out_prefix}_{n}.pgm", "wb") as f:
                        f.write(pf.pgm_bytes(w, h, 255, frame))
    finally:
        link.close()


if __name__ == "__main__":
    main()
