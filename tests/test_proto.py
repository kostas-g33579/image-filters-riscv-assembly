#!/usr/bin/env python3
"""Tests for the Pico device protocol (pico/src/proto.c) without hardware.

build32/proto_test is the same protocol code and the same assembly kernel the
firmware uses, built as a Linux program and run under qemu-riscv32 with the
wire protocol on stdin/stdout. Every successful job is compared byte for byte
with the already-verified Linux harness (build32/filter), so this checks the C
layer, the C/assembly interface (the filter record layout) and the wire format.
Error paths check the status the device returns.

Usage: python3 tests/test_proto.py      (or: make test-proto)
"""
import array, importlib.util, os, random, re, struct, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("pico_filter", os.path.join(ROOT, "pico", "tools", "pico_filter.py"))
pf = importlib.util.module_from_spec(spec); spec.loader.exec_module(pf)
_orig_read_exact = pf.Link.read_exact
pf.Link.read_exact = lambda self, n, timeout=None: _orig_read_exact(self, n, min(pf.TIMEOUT if timeout is None else timeout, 3.0))   # fail fast
_orig_write = pf.Link.write
pf.Link.write = lambda self, data, timeout=None: _orig_write(self, data, min(pf.TIMEOUT if timeout is None else timeout, 3.0))

DEVICE = f"qemu-riscv32 {os.path.join(ROOT, 'build32', 'proto_test')}"
HARNESS = os.path.join(ROOT, "build32", "filter")
MAX_PIXELS = int(re.search(r"#define PROTO_MAX_PIXELS (\d+)u", open(os.path.join(ROOT, "pico/src/proto.h")).read()).group(1))
FILTERS = re.findall(r'^n_(\w+):\s+\.asciz "(\w+)"', open(os.path.join(ROOT, "src/kernel.s")).read(), re.M)
FILTERS = [name for _, name in FILTERS]

failures = 0


def check(name, ok, detail=""):
    global failures
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))
    failures += not ok


def rand_samples(n, maxval, seed):
    rng = random.Random(seed)
    return array.array("H", [rng.randrange(maxval + 1) for _ in range(n)])


def job(link, name, w, h, maxval, samples, passes=None, window=None):
    """Run one job on the device and on the harness; return (match, detail)."""
    try:
        info, out = pf.process(link, name, w, h, maxval, samples, passes, window)
    except (pf.ProtoError, TimeoutError, EOFError, BrokenPipeError) as e:      # a broken device must FAIL a test, not crash the run
        return False, str(e)
    got = pf.pgm_bytes(w, h, info["maxval"], out)
    with tempfile.NamedTemporaryFile(suffix=".pgm") as f:
        f.write(pf.pgm_bytes(w, h, maxval, samples)); f.flush()
        want = pf.harness_output(f.name, name, passes, window, HARNESS)
    return got == want, f"{len(got)} vs {len(want)} bytes"


def expect_status(name, hdr_bytes, status, payload=None):
    """Send a hand-built header; the device must reject it with `status`."""
    link = pf.PipeLink(DEVICE)
    try:
        link.write(hdr_bytes)
        ack = pf.read_response(link, "header", 10)
        if payload is None:                          # rejected at the header
            ok, got = ack["status"] == status, ack["status"]
        else:                                        # header accepted; samples rejected afterwards
            link.write(payload)
            res = pf.read_response(link, "samples", 10)
            ok, got = ack["status"] == 0 and res["status"] == status, (ack["status"], res["status"])
        check(name, ok, f"status {got}")
    except Exception as e:
        check(name, False, repr(e))
    finally:
        link.close()


