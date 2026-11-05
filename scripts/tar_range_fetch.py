#!/usr/bin/env python3
"""Fetch chosen members out of a huge plain (uncompressed) .tar over HTTP with Range
requests, without downloading the archive. Walks the 512-byte tar headers (one tiny
request each) and downloads only the wanted members. Read-only, anonymous, standard
library only.

usage: scripts/tar_range_fetch.py URL OUTDIR NAME_SUFFIX [NAME_SUFFIX ...]

Example: one BraTS case from the openly licensed Medical Segmentation Decathlon
(CC-BY-SA 4.0), about 10 MB out of a 7.6 GB archive. The walk takes a few minutes
because every header is a separate request:

  python3 -I scripts/tar_range_fetch.py \
      https://msd-for-monai.s3.us-west-2.amazonaws.com/Task01_BrainTumour.tar \
      data/msd_task01_sample imagesTr/BRATS_001.nii.gz dataset.json

Downloaded files are untrusted input: keep them in their own folder (data/ is
git-ignored, and CC-BY-SA data should not be committed here) and read them with
`python3 -I`. A suffix such as "dataset.json" also matches the macOS sidecar
"._dataset.json"; delete it afterwards.

Only plain tars work (ustar and GNU long names); a .tar.gz cannot be range-read.
"""
import os
import sys
import time
import urllib.request

URL, OUTDIR = sys.argv[1], sys.argv[2]
WANT = sys.argv[3:]            # name suffixes to download
os.makedirs(OUTDIR, exist_ok=True)


def rng(a, b):
    req = urllib.request.Request(URL, headers={"Range": f"bytes={a}-{b}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def parse_size(f):
    if f[0] & 0x80:                                   # GNU base-256 encoding
        return int.from_bytes(bytes([f[0] & 0x7f]) + f[1:], "big")
    return int(f.split(b"\0")[0].split(b" ")[0] or b"0", 8)


off, n, found, longname, t0 = 0, 0, set(), None, time.time()
while len(found) < len(WANT) and n < 4000:
    h = rng(off, off + 511)
    n += 1
    if len(h) < 512 or h == b"\0" * 512:
        print("end of archive")
        break
    name = h[:100].split(b"\0")[0].decode(errors="replace")
    pre = h[345:500].split(b"\0")[0].decode(errors="replace")
    if pre and h[257:262] == b"ustar":
        name = pre + "/" + name
    size, typ = parse_size(h[124:136]), chr(h[156]) if h[156] else "0"
    data_off = off + 512
    if typ == "L":                                     # GNU long file name entry
        longname = rng(data_off, data_off + size - 1).split(b"\0")[0].decode(errors="replace")
        off = data_off + ((size + 511) // 512) * 512
        continue
    if longname:
        name, longname = longname, None
    if n <= 12 or any(name.endswith(w) for w in WANT):
        print(f"  header {n:4d} at {off:>12d}: {name}  ({size} bytes, type {typ})")
    for w in WANT:
        if name.endswith(w) and typ in "0\0":
            dest = os.path.join(OUTDIR, os.path.basename(name))
            with open(dest, "wb") as f:
                pos = data_off
                while pos < data_off + size:
                    end = min(pos + 4_000_000, data_off + size) - 1
                    f.write(rng(pos, end))
                    pos = end + 1
            print(f"  -> saved {dest} ({size} bytes)")
            found.add(w)
    off = data_off + ((size + 511) // 512) * 512
print(f"walked {n} headers in {time.time() - t0:.1f}s; found {sorted(found)} of {WANT}")
