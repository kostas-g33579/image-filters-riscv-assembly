#!/usr/bin/env python3
"""Run the on-chip anonymiser probe and compare it with the emulator.

The probe firmware (pico/probe/anon_probe.c, build-pico/anon_probe.uf2) runs the whole anonymiser pipeline on a
built-in synthetic series and prints, per slice, a status and a checksum of the converted voxels, then the 352-byte
NIfTI header and the JSON sidecar, with the cycle count of every stage. This tool sends it one byte, reads the
result, runs the same files through the emulator harnesses (build32/nifti_test and build32/sidecar_test, which are
the same C code built for the host) and requires every status, every checksum, the header and the JSON to be
identical. If they are, the chip's compiler flags, software floating point and word size change nothing.

usage: sudo python3 pico/tools/anon_probe_check.py [--port DEV] [--transcript FILE] [--timeout S]
       --transcript FILE   compare a saved transcript instead of talking to a board (used by the tests)
"""
import argparse, glob, importlib.util, os, re, select, struct, subprocess, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LABELS = (b"P017", b"T1c", b"S02", b"C01")          # must match anon_probe.c


def load_series(inc=os.path.join(ROOT, "pico", "probe", "probe_series.inc")):
    """The synthetic files, from the generated C include."""
    text = open(inc).read()
    lens = [int(x) for x in re.search(r"probe_lens\[PROBE_N\] = \{([^}]*)\}", text).group(1).split(",")]
    body = text[text.index("probe_series[] = {"):]
    data = bytes(int(x, 16) for x in re.findall(r"0x([0-9a-f]{2})", body))
    files, p = [], 0
    for n in lens:
        files.append(data[p:p + n])
        p += n
    if p != len(data):
        raise SystemExit("probe_series.inc is inconsistent: %d bytes in the files, %d lengths" % (len(data), p))
    return files


def parse(text):
    """The F/S/HEADER lines and the JSON of one run (probe or emulator). Cycle lines are kept apart."""
    d, cycles, lines, in_json = {}, [], text.split("\n"), None
    for line in lines:
        if in_json is not None:
            if line == "JSON_END":
                d["JSON"] = "\n".join(in_json) + "\n"
                in_json = None
            else:
                in_json.append(line)
        elif line == "JSON_BEGIN":
            in_json = []
        elif re.match(r"^C\d+=", line) or line.startswith("FINISH-CYCLES="):
            cycles.append(line)
        elif re.match(r"^(F\d+|S\d+|HEADER|RESULT)=", line):
            k, _, v = line.partition("=")
            d[k] = v
    return d, cycles


def emulator(files, bin_dir=os.path.join(ROOT, "build32")):
    """What the host build of the same C code produces for the same files."""
    out = {}
    blob = struct.pack("<I", len(files)) + b"".join(struct.pack("<I", len(f)) + f for f in files)
    r = subprocess.run(["qemu-riscv32", os.path.join(bin_dir, "nifti_test")], input=blob, capture_output=True, timeout=120)
    out.update(parse(r.stdout.decode())[0])
    labels = b"".join(struct.pack("B", len(x)) + x for x in LABELS)
    r = subprocess.run(["qemu-riscv32", os.path.join(bin_dir, "sidecar_test")], input=labels + blob, capture_output=True, timeout=120)
    side = parse(r.stdout.decode())[0]
    out["JSON"] = side.get("JSON", "")
    return out


def compare(chip, expect):
    """List of differences (empty if identical)."""
    diffs = []
    for k in sorted(set(chip) | set(expect), key=lambda x: (x[0] != "F", x)):
        if chip.get(k) != expect.get(k):
            a, b = str(chip.get(k))[:70], str(expect.get(k))[:70]
            diffs.append(f"{k}: chip {a!r} vs emulator {b!r}")
    return diffs


def read_board(port, timeout):
    spec = importlib.util.spec_from_file_location("pico_filter", os.path.join(ROOT, "pico", "tools", "pico_filter.py"))
    pf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pf)
    port = port or pf.autodetect_port()
    if not port:
        raise SystemExit("no Pico found; pass --port")
    print("board:", pf.describe_device(port))
    link = pf.SerialLink(port)
    link.write(b"x")                                         # any byte starts a run
    buf, end = b"", time.monotonic() + timeout
    while b"PROBE-END" not in buf:
        left = end - time.monotonic()
        if left <= 0:
            raise SystemExit("timed out waiting for PROBE-END; got %d bytes: %r" % (len(buf), buf[-120:]))
        r, _, _ = select.select([link.rfd], [], [], left)
        if r:
            c = os.read(link.rfd, 65536)
            if not c:
                raise SystemExit("link closed")
            buf += c
    link.close()
    return buf.decode("latin-1")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port")
    ap.add_argument("--transcript")
    ap.add_argument("--timeout", type=float, default=20.0)
    a = ap.parse_args()
    files = load_series()
    for b in ("nifti_test", "sidecar_test"):
        if not os.path.exists(os.path.join(ROOT, "build32", b)):
            sys.exit(f"build the emulator harnesses first: make build32/{b}")
    text = open(a.transcript).read() if a.transcript else read_board(a.port, a.timeout)
    chip, cycles = parse(text)
    expect = emulator(files)
    diffs = compare(chip, expect)
    print(f"{len(files)} slices; chip printed {sum(1 for k in chip if k.startswith('S'))} checksums, "
          f"a {len(chip.get('HEADER', '')) // 2}-byte header and {len(chip.get('JSON', ''))} bytes of JSON")
    for line in cycles:
        print("  ", line)
    if diffs:
        print("DIFFERENT from the emulator:")
        for d in diffs[:12]:
            print("  ", d)
        sys.exit(1)
    print("IDENTICAL to the emulator: every status, every voxel checksum, the NIfTI header and the JSON.")


if __name__ == "__main__":
    main()
