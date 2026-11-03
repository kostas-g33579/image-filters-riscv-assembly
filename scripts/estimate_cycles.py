#!/usr/bin/env python3
"""Estimate Hazard3 cycles spent in filter_core for an RV32 build.

QEMU doesn't model cycles, so this runs the binary under qemu-riscv32 with a
one-instruction-per-block execution trace, then charges each executed
instruction inside filter_core the cycle cost from the RP2350 datasheet
(section 3.8.7): ALU / multiply 1, divide 18-19, taken branch or jump 2,
not-taken branch 1, load 2 (the pessimistic case; 1 if the next instruction
is independent), store 1.

It is an estimate: it ignores pipeline and bus effects the real chip has, so
use it to compare two versions of the code, not as an absolute figure.

usage: scripts/estimate_cycles.py BINARY FILTER [--qemu-args "-cpu rv32,zbb=true"]
                                  [--size 32] [--passes 1]
The binary must be an RV32 build WITHOUT the C extension (the default
`make rv32` / `make rv32zbb` builds), so every instruction is 4 bytes.
"""
import argparse, os, random, re, shlex, subprocess, sys, tempfile

TOOL = "riscv64-linux-gnu-"
BRANCHES = {"beq", "bne", "blt", "bge", "bltu", "bgeu", "beqz", "bnez", "blez",
            "bgez", "bltz", "bgtz", "ble", "bgt", "bleu", "bgtu"}
JUMPS = {"jal", "jalr", "j", "ret", "jr"}
LOADS = {"lw", "lh", "lhu", "lb", "lbu"}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("binary"); ap.add_argument("filter")
    ap.add_argument("--qemu-args", default="")
    ap.add_argument("--size", type=int, default=32, help="square test image side")
    ap.add_argument("--passes", type=int, default=1)
    a = ap.parse_args()

    dis = {}
    for line in subprocess.run([TOOL + "objdump", "-d", a.binary], capture_output=True, text=True).stdout.splitlines():
        m = re.match(r"\s*([0-9a-f]+):\s+([0-9a-f]+)\s+(\S+)", line)
        if m:
            if len(m.group(2)) != 8:
                sys.exit("binary contains 16-bit (compressed) instructions; build without the C extension")
            dis[int(m.group(1), 16)] = m.group(3)
    syms = {}
    for line in subprocess.run([TOOL + "nm", "-n", a.binary], capture_output=True, text=True).stdout.splitlines():
        p = line.split()
        if len(p) == 3: syms[p[2]] = int(p[0], 16)
    lo, hi = syms["filter_core"], syms["window_level"]

    with tempfile.TemporaryDirectory() as d:
        os.mkdir(os.path.join(d, "images"))
        n = a.size
        rng = random.Random(5)
        with open(os.path.join(d, "images", "input.pgm"), "wb") as f:
            f.write(f"P5\n{n} {n}\n255\n".encode() + bytes(rng.randrange(256) for _ in range(n * n)))
        trace = os.path.join(d, "trace.txt")
        subprocess.run(["qemu-riscv32", *shlex.split(a.qemu_args), "-one-insn-per-tb", "-d", "exec,nochain",
                        "-D", trace, os.path.abspath(a.binary), a.filter, str(a.passes)],
                       cwd=d, check=True, capture_output=True)
        pcs = []
        for l in open(trace):
            m = re.search(r"/([0-9a-f]{8,16})/", l) if l.startswith("Trace") else None
            if m: pcs.append(int(m.group(1), 16))

    cyc = n_ins = divs = muls = 0
    for i, pc in enumerate(pcs):
        if not (lo <= pc < hi): continue
        op = dis.get(pc, "?"); n_ins += 1
        taken = (pcs[i + 1] if i + 1 < len(pcs) else pc + 4) != pc + 4
        if op in ("div", "rem"): c = 19; divs += 1
        elif op in ("divu", "remu"): c = 18; divs += 1
        elif op in JUMPS: c = 2
        elif op in BRANCHES: c = 2 if taken else 1
        elif op in LOADS: c = 2
        else: c = 1
        if op in ("mul", "mulhu"): muls += 1
        cyc += c
    px = n * n * a.passes
    print(f"{a.filter}: {n_ins} instructions, ~{cyc} cycles in filter_core "
          f"for {px} pixel-passes = ~{cyc / px:.1f} cycles/pixel "
          f"({divs} divide, {muls} multiply instructions executed)")

main()
