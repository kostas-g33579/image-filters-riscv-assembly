#!/usr/bin/env python3
"""Host client for the Pico image-filter firmware (pico/).

Sends a PGM to the device over USB serial, has the RISC-V core filter it, and
writes the result as a PGM. Pure standard library (no pyserial).

    pico_filter.py IN.pgm OUT.pgm [FILTER] [PASSES] [--window C W]
                   [--port /dev/ttyACM1 | --exec "qemu-riscv32 build32/proto_test"]
                   [--verify]

--stream sends the image row by row (the device then never holds it whole, so any height
works and only passes*3*width*2 bytes of device memory are needed); it is chosen
automatically when the image is too big for whole-image mode (--no-stream forces
whole-image). --verify re-runs the same job through the Linux test harness
(build32/filter under qemu-riscv32) and checks the device's output is
byte-identical. --exec runs the protocol layer as a local process instead of a
serial device, which is how tests/test_proto.py tests it without hardware.

Wire format: pico/README.md.
"""
import argparse, array, glob, os, re, select, shlex, struct, subprocess, sys, tempfile, termios, threading, time, tty

REQ_MAGIC, STR_MAGIC, RESP_MAGIC, ROW_MAGIC = b"IMG1", b"STR1", b"OUT1", b"ROW1"
REQ_FMT = "<4s16s7I"       # magic, name, W, H, maxval, passes, flags, center, width
RESP_FMT = "<4s9I"         # magic, status, W, H, bps, maxval, cycles, instret, payload, reserved
RESP_SIZE = struct.calcsize(RESP_FMT)
PASSES_DEFAULT = 0xFFFFFFFF
TIMEOUT = 30.0     # seconds to wait for the device (--timeout; raise it while debugging at a breakpoint)
STATUS = {0: "ok", 1: "bad magic", 2: "unknown filter", 3: "bad or too-large dimensions",
          4: "unsupported maxval", 5: "bad pass count", 6: "bad window", 7: "sample exceeds maxval",
          8: "timeout (the device stopped receiving)",
          9: "not enough device memory for this width x passes (streaming needs passes*3*width*2 bytes)"}


class ProtoError(Exception):
    def __init__(self, status, where):
        self.status = status
        super().__init__(f"device rejected the request ({where}): status {status} = {STATUS.get(status, '?')}")


# ---------------- transports ----------------
class Link:
    write_slice = 4096        # bytes per write(2); SerialLink overrides it (see below)

    def read_exact(self, n, timeout=None):
        timeout = TIMEOUT if timeout is None else timeout
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

    def write(self, data, timeout=None):
        """Write everything, but never block forever: a device that stops reading
        (or answers early and stalls) must raise, not hang the host."""
        timeout = TIMEOUT if timeout is None else timeout
        view = memoryview(data)
        end = time.monotonic() + timeout
        while view:
            left = end - time.monotonic()
            if left <= 0: raise TimeoutError(f"timed out writing ({len(view)} bytes unsent)")
            _, w, _ = select.select([], [self.wfd], [], left)
            if not w: continue
            try:
                n = os.write(self.wfd, view[:self.write_slice])      # small chunks: never blocks once writable
            except BlockingIOError:
                continue
            view = view[n:]


class SerialLink(Link):
    # Measured on the chip (pico/tools/usb_bench.py, 4 MB, Linux cdc-acm): the upload runs at
    # ~770 KB/s when the host writes 64 bytes (one USB packet) per write(2), but only ~200 KB/s
    # with 4096-byte writes, whatever size the device reads. So write one packet at a time.
    write_slice = 64

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


def describe_device(port, by_id_dir="/dev/serial/by-id"):
    """A human-readable name for a serial port: its stable by-id name if there is one."""
    real = os.path.realpath(port)             # e.g. /dev/ttyACM1, even if given a by-id path
    for p in sorted(glob.glob(os.path.join(by_id_dir, "*"))):
        if os.path.realpath(p) == real:
            return f"{real} ({os.path.basename(p)})"
    return real


def autodetect_port(by_id_dir="/dev/serial/by-id"):
    """The one serial port that looks like our board. A Raspberry Pi debug probe
    (Debugprobe / Picoprobe) also has "Pico" in its name and is a UART bridge to
    the target's pins, not the target's own USB serial, so it is never chosen."""
    def not_a_probe(path):
        return not any(w in os.path.basename(path).lower() for w in ("debugprobe", "picoprobe", "cmsis-dap"))
    found = [p for p in sorted(glob.glob(os.path.join(by_id_dir, "*Pico*"))) if not_a_probe(p)]
    # No fallback to "any serial device": that could be a fingerprint reader or a modem.
    if len(found) == 1: return found[0]
    sys.exit("could not pick a serial port automatically (%s); pass --port" % (found or "none found"))


# ---------------- protocol ----------------
def request_header(name, w, h, maxval, passes=None, window=None, magic=REQ_MAGIC):
    flags, center, width = (1, window[0], window[1]) if window else (0, 0, 0)
    return struct.pack(REQ_FMT, magic, name.encode()[:16], w, h, maxval,
                       PASSES_DEFAULT if passes is None else passes, flags, center, width)


def read_response(link, where, timeout=None):
    """Read a response header, skipping any stray bytes before its magic."""
    timeout = TIMEOUT if timeout is None else timeout
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


