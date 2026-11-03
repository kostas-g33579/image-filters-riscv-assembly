#!/usr/bin/env python3
"""Host client for the Pico image-filter firmware (pico/).

Sends a PGM to the device over USB serial, has the RISC-V core filter it, and
writes the result as a PGM. Pure standard library (no pyserial).

    pico_filter.py IN.pgm OUT.pgm [FILTER] [PASSES] [--window C W]
                   [--port /dev/ttyACM1 | --exec "qemu-riscv32 build32/proto_test"]
                   [--verify]

--verify re-runs the same job through the Linux test harness
(build32/filter under qemu-riscv32) and checks the device's output is
byte-identical. --exec runs the protocol layer as a local process instead of a
serial device, which is how tests/test_proto.py tests it without hardware.

Wire format: pico/README.md.
"""
import argparse, array, glob, os, select, shlex, struct, subprocess, sys, tempfile, termios, time, tty

REQ_MAGIC, RESP_MAGIC = b"IMG1", b"OUT1"
REQ_FMT = "<4s16s7I"       # magic, name, W, H, maxval, passes, flags, center, width
RESP_FMT = "<4s9I"         # magic, status, W, H, bps, maxval, cycles, instret, payload, reserved
RESP_SIZE = struct.calcsize(RESP_FMT)
PASSES_DEFAULT = 0xFFFFFFFF
STATUS = {0: "ok", 1: "bad magic", 2: "unknown filter", 3: "bad or too-large dimensions",
          4: "unsupported maxval", 5: "bad pass count", 6: "bad window", 7: "sample exceeds maxval",
          8: "timeout (the device stopped receiving)"}


class ProtoError(Exception):
    def __init__(self, status, where):
        self.status = status
        super().__init__(f"device rejected the request ({where}): status {status} = {STATUS.get(status, '?')}")


# ---------------- transports ----------------
class Link:
    def read_exact(self, n, timeout=30.0):
        buf = bytearray()
        end = time.monotonic() + timeout
        while len(buf) < n:
            left = end - time.monotonic()
            if left <= 0: raise TimeoutError(f"timed out waiting for {n - len(buf)} more bytes")
            r, _, _ = select.select([self.rfd], [], [], left)
            if not r: continue
            chunk = os.read(self.rfd, n - len(buf))
            if not chunk: raise EOFError("link closed")
            buf += chunk
        return bytes(buf)

    def write(self, data, timeout=30.0):
        """Write everything, but never block forever: a device that stops reading
        (or answers early and stalls) must raise, not hang the host."""
        view = memoryview(data)
        end = time.monotonic() + timeout
        while view:
            left = end - time.monotonic()
            if left <= 0: raise TimeoutError(f"timed out writing ({len(view)} bytes unsent)")
            _, w, _ = select.select([], [self.wfd], [], left)
            if not w: continue
            try:
                n = os.write(self.wfd, view[:4096])      # small chunks: never blocks once writable
            except BlockingIOError:
                continue
            view = view[n:]


class SerialLink(Link):
    def __init__(self, port):
        self.rfd = self.wfd = os.open(port, os.O_RDWR | os.O_NOCTTY)
        tty.setraw(self.rfd)
        attrs = termios.tcgetattr(self.rfd)
        attrs[2] &= ~termios.HUPCL                      # don't drop DTR when we close
        termios.tcsetattr(self.rfd, termios.TCSANOW, attrs)
        termios.tcflush(self.rfd, termios.TCIOFLUSH)

    def close(self): os.close(self.rfd)


