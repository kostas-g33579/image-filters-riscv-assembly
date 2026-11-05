#!/usr/bin/env python3
"""Tests for the USB benchmark logic (pico/bench/bench.c) under qemu-riscv32, no hardware.

A throughput number is only worth reading if the benchmark itself is right, so this
checks that it moves exactly the bytes it claims, notices a corrupted or dropped byte,
rejects bad commands, skips line noise before a command and runs commands back to back.

Usage: python3 tests/test_bench.py      (or: make test-bench)
"""
import importlib.util, os, struct, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("usb_bench", os.path.join(ROOT, "pico", "tools", "usb_bench.py"))
ub = importlib.util.module_from_spec(spec); spec.loader.exec_module(ub)
BIN = os.environ.get("BENCH_BIN", os.path.join(ROOT, "build32", "bench_test"))
CMD = f"qemu-riscv32 {BIN}"
failures = 0


def check(name, ok, detail=""):
    global failures
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))
    failures += not ok


def link():
    l = ub.pf.PipeLink(CMD)
    return l


def raw(link_, mode, total, chunk, flags=0):
    link_.write(struct.pack("<4s4I", b"BNCH", mode, total, chunk, flags), 5)


def done(link_):
    return struct.unpack(ub.DONE_FMT, link_.read_exact(32, 5))


# 1. every mode, odd sizes (total not a multiple of the chunk), chunk 1 .. 65536
for mode in ("sink", "source", "echo"):
    l = link()
    ok, detail = True, ""
    for total, chunk in [(1, 1), (7, 3), (255, 256), (256, 256), (257, 256), (4096, 64), (10007, 4096),
                         (70000, 65536), (65536, 65536), (200000, 1000)]:
        for fl in (False, True):
            r = ub.run_one(l, mode, total, chunk, fl, 20)
            if not (r["bytes"] == total and r["errors"] == 0 and r["host_errors"] == 0 and r["status"] == 0):
                ok, detail = False, f"{total}/{chunk} flush={fl}: {r}"
    check(f"{mode}: exact byte counts and no errors for 10 size combinations x flush on/off", ok, detail)
    l.close()

# 2. the device notices a corrupted byte in the data it is sent (sink and echo), at several positions
for mode in (1, 3):
    for pos in (0, 1, 99, 4095):
        l = link()
        raw(l, mode, 4096, 4096)
        data = bytearray(ub.pattern(4096))
        data[pos] ^= 0x40
        # echo must be read concurrently or both pipes fill; 4 KB fits the pipes so write first
        l.write(bytes(data), 5)
        if mode == 3: l.read_exact(4096, 5)
        d = done(l)
        check(f"mode {mode}: one flipped bit at byte {pos} is counted as exactly 1 error", d[1] == 0 and d[4] == 1, str(d))
        l.close()

# 3. a dropped byte (everything after it is shifted) is noticed, not silently accepted
l = link(); raw(l, 1, 1000, 1000)
l.write(ub.pattern(1001)[:10] + ub.pattern(1001)[11:], 5)
d = done(l)
check("a dropped byte produces errors", d[4] > 0, str(d))
l.close()

# 4. bad arguments are refused with status 1 and the link stays usable afterwards
for args in [(0, 100, 10), (4, 100, 10), (1, 0, 10), (1, 100, 0), (1, 100, 65537), (1, (1 << 30) + 1, 10), (2, 0, 10)]:
    l = link()
    raw(l, *args)
    d = done(l)
    r = ub.run_one(l, "source", 100, 10, False, 5)
    check(f"bad command {args} -> status 1, then the next command works", d[1] == 1 and r["errors"] == 0 and r["bytes"] == 100, str(d))
    l.close()

# 5. noise before the magic (including a partial magic) is skipped
l = link()
l.write(b"\x00\xff BN BNC xx", 5)
r = ub.run_one(l, "source", 1000, 100, False, 5)
check("garbage and a partial magic before a command are skipped", r["bytes"] == 1000 and r["errors"] == 0, str(r))
l.close()

