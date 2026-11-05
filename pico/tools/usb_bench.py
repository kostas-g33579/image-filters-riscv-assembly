#!/usr/bin/env python3
"""USB throughput benchmark client (device side: pico/bench/).

usage: usb_bench.py [--port DEV | --cmd "qemu-riscv32 build32/bench_test"]
                    [--mb 4] [--chunk 4096,16384] [--modes sink,source,echo] [--flush both]

For every mode x chunk size it sends a "BNCH" command, moves the data, checks every byte
against the pattern byte[i] = i*7 mod 256 and prints two throughputs: the HOST's
(wall clock around the whole exchange) and the DEVICE's (its own clock, first byte to
last). They should agree; a big gap means time went into setup or the reply.

  sink    host -> device       source  device -> host       echo  both ways at once
"""
import importlib.util, os, struct, sys, threading, time, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("pico_filter", os.path.join(HERE, "pico_filter.py"))
pf = importlib.util.module_from_spec(spec); spec.loader.exec_module(pf)

MODES = {"sink": 1, "source": 2, "echo": 3}
DONE_FMT = "<8I"        # DONE, status, bytes, elapsed_us, errors, variant, rx_fifo, tx_fifo


def pattern(n, start=0):
    """bytes start..start+n-1 of the stream: byte[i] = (i*7) & 255"""
    period = bytes((i * 7) & 255 for i in range(256))
    reps = (start % 256 + n) // 256 + 2
    off = start % 256
    return (period * reps)[off:off + n]


def diagnose(first, tail, total):
    """Describe a reply that is not a DONE message. `first` = the 32 bytes read where DONE was
    expected, `tail` = everything that arrived afterwards. Pure function (tested)."""
    got = first + tail
    i = got.find(b"DONE")
    out = [f"expected DONE after {total} payload bytes; got {len(got)} more bytes: {first[:16].hex(' ')} ..."]
    if got[:32] == pattern(32, total):
        out.append("these are the pattern CONTINUING from byte %d: the device sent more than the %d bytes asked for" % (total, total))
    elif got[:32] == pattern(32, total - 32):
        out.append("these repeat the last 32 payload bytes: data was duplicated")
    if i >= 0:
        out.append(f"DONE found {i} bytes later: {i} extra bytes were sent before it")
        ex = got[:i]
        out.append("extra bytes are pattern-continuing: %s" % (ex == pattern(i, total)))
    else:
        out.append("no DONE message seen in the %d bytes that followed" % len(tail))
    return "; ".join(out)


def drain(link, quiet=1.0):
    """Read whatever arrives until the line has been silent for `quiet` seconds."""
    import select
    buf = b""
    while True:
        r, _, _ = select.select([link.rfd], [], [], quiet)
        if not r: return buf
        c = os.read(link.rfd, 65536)
        if not c: return buf
        buf += c


def run_one(link, mode, total, chunk, flush, timeout=60.0, hostwrite=0):
    """Returns dict(host_s, bytes, dev_us, errors, status, variant, rx, tx, host_errors)."""
    link.write(struct.pack("<4s4I", b"BNCH", MODES[mode], total, chunk, 1 if flush else 0))
    t0 = time.monotonic()
    send_exc = []

    def sender():
        try:
            sent = 0
            step = hostwrite or chunk          # the host's write size; chunk is the DEVICE's read/write size
            while sent < total:
                n = min(step, total - sent)
                link.write(pattern(n, sent), timeout)
                sent += n
        except Exception as e:                       # reported by the main thread
            send_exc.append(e)

    th = None
    if mode in ("sink", "echo"):
        th = threading.Thread(target=sender, daemon=True); th.start()
    host_errors = 0
    if mode in ("source", "echo"):
        got = 0
        while got < total:
            n = min(65536, total - got)
            data = link.read_exact(n, timeout)
            if data != pattern(n, got):
                host_errors += sum(1 for a, b in zip(data, pattern(n, got)) if a != b)
            got += n
    if th:
        th.join(timeout)
        if send_exc: raise send_exc[0]
    d = link.read_exact(32, timeout)
    host_s = time.monotonic() - t0
    magic, status, nbytes, us, errs, variant, rx, tx = struct.unpack(DONE_FMT, d)
    if magic != 0x454E4F44:
        raise RuntimeError(f"bad DONE magic {magic:#x}: " + diagnose(d, drain(link), total if mode != "sink" else 0))
    return dict(host_s=host_s, bytes=nbytes, dev_us=us, errors=errs, status=status,
                variant=variant, rx=rx, tx=tx, host_errors=host_errors)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port"); ap.add_argument("--cmd")
    ap.add_argument("--mb", type=float, default=4.0, help="megabytes per test (default 4)")
    ap.add_argument("--chunk", default="64,512,4096,16384", help="write sizes in bytes, comma separated")
    ap.add_argument("--modes", default="sink,source,echo")
    ap.add_argument("--flush", choices=["yes", "no", "both"], default="both",
                    help="flush after every write (the filter firmware does)")
    ap.add_argument("--hostwrite", type=int, default=0,
                    help="bytes per host write(2) in sink/echo (default: same as --chunk). Separates the "
                         "host's write size from the device's read size")
    ap.add_argument("--repeat", type=int, default=1)
    a = ap.parse_args()
    if a.cmd: link = pf.PipeLink(a.cmd)
    else:
        port = a.port or pf.autodetect_port()
        if not port: sys.exit("no Pico found; pass --port")
        print("device:", pf.describe_device(port)); link = pf.SerialLink(port)
    total = int(a.mb * 1e6) // 4096 * 4096 or 4096
    flushes = {"yes": [True], "no": [False], "both": [True, False]}[a.flush]
    print(f"hostwrite={a.hostwrite or 'same as chunk'}")
    print(f"{'mode':7s} {'chunk':>6s} {'flush':>5s} | {'host KB/s':>10s} {'device KB/s':>11s} | errors  (variant rx/tx fifo)")
    bad = 0
    for mode in a.modes.split(","):
        for chunk in (int(c) for c in a.chunk.split(",")):
            for fl in flushes:
                for _ in range(a.repeat):
                    r = run_one(link, mode, total, chunk, fl, hostwrite=a.hostwrite)
                    host = r["bytes"] / r["host_s"] / 1e3
                    dev = r["bytes"] / max(r["dev_us"], 1) * 1e3
                    errs = r["errors"] + r["host_errors"]
                    bad += errs + (r["status"] != 0)
                    print(f"{mode:7s} {chunk:6d} {'yes' if fl else 'no':>5s} | {host:10.0f} {dev:11.0f} | "
                          f"{errs}{'' if r['status']==0 else ' status '+str(r['status'])}  ({r['variant']} {r['rx']}/{r['tx']})")
    link.close()
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