def process_stream(link, name, w, h, maxval, samples, passes=None, window=None):
    """Run one filter job in STREAMING mode: the image goes to the device row by row and the
    filtered rows come back as soon as they exist, so the device never holds the whole image.
    Returns the same (info dict, output) as process(). Sending and receiving run at the same
    time (a thread sends); doing them one after the other would deadlock once the data
    outgrows the pipe / USB buffers."""
    link.write(request_header(name, w, h, maxval, passes, window, magic=STR_MAGIC))
    ack = read_response(link, "header")
    if ack["status"] != 0: raise ProtoError(ack["status"], "header")
    row_bytes, bps = ack["payload"], ack["bps"]

    send_error = []

    def sender():
        try:
            for y in range(h):          # always send every row: after an error the device reads
                link.write(to_le_bytes(samples[y * w:(y + 1) * w]))   # and discards the rest to stay in step
        except Exception as e:          # noqa: BLE001 - reported by the main thread
            send_error.append(e)

    t = threading.Thread(target=sender, daemon=True)
    t.start()
    rows, res = [], None
    keys = ("status", "w", "h", "bps", "maxval", "cycles", "instret", "payload", "reserved")
    try:
        while True:
            head = link.read_exact(4)
            if head == ROW_MAGIC:
                (idx,) = struct.unpack("<I", link.read_exact(4))
                if idx != len(rows):
                    raise RuntimeError("device sent row %d, expected %d" % (idx, len(rows)))
                rows.append(link.read_exact(row_bytes))
            elif head == RESP_MAGIC:
                res = dict(zip(keys, struct.unpack("<9I", link.read_exact(RESP_SIZE - 4))))
                break
            else:
                raise RuntimeError("unexpected bytes from the device: %r" % head)
    finally:
        t.join(timeout=TIMEOUT)         # let the sender finish so the connection stays in step
    if res["status"] != 0: raise ProtoError(res["status"], "stream")
    if send_error: raise send_error[0]
    if len(rows) != h: raise RuntimeError("device returned %d of %d rows" % (len(rows), h))
    payload = b"".join(rows)
    if bps == 2:
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


def whole_image_limit():
    """The device's whole-image pixel limit, read from proto.h (so it cannot drift)."""
    try:
        text = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "proto.h")).read()
        return int(re.search(r"#define PROTO_MAX_PIXELS (\d+)u", text).group(1))
    except (OSError, AttributeError):
        return 90000


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input"); ap.add_argument("output")
    ap.add_argument("filter", nargs="?", default="blur")
    ap.add_argument("passes", nargs="?", type=int)
    ap.add_argument("--window", nargs=2, type=int, metavar=("CENTER", "WIDTH"))
    ap.add_argument("--port"); ap.add_argument("--exec")
    ap.add_argument("--timeout", type=float, default=TIMEOUT,
                    help="seconds to wait for the device (default %(default)s; raise it when stopped at a breakpoint)")
    ap.add_argument("--stream", action="store_true",
                    help="send the image row by row (any height; needs passes*3*width*2 bytes of device memory)")
    ap.add_argument("--no-stream", action="store_true", help="force whole-image mode even for big images")
    ap.add_argument("--verify", action="store_true",
                    help="also run the Linux test harness and require identical output")
    a = ap.parse_args()
    globals()["TIMEOUT"] = a.timeout

    w, h, maxval, samples = read_pgm(a.input)
    if a.exec:
        link = PipeLink(a.exec)
        where = (f"NOT hardware: a local process ({a.exec}) standing in for the board. "
                 "This is the automated-test mode.")
    else:
        port = a.port or autodetect_port()
        link = SerialLink(port)
        where = (f"the board at {describe_device(port)}: the filter ran on the RP2350's RISC-V core. "
                 "This PC only sent the pixels and received the result.")
    use_stream = a.stream or (not a.no_stream and w * h > whole_image_limit())
    if use_stream:
        where += "  [streaming: the device held only a few rows at a time]"
    t0 = time.monotonic()
    try:
        run = process_stream if use_stream else process
        info, out = run(link, a.filter, w, h, maxval, samples, a.passes, tuple(a.window) if a.window else None)
    except ProtoError as e:
        sys.exit(str(e))
    dt = time.monotonic() - t0
    link.close()

    result = pgm_bytes(w, h, info["maxval"], out)
    open(a.output, "wb").write(result)
    passes_run = a.passes
    print(f"{a.filter}: {w}x{h}, maxval {maxval} -> wrote {a.output} ({len(result)} bytes, {dt:.2f}s round trip)")
    print(f"filtered on: {where}")
    if info["cycles"]:
        print(f"on the core: {info['cycles']} cycles, {info['instret']} instructions retired "
              f"(IPC {info['instret'] / info['cycles']:.2f}), read from the core's own mcycle/minstret counters")
    if a.verify:
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
        ref = harness_output(a.input, a.filter, a.passes, tuple(a.window) if a.window else None,
                             os.path.join(root, "build32", "filter"))
        if ref == result:
            print("verify: output is byte-identical to the Linux harness. That reference ran on THIS PC "
                  "under qemu-riscv32, as an independent second implementation.")
        else: sys.exit("verify: OUTPUT DIFFERS from the Linux harness")


if __name__ == "__main__":
    main()
