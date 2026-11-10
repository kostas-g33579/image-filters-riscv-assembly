#!/usr/bin/env python3
"""Tests for the separable kernel in the streaming protocol (STR1 with flags bit 1; pico/src/stream.c, proto.c, src/gauss.s)
without hardware: build32/proto_test is the same protocol, pipeline and assembly the firmware uses, run under qemu-riscv32.

The expected result comes from a model written here, independent of the client's own --verify: the symmetric kernel laid out in
full, each row padded by repeating its ends, integer arithmetic with the same rounding, applied `passes` times. A window is
applied by the already verified Linux harness (identity filter with that window). Checked:
  * bit-exact output for radii 0..15, kernels of several kinds, widths 1..100, heights from 1 up to well beyond the ring
    (including every height around 2r+1), several bit depths, 1 to 3 passes, with and without a window
  * the 3x3 filters still work on the same connection before, between and after (no state leaks)
  * the memory limit to the byte, and the status for every way to send a bad kernel; the device is in step afterwards
  * a bad sample in the first, middle and last row; a host that stops in the middle of the kernel or of a row
(The cycle and instruction counters read 0 under the emulator; they are checked on the board.)
"""
import array, importlib.util, os, random, re, struct, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import gauss_weights as gw
spec = importlib.util.spec_from_file_location("pico_filter", os.path.join(ROOT, "pico", "tools", "pico_filter.py"))
pf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pf)
pf.TIMEOUT = float(os.environ.get("STREAM_TEST_TIMEOUT", 20))

DEVICE = f"qemu-riscv32 {os.path.join(ROOT, 'build32', 'proto_test')}"
HARNESS = os.path.join(ROOT, "build32", "filter")
ARENA = int(re.search(r"#define PROTO_ARENA_BYTES (\d+)u", open(os.path.join(ROOT, "pico", "src", "proto.h")).read()).group(1))
PS_OK, PS_BAD_DIMS, PS_BAD_SAMPLE, PS_TIMEOUT, PS_NO_MEMORY, PS_BAD_KERNEL = 0, 3, 7, 8, 9, 10
failures = 0


def check(name, ok, detail=""):
    global failures
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))
    failures += not ok


# ------------------------------------------------------------------ the model
def model(samples, W, H, w, passes):
    r, K = len(w) - 1, w[:0:-1] + w
    rows = [list(samples[y * W:(y + 1) * W]) for y in range(H)]
    for _ in range(passes):
        mid = [[(sum(K[j] * ([row[0]] * r + row + [row[-1]] * r)[x + j] for j in range(2 * r + 1)) + 8192) >> 14 for x in range(W)] for row in rows]
        rows = [[(sum(K[j] * mid[min(max(y + j - r, 0), H - 1)][x] for j in range(2 * r + 1)) + 8192) >> 14 for x in range(W)] for y in range(H)]
    return [v for row in rows for v in row]


def windowed(values, W, H, maxval, window):
    with tempfile.NamedTemporaryFile(suffix=".pgm") as f:
        f.write(pf.pgm_bytes(W, H, maxval, array.array("H", values))); f.flush()
        return pf.harness_output(f.name, "identity", 1, window, HARNESS)


def pgm_payload(out, W, H, maxval):
    return pf.pgm_bytes(W, H, maxval, out)


def rand_samples(n, maxval, rng):
    return array.array("H", [rng.randrange(maxval + 1) for _ in range(n)])


# ------------------------------------------------------------------ talking to the device
def job(link, W, H, maxval, samples, kernel, passes=None, window=None):
    """Run one separable job. Returns (ok, detail, info)."""
    try:
        info, out = pf.process_stream(link, "gauss", W, H, maxval, samples, passes, window, kernel)
    except (pf.ProtoError, TimeoutError, EOFError, RuntimeError, BrokenPipeError) as e:
        return False, str(e), None
    p = 1 if passes is None else passes
    want_vals = model(list(samples), W, H, kernel, p)
    if window:
        want = windowed(want_vals, W, H, maxval, window)
        got = pgm_bytes_for(out, W, H, info["maxval"])
        return got == want, "window output differs", info
    return list(out) == want_vals, "values differ (first %r vs %r)" % (list(out)[:6], want_vals[:6]), info