# 6. 25 commands back to back on one connection
l = link(); ok = True
for i in range(25):
    r = ub.run_one(l, ("sink", "source", "echo")[i % 3], 1000 + i, 97, bool(i & 1), 5)
    ok &= r["bytes"] == 1000 + i and r["errors"] == 0 and r["host_errors"] == 0
check("25 mixed commands in a row (device AND host error counts)", ok)
l.close()

# 6b. a receive test must not damage the pattern a later send test uses (chip bug: the sink's
#     last read clobbered the start of the pattern buffer, so every later write had 64 bad bytes)
l = link(); ok = True; detail = ""
for first in ("sink", "echo"):
    for chunk in (64, 512, 4096):
        ub.run_one(l, first, 8192 + 101, chunk, False, 10)   # last block does not start on a 256 boundary
        r = ub.run_one(l, "source", 8192, chunk, False, 10)
        if r["host_errors"] or r["errors"]: ok, detail = False, f"{first} then source chunk {chunk}: {r}"
check("source is error-free right after sink and after echo, for chunk 64/512/4096", ok, detail)
l.close()

# 7. the device's own timing is plausible: non-zero for a real transfer and never longer than the
#    host saw. (No lower bound: under qemu the device finishes into a pipe buffer long before the
#    host has read it, and the host time includes starting qemu. On the chip writes block, so the
#    two clocks agree; that is checked on hardware.)
l = link()
r = ub.run_one(l, "source", 2_000_000, 4096, False, 30)
check("device clock is non-zero and not longer than the host's", 0 < r["dev_us"] / 1e6 <= r["host_s"] * 1.05, str(r))
l.close()

# 8. a host that stops sending: the device gives up after its 3 s read timeout, answers
#    status 8 with the bytes it really received (not the bytes requested), and carries on.
import time
for mode, send_back in ((1, 0), (3, 100)):
    l = link()
    raw(l, mode, 1000, 1000)
    l.write(ub.pattern(100), 5)
    if send_back: l.read_exact(send_back, 5)
    t = time.monotonic()
    d = done(l)
    waited = time.monotonic() - t
    check(f"mode {mode}: silent host -> status 8 after about 3 s, bytes = 100 actually received",
          d[1] == 8 and d[2] == 100 and 2.0 < waited < 5.0, f"{d} waited {waited:.1f}s")
    r = ub.run_one(l, "source", 500, 100, False, 5)
    check(f"mode {mode}: the next command works after a timeout", r["bytes"] == 500 and r["errors"] == 0)
    l.close()

# 9. the device's elapsed time runs from the FIRST byte to the last: a host that pauses 0.6 s
#    between two halves must show up in it (and the wait before the first byte must not).
l = link()
raw(l, 1, 8192, 4096)
time.sleep(0.5)                       # idle before the first byte: not part of the transfer
l.write(ub.pattern(4096), 5)
time.sleep(0.6)
l.write(ub.pattern(4096, 4096), 5)
d = done(l)
check("elapsed covers the pause between the halves but not the wait before the first byte",
      d[1] == 0 and 550_000 < d[3] < 900_000, str(d))
l.close()

# 10. the diagnosis of a wrong reply (used when the chip's reply is not a DONE message)
T = 5000
done_msg = struct.pack(ub.DONE_FMT, 0x454E4F44, 0, T, 1, 0, 0, 0, 0)
d1 = ub.diagnose(ub.pattern(32, T), ub.pattern(100, T + 32) + done_msg, T)
check("diagnose: pattern continuing + DONE 132 bytes later is reported as over-sending",
      "CONTINUING" in d1 and "132 extra bytes" in d1 and "pattern-continuing: True" in d1, d1)
d2 = ub.diagnose(ub.pattern(32, T - 32), done_msg, T)
check("diagnose: a repeat of the last 32 payload bytes is reported as duplication", "duplicated" in d2, d2)
d3 = ub.diagnose(b"\x01" * 32, b"", T)
check("diagnose: unrelated bytes with no DONE are reported as such", "no DONE message seen" in d3 and "CONTINUING" not in d3, d3)

print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