def main():
    for p in (HARNESS, os.path.join(ROOT, "build32", "proto_test")):
        if not os.path.exists(p): sys.exit(f"{p} not found - run `make rv32 build32/proto_test` first")

    # --- every filter, three bit depths, one long-lived device process ---
    link = pf.PipeLink(DEVICE)
    ok, d = job(link, FILTERS[0], 8, 8, 255, rand_samples(64, 255, 0))    # smoke test
    if not ok and ("timed out" in d or "closed" in d):
        link.close()
        sys.exit(f"FAIL  the device is unresponsive ({d}); aborting")
    for name in FILTERS:
        for (w, h, maxval, seed) in ((24, 13, 255, 1), (17, 9, 4095, 2), (20, 11, 65535, 3)):
            ok, d = job(link, name, w, h, maxval, rand_samples(w * h, maxval, seed))
            check(f"{name}: {w}x{h} maxval {maxval}", ok, d)
    check("(device served every request above from one process)", link.proc.poll() is None)

    # --- options: passes and window/level ---
    s = rand_samples(30 * 21, 4095, 4)
    for passes in (0, 1, 3):
        ok, d = job(link, "blur", 30, 21, 4095, s, passes=passes); check(f"blur passes={passes}", ok, d)
    for (c, wd) in ((2048, 4096), (400, 200), (0, 100), (4095, 4095), (1000, 1), (1000, 2)):
        ok, d = job(link, "sharpen", 30, 21, 4095, s, window=(c, wd)); check(f"window c={c} w={wd}", ok, d)
    ok, d = job(link, "sobel", 30, 21, 4095, s, passes=2, window=(2000, 1500)); check("passes + window together", ok, d)

    # samples equal to maxval are valid (only values ABOVE maxval are rejected)
    for maxval in (1, 255, 256, 4095, 65535):
        ok, d = job(link, "blur", 6, 5, maxval, array.array("H", [maxval] * 30)); check(f"all samples == maxval {maxval}", ok, d)

    # --- shapes and sizes, including the device's largest image ---
    ok, d = job(link, "gaussian", 1, 1, 255, array.array("H", [200])); check("1x1 image", ok, d)
    ok, d = job(link, "blur", 1, 301, 255, rand_samples(301, 255, 5)); check("single column", ok, d)
    ok, d = job(link, "blur", 301, 1, 255, rand_samples(301, 255, 6)); check("single row", ok, d)
    side = int(MAX_PIXELS ** 0.5)
    ok, d = job(link, "blur", side, side, 65535, rand_samples(side * side, 65535, 7), passes=2)
    check(f"largest square image ({side}x{side}, 16-bit)", ok, d)
    ok, d = job(link, "sobel", MAX_PIXELS, 1, 255, rand_samples(MAX_PIXELS, 255, 8), passes=1)
    check(f"largest single row ({MAX_PIXELS}x1)", ok, d)
    link.close()

    # --- resynchronisation: noise before a request, and back-to-back requests ---
    link = pf.PipeLink(DEVICE)
    link.write(b"boot noise \x00\xff IMG IMG0 garbage OU")             # near-miss magics included
    s = rand_samples(10 * 10, 255, 9)
    ok, d = job(link, "edge", 10, 10, 255, s); check("noise before the request is skipped", ok, d)
    for i in range(5):
        ok, d = job(link, "blur", 10, 10, 255, s, passes=i)
        check(f"back-to-back request {i + 1}", ok, d)
    link.close()

    # --- rejected headers: each must get the right status and leave the device usable ---
    good = pf.request_header("blur", 4, 4, 255)
    expect_status("unknown filter", pf.request_header("nosuchfilter", 4, 4, 255), 2)
    expect_status("filter name is case-sensitive", pf.request_header("Blur", 4, 4, 255), 2)
    expect_status("filter name prefix rejected", pf.request_header("blu", 4, 4, 255), 2)
    expect_status("zero width", pf.request_header("blur", 0, 4, 255), 3)
    expect_status("zero height", pf.request_header("blur", 4, 0, 255), 3)
    expect_status("one pixel over the limit", pf.request_header("blur", MAX_PIXELS + 1, 1, 255), 3)
    expect_status("width*height over the limit", pf.request_header("blur", 1000, 1000, 255), 3)
    expect_status("32-bit overflow in width*height", pf.request_header("blur", 65537, 65537, 255), 3)
    expect_status("maxval 0", pf.request_header("blur", 4, 4, 0), 4)
    expect_status("maxval 65536", pf.request_header("blur", 4, 4, 65536), 4)
    expect_status("passes 256", pf.request_header("blur", 4, 4, 255, passes=256), 5)
    expect_status("window width 0", pf.request_header("blur", 4, 4, 255, window=(10, 0)), 6)
    expect_status("window width 65536", pf.request_header("blur", 4, 4, 255, window=(10, 65536)), 6)
    expect_status("window center 65536", pf.request_header("blur", 4, 4, 255, window=(65536, 10)), 6)
    expect_status("sample above maxval", good, 7, payload=pf.to_le_bytes([0] * 15 + [256]))
    expect_status("16-bit sample above maxval", pf.request_header("blur", 2, 2, 1000), 7,
                  payload=pf.to_le_bytes([1, 2, 1001, 4]))

    # a rejected request must not wedge the device: the next one still works
    link = pf.PipeLink(DEVICE)
    try:
        pf.process(link, "nosuchfilter", 4, 4, 255, array.array("H", [0] * 16))
    except pf.ProtoError:
        pass
    ok, d = job(link, "blur", 8, 8, 255, rand_samples(64, 255, 10)); check("device still works after a rejected request", ok, d)
    link.close()

    # --- the host stops mid-message: the device reports a timeout ---
    link = pf.PipeLink(DEVICE)
    link.write(good)
    ack = pf.read_response(link, "header", 10)
    link.write(pf.to_le_bytes([0] * 7))               # 7 of 16 samples, then the link goes quiet
    link.close_stdin()
    res = pf.read_response(link, "samples", 10)
    check("truncated sample data -> timeout status", ack["status"] == 0 and res["status"] == 8, f"{res['status']}")
    link.close()
    link = pf.PipeLink(DEVICE)
    link.write(good[:20])                              # header cut off mid-way
    link.close_stdin()
    check("truncated header -> timeout status", pf.read_response(link, "header", 10)["status"] == 8)
    link.close()

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    sys.exit(1 if failures else 0)


main()
