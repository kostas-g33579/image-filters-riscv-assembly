#!/usr/bin/env python3
"""Tests for STREAMING mode (pico/src/stream.c + the STR1 protocol) without hardware.

build32/proto_test is the same protocol code, streaming pipeline and assembly kernel the
firmware uses, built as a Linux program and run under qemu-riscv32. The image is sent
row by row and the filtered rows come back as they are produced. Every successful job is
compared byte for byte with the already-verified whole-image Linux harness
(build32/filter): streaming must give EXACTLY the whole-image result, for every filter,
bit depth, pass count and shape, including one-pixel-wide and one-row images, deep chains,
very wide and very tall images. Error and memory-limit cases check the status returned and
that the device is still in step afterwards.

Usage: python3 tests/test_stream.py      (or: make test-stream)
"""
import array, importlib.util, os, random, re, struct, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("pico_filter", os.path.join(ROOT, "pico", "tools", "pico_filter.py"))
pf = importlib.util.module_from_spec(spec); spec.loader.exec_module(pf)
pf.TIMEOUT = float(os.environ.get("STREAM_TEST_TIMEOUT", 20))   # generous for the big images; a dead device is caught by the smoke test

DEVICE = f"qemu-riscv32 {os.path.join(ROOT, 'build32', 'proto_test')}"
HARNESS = os.path.join(ROOT, "build32", "filter")
PROTO_H = open(os.path.join(ROOT, "pico", "src", "proto.h")).read()
ARENA = int(re.search(r"#define PROTO_ARENA_BYTES (\d+)u", PROTO_H).group(1))
FILTERS = [name for _, name in re.findall(r'^n_(\w+):\s+\.asciz "(\w+)"', open(os.path.join(ROOT, "src/kernel.s")).read(), re.M)]

failures = 0


def check(name, ok, detail=""):
    global failures
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))
    failures += not ok


def rand_samples(n, maxval, seed):
    rng = random.Random(seed)
    if maxval in (255, 65535):                       # fast for the big images: any bytes are valid samples
        raw = rng.randbytes(n if maxval == 255 else 2 * n)
        if maxval == 255:
            return array.array("H", list(raw))
        a = array.array("H"); a.frombytes(raw)
        if sys.byteorder == "big": a.byteswap()
        return a
    return array.array("H", [rng.randrange(maxval + 1) for _ in range(n)])


def oracle(w, h, maxval, samples, name, passes, window):
    with tempfile.NamedTemporaryFile(suffix=".pgm") as f:
        f.write(pf.pgm_bytes(w, h, maxval, samples)); f.flush()
        return pf.harness_output(f.name, name, passes, window, HARNESS)


def stream_job(link, name, w, h, maxval, samples, passes=None, window=None):
    """Stream one job and compare with the whole-image harness. Returns (ok, detail)."""
    try:
        info, out = pf.process_stream(link, name, w, h, maxval, samples, passes, window)
    except (pf.ProtoError, TimeoutError, EOFError, RuntimeError, BrokenPipeError) as e:
        return False, str(e)
    got = pf.pgm_bytes(w, h, info["maxval"], out)
    want = oracle(w, h, maxval, samples, name, passes, window)
    return got == want, f"{len(got)} vs {len(want)} bytes"


def expect_status(name, args, status, samples=None):
    """Send a STR1 header built from args; the device must answer with `status`."""
    link = pf.PipeLink(DEVICE)
    try:
        link.write(pf.request_header(*args, magic=pf.STR_MAGIC))
        ack = pf.read_response(link, "header", 10)
        check(name, ack["status"] == status, f"status {ack['status']}")
    except Exception as e:
        check(name, False, repr(e))
    finally:
        link.close()