class PipeLink(Link):
    def __init__(self, cmd):
        self.proc = subprocess.Popen(shlex.split(cmd), stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.rfd, self.wfd = self.proc.stdout.fileno(), self.proc.stdin.fileno()

    def close_stdin(self):
        self.proc.stdin.close()

    def close(self):
        try: self.proc.stdin.close()
        except Exception: pass
        try: self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:         # a stuck device must not stall the host
            self.proc.kill(); self.proc.wait()


def autodetect_port():
    found = sorted(glob.glob("/dev/serial/by-id/*Pico*")) or sorted(glob.glob("/dev/serial/by-id/*"))
    if len(found) == 1: return found[0]
    sys.exit("could not pick a serial port automatically (%s); pass --port" % (found or "none found"))


# ---------------- protocol ----------------
def request_header(name, w, h, maxval, passes=None, window=None):
    flags, center, width = (1, window[0], window[1]) if window else (0, 0, 0)
    return struct.pack(REQ_FMT, REQ_MAGIC, name.encode()[:16], w, h, maxval,
                       PASSES_DEFAULT if passes is None else passes, flags, center, width)


def read_response(link, where, timeout=30.0):
    """Read a response header, skipping any stray bytes before its magic."""
    window = b""
    end = time.monotonic() + timeout
    while window != RESP_MAGIC:
        window = (window + link.read_exact(1, max(0.1, end - time.monotonic())))[-4:]
    fields = struct.unpack("<9I", link.read_exact(RESP_SIZE - 4, timeout))
    keys = ("status", "w", "h", "bps", "maxval", "cycles", "instret", "payload", "reserved")
    r = dict(zip(keys, fields))
    return r


def to_le_bytes(samples):
    a = array.array("H", samples)
    if sys.byteorder == "big": a.byteswap()
    return a.tobytes()


def process(link, name, w, h, maxval, samples, passes=None, window=None):
    """Run one filter job on the device. Returns (info dict, output samples as
    array('H') or bytes). Raises ProtoError if the device rejects the request."""
    link.write(request_header(name, w, h, maxval, passes, window))
    ack = read_response(link, "header")
    if ack["status"] != 0: raise ProtoError(ack["status"], "header")
    link.write(to_le_bytes(samples))
    res = read_response(link, "samples")
    if res["status"] != 0: raise ProtoError(res["status"], "samples")
    payload = link.read_exact(res["payload"])
    if res["bps"] == 2:
        out = array.array("H"); out.frombytes(payload)
        if sys.byteorder == "big": out.byteswap()
    else:
        out = payload
    return res, out


# ---------------- PGM ----------------
def read_pgm(path):
    data = open(path, "rb").read()
    pos, toks = 2, []
    if data[:2] != b"P5": sys.exit(f"{path}: not a binary PGM (P5)")
    while len(toks) < 3:
        while data[pos:pos + 1].isspace(): pos += 1
        if data[pos:pos + 1] == b"#":
            while data[pos:pos + 1] != b"\n": pos += 1
            continue
        start = pos
        while data[pos:pos + 1].isdigit(): pos += 1
        toks.append(int(data[start:pos]))
    w, h, maxval = toks
    body = data[pos + 1:]                 # exactly one whitespace byte after maxval
    if maxval < 256:
        samples = array.array("H", list(body[:w * h]))      # one byte per sample -> halfwords
    else:
        samples = array.array("H")
        samples.frombytes(body[:2 * w * h])
        if sys.byteorder == "little": samples.byteswap()
    if len(samples) != w * h: sys.exit(f"{path}: pixel data is truncated")
    return w, h, maxval, samples


def pgm_bytes(w, h, maxval, samples):
    head = f"P5\n{w} {h}\n{maxval}\n".encode()
    if maxval < 256: return head + bytes(array.array("B", samples))
    a = array.array("H", samples)
    if sys.byteorder == "little": a.byteswap()
    return head + a.tobytes()


def harness_output(in_path, name, passes, window, filter_bin):
    """The verified Linux harness (build32/filter under qemu-riscv32) on the same job."""
    with tempfile.TemporaryDirectory() as d:
        os.mkdir(os.path.join(d, "images"))
        with open(in_path, "rb") as src, open(os.path.join(d, "images", "input.pgm"), "wb") as dst:
            dst.write(src.read())
        args = ["qemu-riscv32", os.path.abspath(filter_bin), name]
        if passes is not None: args.append(str(passes))
        if window: args += ["window", str(window[0]), str(window[1])]
        subprocess.run(args, cwd=d, check=True, capture_output=True)
        return open(os.path.join(d, "images", "output.pgm"), "rb").read()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input"); ap.add_argument("output")
    ap.add_argument("filter", nargs="?", default="blur")
    ap.add_argument("passes", nargs="?", type=int)
    ap.add_argument("--window", nargs=2, type=int, metavar=("CENTER", "WIDTH"))
    ap.add_argument("--port"); ap.add_argument("--exec")
    ap.add_argument("--verify", action="store_true",
                    help="also run the Linux test harness and require identical output")
    a = ap.parse_args()

    w, h, maxval, samples = read_pgm(a.input)
    link = PipeLink(a.exec) if a.exec else SerialLink(a.port or autodetect_port())
    t0 = time.monotonic()
    try:
        info, out = process(link, a.filter, w, h, maxval, samples, a.passes, tuple(a.window) if a.window else None)
    except ProtoError as e:
        sys.exit(str(e))
    dt = time.monotonic() - t0
    link.close()

    result = pgm_bytes(w, h, info["maxval"], out)
    open(a.output, "wb").write(result)
    passes_run = a.passes
    print(f"{a.filter}: {w}x{h}, maxval {maxval} -> wrote {a.output} ({len(result)} bytes, {dt:.2f}s round trip)")
    if info["cycles"]:
        print(f"on the core: {info['cycles']} cycles, {info['instret']} instructions retired "
              f"(IPC {info['instret'] / info['cycles']:.2f})")
    if a.verify:
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
        ref = harness_output(a.input, a.filter, a.passes, tuple(a.window) if a.window else None,
                             os.path.join(root, "build32", "filter"))
        if ref == result: print("verify: output is byte-identical to the Linux harness (qemu-riscv32)")
        else: sys.exit("verify: OUTPUT DIFFERS from the Linux harness")


if __name__ == "__main__":
    main()