def pgm_bytes_for(out, W, H, maxval):
    return pf.pgm_bytes(W, H, maxval, out)


def raw_request(link, W, H, maxval, kernel_payload, flags=2, passes=0xFFFFFFFF, magic=pf.STR_MAGIC):
    """Send a header with arbitrary flags and kernel bytes; return the device's first response."""
    link.write(struct.pack(pf.REQ_FMT, magic, b"gauss", W, H, maxval, passes, flags, 0, 0) + kernel_payload)
    return pf.read_response(link, "header")


def in_step(link, label):
    """A good job right after: proves the device is back at a message boundary."""
    rng = random.Random(1)
    ok, why, _ = job(link, 9, 5, 255, rand_samples(45, 255, rng), gw.gaussian(1.0, 3))
    return ok, "%s: the next job failed (%s)" % (label, why)


def main():
    if not os.path.exists(os.path.join(ROOT, "build32", "proto_test")) or not os.path.exists(HARNESS):
        sys.exit("build the emulator programs first: make test-gauss-stream (needs build32/proto_test and build32/filter)")
    rng = random.Random(20261110)
    link = pf.PipeLink(DEVICE)

    # ---- exactness over radii, kernels, shapes, depths, passes, windows
    kernels = lambda r: ([("identity", [gw.TOTAL])] if r == 0 else
                         [("gauss", gw.gaussian(max(r / 3, 0.5), r)), ("box", gw.box(r)), ("random", gw.quantise([rng.random() + 0.01 for _ in range(r + 1)])),
                          ("far", gw.quantise([0.0] * r + [1.0]))])
    n = bad = 0
    bad_list = []
    for r in (0, 1, 2, 3, 5, 8, 15):
        heights = sorted(h_ for h_ in {1, 2, 3, r - 1, r, r + 1, 2 * r, 2 * r + 1, 2 * r + 2, 2 * r + 3, 37} if h_ >= 1)
        widths = (1, 3, 16, 100) if r <= 8 else (1, 3, 16)
        for kname, k in kernels(r):
            if r >= 8 and kname in ("box", "far"):
                continue
            for W in widths:
                for H in heights:
                    maxval = (255, 4095, 65535)[(W + H + r) % 3]
                    passes = 1 if (W + H) % 7 else 2
                    window = (maxval // 2, maxval // 3 + 1) if (W * H + r) % 5 == 0 else None
                    ok, why, info = job(link, W, H, maxval, rand_samples(W * H, maxval, rng), k, None if passes == 1 else passes, window)
                    n += 1
                    if not ok:
                        bad += 1
                        bad_list.append((r, kname, W, H, maxval, passes, bool(window), why[:60]))
    check(f"{n} separable jobs (radii 0..15, 4 kernel kinds, widths 1..100, heights around 2r+1 and beyond, 8/12/16 bits, 1-2 passes, some windowed) equal the independent model bit for bit",
          not bad, str(bad_list[:3]))

    # ---- chains of passes, and extreme data
    bad_list = []
    for passes in (1, 2, 3, 5):
        k = gw.gaussian(1.2, 4)
        for pname, vals in (("max", [65535] * 60), ("zero", [0] * 60), ("alternating", [65535 * (i & 1) for i in range(60)])):
            ok, why, _ = job(link, 6, 10, 65535, array.array("H", vals), k, passes)
            if not ok:
                bad_list.append((passes, pname, why[:50]))
    check("chains of 1, 2, 3 and 5 passes on all-65535, all-0 and alternating data are exact", not bad_list, str(bad_list[:3]))
    ok, why, _ = job(link, 8, 8, 4095, array.array("H", [1234] * 64), gw.gaussian(2.0, 6), 3)
    check("a constant image comes out unchanged after 3 passes", ok, why)

    # ---- the 3x3 filters before, between and after (no state leaks in the shared pipeline or arena)
    bad_list = []
    for i in range(6):
        W, H, maxval = 12 + i, 9 + i, 4095
        s = rand_samples(W * H, maxval, rng)
        try:
            info, out = pf.process_stream(link, "gaussian", W, H, maxval, s, 2, None)
            with tempfile.NamedTemporaryFile(suffix=".pgm") as f:
                f.write(pf.pgm_bytes(W, H, maxval, s)); f.flush()
                want = pf.harness_output(f.name, "gaussian", 2, None, HARNESS)
            if pf.pgm_bytes(W, H, info["maxval"], out) != want:
                bad_list.append(("3x3 differs", i))
        except Exception as e:                                                  # noqa: BLE001 - report which step failed
            bad_list.append(("3x3", i, str(e)[:50]))
        ok, why, _ = job(link, W, H, maxval, rand_samples(W * H, maxval, rng), gw.gaussian(1.0 + i / 3, 1 + i), 1 + i % 2)
        if not ok:
            bad_list.append(("sep", i, why[:50]))
    check("named 3x3 jobs and separable jobs alternate on one connection and every result is right", not bad_list, str(bad_list[:3]))

    # ---- the memory limit
    r, passes = 15, 4
    rows = passes * (2 * r + 1) + 2                                               # ring rows, staging row, sink row (16-bit samples)
    fits = lambda W, win: (((rows * W * 2) + 3) & ~3) + 4 * W + (W if win else 0) <= ARENA
    for win in (False, True):
        Wmax = max(W for W in range(1, 3000) if fits(W, win))
        k = gw.gaussian(5.0, r)
        win_arg = (100, 50) if win else None
        ok_in, why, _ = job(link, Wmax, 3, 255, rand_samples(Wmax * 3, 255, rng), k, passes, win_arg)
        try:
            pf.process_stream(link, "gauss", Wmax + 1, 3, 255, rand_samples((Wmax + 1) * 3, 255, rng), passes, win_arg, k)
            st = 0
        except pf.ProtoError as e:
            st = e.status
        check(f"memory limit to the byte{' (with a window)' if win else ''}: width {Wmax} with r=15 and 4 passes fits, width {Wmax + 1} gives status 9 (no memory); the device stays in step",
              ok_in and st == PS_NO_MEMORY and in_step(link, "memory")[0], f"{why} / status {st}")

    # ---- bad kernels: each must be refused with status 10 and leave the device in step
    good = gw.gaussian(1.0, 3)
    pk = lambda r_, w_: struct.pack("<I%di" % len(w_), r_, *w_)
    cases = {
        "radius 16": pk(16, [1000] * 17),
        "radius 255": pk(255, [64] * 256),
        "a negative centre weight and a wrong sum": pk(2, [-2, 4097, 4096]),
        "a negative last weight (the sum is right: 8192 + 2*(8192-4096))": pk(2, [8192, 8192, -4096]),
        "a negative first side weight (the sum is right)": pk(2, [8192, -4096, 8192]),
        "a negative centre weight (the sum is right)": pk(2, [-2, 8193, 0]),
        "non-negative weights whose sum wraps around 32 bits to 16384 (16386 + 2*0x7FFFFFFF)": pk(1, [16386, 0x7FFFFFFF]),
        "weights summing to 16383": pk(1, [16381, 1]),
        "weights summing to 16385": pk(1, [16385, 0]),
        "weights summing to 0": pk(2, [0, 0, 0]),
        "a sum that wraps around 32 bits (2 * 0x80002000 = 16384 mod 2^32)": pk(1, [0, -2147475456]),
        "a huge positive weight (2 * 0x40002000 > 2^31)": pk(1, [0, 0x40002000]),
        "a centre weight of 2^31": pk(1, [-2147483648, 1073750016]),
    }
    bad_list = []
    for label, payload in cases.items():
        resp = raw_request(link, 8, 4, 255, payload)
        if resp["status"] != PS_BAD_KERNEL:
            bad_list.append((label, resp["status"]))
        elif payload and not in_step(link, label)[0]:
            bad_list.append((label, "out of step"))
    check(f"{len(cases)} bad kernels (radius above 15, negative weights, wrong sums, 32-bit wrap-around) are refused with status 10 and the device stays in step", not bad_list, str(bad_list[:3]))
    resp = raw_request(link, 8, 4, 255, pk(3, good[:1] + good[1:]), magic=pf.REQ_MAGIC)
    check("a kernel in whole-image mode (IMG1) is refused with status 10", resp["status"] == PS_BAD_KERNEL and in_step(link, "IMG1")[0], str(resp["status"]))
    resp = raw_request(link, 0, 4, 255, pk(3, good))
    check("a kernel with a bad width is refused for the width (status 3), the bytes after the header being consumed", resp["status"] == PS_BAD_DIMS and in_step(link, "dims")[0], str(resp["status"]))
    resp = raw_request(link, 8, 4, 255, pk(3, good), passes=300)
    check("a kernel with too many passes is refused for the passes (status 5)", resp["status"] == 5 and in_step(link, "passes")[0], str(resp["status"]))

    # ---- a host that stops in the middle of the kernel (the emulated pipe reports a timeout when its input ends, as the board
    # does after its read timeout; the connection ends with it, so each case has its own)
    for label, cut in (("before r", 0), ("after r, before any weight", 4), ("in the middle of the weights", 4 + 2 * 4)):
        lk = pf.PipeLink(DEVICE)
        lk.write(struct.pack(pf.REQ_FMT, pf.STR_MAGIC, b"gauss", 8, 4, 255, 0xFFFFFFFF, 2, 0, 0) + pk(3, good)[:cut])
        lk.close_stdin()
        try:
            st = pf.read_response(lk, "timeout", timeout=10)["status"]
        except Exception as e:                                                  # noqa: BLE001 - report it
            st = repr(e)
        lk.close()
        check(f"the host stops {label}: the device answers with a timeout (status 8)", st == PS_TIMEOUT, str(st))

    # ---- bad samples and a host that stops mid-row
    bad_list = []
    for where in ("first", "middle", "last"):
        H = 12
        s = rand_samples(7 * H, 255, rng)
        idx = {"first": 3, "middle": 7 * 6 + 2, "last": 7 * H - 1}[where]
        s[idx] = 256
        try:
            pf.process_stream(link, "gauss", 7, H, 255, s, None, None, gw.gaussian(1.0, 2))
            bad_list.append((where, "accepted"))
        except pf.ProtoError as e:
            if e.status != PS_BAD_SAMPLE:
                bad_list.append((where, e.status))
        if not in_step(link, where)[0]:
            bad_list.append((where, "out of step"))
    check("a sample above maxval in the first, middle and last row: status 7, the rest of the rows are discarded, the device stays in step", not bad_list, str(bad_list))
    lk = pf.PipeLink(DEVICE)
    lk.write(pf.request_header("gauss", 20, 6, 255, None, None, magic=pf.STR_MAGIC, kernel=good) + pf.kernel_bytes(good))
    ack = pf.read_response(lk, "ack")
    lk.write(array.array("H", [7] * 25).tobytes())                                # one row and a quarter, then the input ends
    lk.close_stdin()
    st = None
    try:
        while True:
            head = lk.read_exact(4, 10)
            if head == pf.ROW_MAGIC:
                lk.read_exact(4 + ack["payload"], 10)
            else:
                st = struct.unpack("<9I", lk.read_exact(36, 10))[0]
                break
    except Exception as e:                                                      # noqa: BLE001 - report it
        st = repr(e)
    lk.close()
    check("a host that stops in the middle of a row: the device answers with a timeout (status 8)", ack["status"] == 0 and st == PS_TIMEOUT, str(st))

    link.close()
    import subprocess
    lay = subprocess.run([os.path.join(ROOT, "build32", "stream_layout_host")], capture_output=True, text=True)
    check("memory layout (host program): for every width 1..70, radius 0..15 and 0..4 passes the rings, rows and accumulators lie inside the "
          "reported size without overlap, rows are 2-byte and accumulators 4-byte aligned, and one byte less is refused",
          lay.returncode == 0 and lay.stdout.startswith("LAYOUT OK"), lay.stdout.strip())
    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    sys.exit(1 if failures else 0)


main()