def main():
    for p in (HARNESS, os.path.join(ROOT, "build32", "proto_test")):
        if not os.path.exists(p): sys.exit(f"{p} not found - run `make rv32 build32/proto_test` first")

    link = pf.PipeLink(DEVICE)
    ok, d = stream_job(link, FILTERS[0], 8, 8, 255, rand_samples(64, 255, 0))              # smoke test
    if not ok and ("timed out" in d or "closed" in d):
        link.close(); sys.exit(f"FAIL  the device is unresponsive ({d}); aborting")

    # --- every filter, three bit depths, default pass counts (one long-lived connection)
    for name in FILTERS:
        for (w, h, maxval, seed) in ((24, 13, 255, 1), (17, 9, 4095, 2), (20, 11, 65535, 3)):
            ok, d = stream_job(link, name, w, h, maxval, rand_samples(w * h, maxval, seed))
            check(f"stream {name}: {w}x{h} maxval {maxval}", ok, d)

    # --- the edges of the pipeline: every small width and height, so the top replication and
    # --- the bottom flush happen with 1, 2, 3 and more rows, and with a one-pixel-wide image
    for w in (1, 2, 3, 5, 9):
        for h in (1, 2, 3, 4, 7):
            for name, passes in (("blur", 3), ("emboss", 1), ("sobel", 2)):
                ok, d = stream_job(link, name, w, h, 255, rand_samples(w * h, 255, 100 + 10 * w + h), passes)
                check(f"stream {name} x{passes}: {w}x{h}", ok, d)

    # --- pass counts, including none and the maximum
    s = rand_samples(13 * 11, 4095, 4)
    for passes in (0, 1, 2, 3, 5, 8, 16):
        ok, d = stream_job(link, "blur", 13, 11, 4095, s, passes); check(f"stream blur passes={passes}", ok, d)
    ok, d = stream_job(link, "sobel", 13, 11, 4095, s, 6); check("stream sobel (two-kernel path) passes=6", ok, d)
    ok, d = stream_job(link, "sharpen", 6, 5, 255, rand_samples(30, 255, 5), 255); check("stream 255 passes (the maximum)", ok, d)
    ok, d = stream_job(link, "gaussian", 6, 2, 255, rand_samples(12, 255, 6), 255); check("stream 255 passes on a 2-row image", ok, d)

    # --- window/level on the streamed rows
    s = rand_samples(30 * 21, 4095, 7)
    for (c, wd) in ((2048, 4096), (400, 200), (1000, 1)):
        ok, d = stream_job(link, "sharpen", 30, 21, 4095, s, window=(c, wd)); check(f"stream window c={c} w={wd}", ok, d)
    ok, d = stream_job(link, "sobel", 30, 21, 4095, s, 2, (2000, 1500)); check("stream passes + window together", ok, d)

    # --- both request types on one connection, mixed, and noise before a request
    link.write(b"boot noise \x00\xff STR STR0 garbage")
    ok, d = stream_job(link, "edge", 10, 10, 255, rand_samples(100, 255, 8)); check("noise before a STR1 request is skipped", ok, d)
    for i in range(4):
        w_, h_, m_ = 7 + i, 5 + i, 255
        s = rand_samples(w_ * h_, m_, 20 + i)
        ok, d = stream_job(link, "blur", w_, h_, m_, s, i); check(f"back-to-back stream {i + 1}", ok, d)
        info, out = pf.process(link, "blur", w_, h_, m_, s, i)
        check(f"whole-image (IMG1) request {i + 1} between streams still works",
              pf.pgm_bytes(w_, h_, info['maxval'], out) == oracle(w_, h_, m_, s, "blur", i, None))
    link.close()

    # --- big and unusual shapes (fresh connection each)
    big = [("blur", 2048, 48, 255, 4, "2048 wide (the width the notes called a ceiling)"),
           ("gaussian", 4096, 12, 65535, 8, "4096 wide, 16-bit, 8 passes"),
           ("gaussian", 640, 600, 4095, None, "640x600 12-bit, default 4 passes (a large MRI matrix)"),
           ("sobel", 40, 5000, 255, 2, "tall: 5000 rows, two-kernel path"),
           ("sharpen", 1024, 1024, 255, 2, "1024x1024"),
           ("blur", 2048, 10, 255, 33, "2048 wide with 33 passes (just inside the memory budget)"),
           ("edge", 1, 20000, 65535, 3, "one pixel wide, 20000 rows"),
           ("emboss", 16000, 1, 255, 3, "one row, 16000 wide")]
    for name, w, h, maxval, passes, label in big:
        s = rand_samples(w * h, maxval, 40 + w % 7)
        lk = pf.PipeLink(DEVICE)
        try:
            ok, d = stream_job(lk, name, w, h, maxval, s, passes)
        finally:
            lk.close()
        check(f"large: {label}", ok, d)
    for (w, h, label) in ((16384, 3, "16384 wide, 3 rows (the width sanity limit)"),):
        s = rand_samples(w * h, 255, 50)
        lk = pf.PipeLink(DEVICE)
        try:
            ok, d = stream_job(lk, "blur", w, h, 255, s, 2)
        finally:
            lk.close()
        check(f"large: {label}", ok, d)

    # --- memory budget: passes * 3 * width * 2 (+ one output row) must fit the arena
    need = lambda w_, p_: (p_ * 3 + 1) * w_ * 2
    check("budget arithmetic: 2048 wide, 33 passes fits; 34 does not",
          need(2048, 33) <= ARENA < need(2048, 34), f"{need(2048, 33)} {need(2048, 34)} {ARENA}")
    # the exact edge: a job that uses the arena to the byte is accepted; the same job with a window
    # needs one more byte per sample for the display row and is refused
    wx = ARENA // 200                                  # 33 passes: need = 200 * width
    check("boundary job uses the arena exactly", need(wx, 33) <= ARENA < need(wx, 33) + wx, f"{wx} {need(wx, 33)} {ARENA}")
    lk = pf.PipeLink(DEVICE)
    ok, d = stream_job(lk, "blur", wx, 3, 255, rand_samples(wx * 3, 255, 80), 33)
    lk.close()
    check(f"a job needing {need(wx, 33)} of {ARENA} arena bytes is accepted and exact", ok, d)
    expect_status("the same job with a window needs more than the arena -> not enough memory",
                  ("blur", wx, 3, 255, 33, (100, 50)), 9)
    expect_status("height exactly 2**20 is accepted", ("blur", 1, 1 << 20, 255), 0)
    expect_status("width exactly 16384 is accepted", ("blur", 16384, 2, 255, 1), 0)
    expect_status("2048 wide, 34 passes -> not enough memory", ("blur", 2048, 4, 255, 34), 9)
    expect_status("16384 wide, 8 passes -> not enough memory", ("blur", 16384, 4, 255, 8), 9)
    link = pf.PipeLink(DEVICE)
    try:
        pf.process_stream(link, "blur", 2048, 4, 255, rand_samples(2048 * 4, 255, 60), 34)
        check("process_stream raises on the memory error", False)
    except pf.ProtoError as e:
        check("process_stream raises ProtoError(9) on the memory error", e.status == 9)
    ok, d = stream_job(link, "blur", 9, 6, 255, rand_samples(54, 255, 61), 2)
    check("device still works after a memory rejection", ok, d)
    link.close()

    # --- header rejections
    ok_args = ("blur", 8, 8, 255)
    expect_status("unknown filter", ("nosuchfilter", 8, 8, 255), 2)
    expect_status("zero width", ("blur", 0, 8, 255), 3)
    expect_status("zero height", ("blur", 8, 0, 255), 3)
    expect_status("width over the sanity limit", ("blur", 16385, 2, 255), 3)
    expect_status("height over the sanity limit", ("blur", 8, (1 << 20) + 1, 255), 3)
    expect_status("maxval 0", ("blur", 8, 8, 0), 4)
    expect_status("maxval 65536", ("blur", 8, 8, 65536), 4)
    expect_status("passes 256", ("blur", 8, 8, 255, 256), 5)
    expect_status("window width 0", ("blur", 8, 8, 255, None, (10, 0)), 6)

    # --- a bad sample in the middle of the stream: status 7, the rest is drained, device stays in step
    link = pf.PipeLink(DEVICE)
    s = rand_samples(10 * 8, 255, 70); s[3 * 10 + 4] = 256            # row 3 of 8
    try:
        pf.process_stream(link, "blur", 10, 8, 255, s, 2)
        check("sample above maxval mid-stream is reported", False)
    except pf.ProtoError as e:
        check("sample above maxval mid-stream -> status 7", e.status == 7, f"status {e.status}")
    ok, d = stream_job(link, "blur", 11, 6, 255, rand_samples(66, 255, 71), 2)
    check("device is back in step after the error (the remaining rows were discarded)", ok, d)
    s = rand_samples(10 * 8, 1000, 72); s[0] = 1001                      # the very first row
    try:
        pf.process_stream(link, "blur", 10, 8, 1000, s, 1)
        check("sample above maxval in the first row is reported", False)
    except pf.ProtoError as e:
        check("sample above maxval in the first row -> status 7", e.status == 7)
    ok, d = stream_job(link, "sharpen", 5, 5, 255, rand_samples(25, 255, 73))
    check("device still in step after an error in row 0", ok, d)
    s = rand_samples(10 * 8, 1000, 74); s[-1] = 1001                     # the very last sample
    try:
        pf.process_stream(link, "blur", 10, 8, 1000, s, 1)
        check("sample above maxval in the last row is reported", False)
    except pf.ProtoError as e:
        check("sample above maxval in the last row -> status 7", e.status == 7)
    ok, d = stream_job(link, "sharpen", 5, 5, 255, rand_samples(25, 255, 75))
    check("device still in step after an error in the last row", ok, d)
    link.close()

    # --- the host stops mid-row: the device reports a timeout instead of waiting forever
    link = pf.PipeLink(DEVICE)
    link.write(pf.request_header("blur", 10, 6, 255, magic=pf.STR_MAGIC))
    ack = pf.read_response(link, "header", 10)
    link.write(pf.to_le_bytes([1] * 10))                                 # row 0 complete
    link.write(pf.to_le_bytes([1] * 4))                                  # row 1 cut off
    link.close_stdin()
    frames = []
    try:
        while True:
            head = link.read_exact(4, 10)
            if head == pf.ROW_MAGIC:
                link.read_exact(4, 10); link.read_exact(ack["payload"], 10); frames.append("row")
            else:
                st = struct.unpack("<9I", link.read_exact(36, 10))[0]; break
        check("host stops mid-row -> timeout status 8", ack["status"] == 0 and st == 8, f"status {st}")
    except Exception as e:
        check("host stops mid-row -> timeout status 8", False, repr(e))
    link.close()

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    sys.exit(1 if failures else 0)


main()
