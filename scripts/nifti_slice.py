#!/usr/bin/env python3
"""Describe a NIfTI-1 volume and extract one 2-D slice of it as a PGM image.

usage: nifti_slice.py FILE.nii[.gz] [--channel C] [--axis x|y|z] [--index N]
                      [-o SLICE.pgm] [--maxval M] [--raw] [--info-only]

* FILE is NIfTI-1 (.nii or .nii.gz, either byte order). 3-D and 4-D volumes are
  supported; for 4-D the 4th dimension is the "channel" (for BraTS: 0 FLAIR,
  1 T1, 2 T1 post-contrast, 3 T2; check the dataset's own description).
* --axis z (default) is an axial slice, y coronal, x sagittal; --index defaults to
  the middle slice. The picture is oriented so the first voxel row is at the top
  for the usual RAS-style voxel order; this only affects how the picture looks,
  not any filtering.
* The slice is written as a binary PGM. If its values are exact whole numbers in
  0..65535 they are written unchanged (maxval 255, 4095 or 65535, whichever is the
  smallest that fits); otherwise they are rescaled linearly and the tool says so.
* NIfTI's scl_slope / scl_inter are applied unless --raw is given.
* The tool prints a suggested window (centre, width) from the 1st and 99th
  percentile of the foreground, and whether the slice fits the Pico firmware's
  pixel limit (read from pico/src/proto.h).

The input is untrusted: the header is validated before use (size, magic, data
type vs bit width, dimensions, total size, truncation) and nothing in the file is
ever executed. Standard library only. Run it as `python3 -I`.
"""
import argparse
import array
import gzip
import math
import os
import re
import struct
import sys
import zlib

# NIfTI datatype code -> (array typecode, bytes per value, name)
DTYPES = {2: ("B", 1, "uint8"), 4: ("h", 2, "int16"), 8: ("i", 4, "int32"),
          16: ("f", 4, "float32"), 64: ("d", 8, "float64"), 256: ("b", 1, "int8"),
          512: ("H", 2, "uint16"), 768: ("I", 4, "uint32")}
MAX_BYTES = 2 * 1024 ** 3        # refuse to load a single channel larger than this


class NiftiError(Exception):
    pass


def open_any(path):
    with open(path, "rb") as f:
        magic = f.read(2)
    return gzip.open(path, "rb") if magic == b"\x1f\x8b" else open(path, "rb")


def read_header(f):
    h = f.read(348)
    if len(h) < 348:
        raise NiftiError("file is too short to be a NIfTI-1 file (header is 348 bytes)")
    for e in ("<", ">"):
        if struct.unpack(e + "i", h[:4])[0] == 348:
            break
    else:
        if struct.unpack("<i", h[:4])[0] == 540 or struct.unpack(">i", h[:4])[0] == 540:
            raise NiftiError("this is NIfTI-2 (540-byte header); only NIfTI-1 is supported")
        raise NiftiError("not a NIfTI-1 file: the first 4 bytes are not 348 in either byte order")
    if h[344:348] != b"n+1\0":
        raise NiftiError("unsupported NIfTI variant (magic %r); only single-file 'n+1' is supported" % h[344:348])
    dim = struct.unpack(e + "8h", h[40:56])
    ndim = dim[0]
    if not 2 <= ndim <= 5:
        raise NiftiError("unsupported number of dimensions: %d" % ndim)
    dims = list(dim[1:1 + ndim]) + [1] * (5 - ndim)
    if any(d < 1 for d in dims):
        raise NiftiError("invalid dimensions %s" % (dims,))
    if dims[4] != 1:
        raise NiftiError("5-D volumes are not supported (dim[5] = %d)" % dims[4])
    code, bitpix = struct.unpack(e + "2h", h[70:74])
    if code not in DTYPES:
        raise NiftiError("unsupported data type code %d" % code)
    tc, nbytes, tname = DTYPES[code]
    if bitpix != 8 * nbytes:
        raise NiftiError("inconsistent header: data type %s needs bitpix %d, header says %d" % (tname, 8 * nbytes, bitpix))
    pixdim = struct.unpack(e + "8f", h[76:108])
    vox_offset = struct.unpack(e + "f", h[108:112])[0]
    if not (math.isfinite(vox_offset) and 352 <= vox_offset <= 1 << 24 and vox_offset == int(vox_offset)):
        raise NiftiError("implausible vox_offset %r" % vox_offset)
    slope, inter = struct.unpack(e + "2f", h[112:120])
    vol_bytes = dims[0] * dims[1] * dims[2] * nbytes
    if vol_bytes > MAX_BYTES:
        raise NiftiError("one channel would be %d MB; refusing to load more than %d MB" % (vol_bytes >> 20, MAX_BYTES >> 20))
    return {"endian": e, "dims": dims, "ndim": ndim, "typecode": tc, "nbytes": nbytes, "dtype": tname,
            "pixdim": pixdim[1:5], "vox_offset": int(vox_offset), "slope": slope, "inter": inter,
            "descrip": h[148:228].split(b"\0")[0].decode(errors="replace"), "vol_bytes": vol_bytes}


def skip(f, n):
    while n > 0:
        chunk = f.read(min(n, 1 << 22))
        if not chunk:
            raise NiftiError("file is truncated: ran out of data while skipping to the voxels")
        n -= len(chunk)


def read_channel(f, hdr, channel):
    t = hdr["dims"][3]
    if not 0 <= channel < t:
        raise NiftiError("channel %d out of range (this volume has %d)" % (channel, t))
    skip(f, hdr["vox_offset"] - 348 + channel * hdr["vol_bytes"])
    data = bytearray()
    while len(data) < hdr["vol_bytes"]:
        chunk = f.read(min(hdr["vol_bytes"] - len(data), 1 << 22))
        if not chunk:
            raise NiftiError("file is truncated: expected %d bytes of voxels, got %d" % (hdr["vol_bytes"], len(data)))
        data += chunk
    vol = array.array(hdr["typecode"])
    vol.frombytes(bytes(data))
    if (hdr["endian"] == "<") != (sys.byteorder == "little"):
        vol.byteswap()
    return vol


def extract_slice(vol, dims, axis, index):
    """Return (width, height, rows) with rows[0] at the top. Voxel (x, y, z) is vol[x + X*(y + Y*z)]."""
    X, Y, Z = dims[0], dims[1], dims[2]
    size = {"x": X, "y": Y, "z": Z}[axis]
    if not 0 <= index < size:
        raise NiftiError("index %d out of range for axis %s (0..%d)" % (index, axis, size - 1))
    if axis == "z":      # axial: columns = x, rows = y, anterior (high y) at the top
        rows = [list(vol[(index * Y + y) * X:(index * Y + y) * X + X]) for y in range(Y - 1, -1, -1)]
        return X, Y, rows
    if axis == "y":      # coronal: columns = x, rows = z, superior (high z) at the top
        rows = [list(vol[(z * Y + index) * X:(z * Y + index) * X + X]) for z in range(Z - 1, -1, -1)]
        return X, Z, rows
    rows = [[vol[index + X * (y + Y * z)] for y in range(Y)] for z in range(Z - 1, -1, -1)]   # sagittal
    return Y, Z, rows


def percentile(sorted_vals, p):
    return sorted_vals[int(round(p / 100 * (len(sorted_vals) - 1)))]


def device_pixel_limit():
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        text = open(os.path.join(here, "..", "pico", "src", "proto.h")).read()
        return int(re.search(r"#define PROTO_MAX_PIXELS (\d+)u", text).group(1))
    except (OSError, AttributeError):
        return None


def write_pgm(path, w, h, maxval, rows):
    with open(path, "wb") as f:
        f.write(b"P5\n%d %d\n%d\n" % (w, h, maxval))
        for row in rows:
            if maxval < 256:
                f.write(bytes(row))
            else:
                f.write(b"".join(struct.pack(">H", v) for v in row))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--channel", type=int, default=0, help="4th-dimension index (default 0)")
    ap.add_argument("--axis", choices="xyz", default="z", help="z axial (default), y coronal, x sagittal")
    ap.add_argument("--index", type=int, help="slice index along the axis (default: the middle)")
    ap.add_argument("-o", "--output", help="PGM file to write")
    ap.add_argument("--maxval", type=int, help="force the PGM maxval (must hold the largest value)")
    ap.add_argument("--raw", action="store_true", help="ignore scl_slope / scl_inter")
    ap.add_argument("--info-only", action="store_true", help="describe the volume and slice, write nothing")
    a = ap.parse_args()
    if not a.info_only and not a.output:
        ap.error("give -o OUTPUT.pgm, or --info-only")

    try:
        with open_any(a.file) as f:
            hdr = read_header(f)
            vol = read_channel(f, hdr, a.channel)
        X, Y, Z, T = hdr["dims"][:4]
        index = a.index if a.index is not None else {"x": X, "y": Y, "z": Z}[a.axis] // 2
        w, h, rows = extract_slice(vol, hdr["dims"], a.axis, index)
    except NiftiError as e:
        sys.exit("error: %s" % e)
    except (OSError, EOFError, zlib.error) as e:      # unreadable, or a corrupt / cut-off gzip stream
        sys.exit("error: cannot read %s: %s" % (a.file, e))

    print(f"file        : {a.file}")
    print(f"format      : NIfTI-1, {'little' if hdr['endian'] == '<' else 'big'}-endian, {hdr['ndim']}-D, "
          f"dims x={X} y={Y} z={Z}" + (f" channels={T}" if T > 1 else ""))
    print(f"data type   : {hdr['dtype']}   voxel size (mm) {[round(v, 3) for v in hdr['pixdim'][:3]]}"
          f"   descrip {hdr['descrip']!r}")
    scaled = (not a.raw) and hdr["slope"] not in (0.0,) and math.isfinite(hdr["slope"]) \
        and (hdr["slope"], hdr["inter"]) != (1.0, 0.0)
    print(f"scaling     : scl_slope={hdr['slope']:g} scl_inter={hdr['inter']:g}  ->  "
          f"{'applied' if scaled else 'not applied (identity, zero, or --raw)'}")
    print(f"slice       : channel {a.channel}, axis {a.axis} ({'axial' if a.axis == 'z' else 'coronal' if a.axis == 'y' else 'sagittal'}), "
          f"index {index} of 0..{ {'x': X, 'y': Y, 'z': Z}[a.axis] - 1}  ->  {w} x {h} = {w * h} pixels")

    vals = [v for row in rows for v in row]
    if scaled:
        vals = [v * hdr["slope"] + hdr["inter"] for v in vals]
    if not all(math.isfinite(v) for v in vals):
        sys.exit("error: the slice contains NaN or infinite values")
    lo, hi = min(vals), max(vals)
    exact = all(v == int(v) for v in vals) and lo >= 0 and hi <= 65535
    if exact:
        ivals = [int(v) for v in vals]
        need = 255 if hi <= 255 else 4095 if hi <= 4095 else 65535
        maxval = a.maxval or need
        if maxval < hi or maxval > 65535 or maxval < 1:
            sys.exit("error: --maxval %d cannot hold the largest value %d" % (maxval, hi))
        print(f"values      : whole numbers {int(lo)}..{int(hi)}  ->  written unchanged, PGM maxval {maxval}")
    else:
        maxval = a.maxval or 65535
        if not 1 <= maxval <= 65535:
            sys.exit("error: --maxval must be 1..65535")
        span = (hi - lo) or 1.0
        ivals = [int(round((v - lo) / span * maxval)) for v in vals]
        print(f"values      : {lo:g}..{hi:g} are NOT whole numbers in 0..65535  ->  RESCALED linearly to 0..{maxval} "
              f"(out = round((v - {lo:g}) / {span:g} * {maxval})); windows must be converted accordingly")

    fg = sorted(v for v in ivals if v > 0)
    zero_frac = 1 - len(fg) / len(ivals)
    pool = fg if zero_frac >= 0.05 and len(fg) >= 20 else sorted(ivals)
    p1, p50, p99 = percentile(pool, 1), percentile(pool, 50), percentile(pool, 99)
    print(f"statistics  : {100 * zero_frac:.1f}% of pixels are zero; foreground p1={p1} median={p50} p99={p99}")
    center, width = (p1 + p99) // 2, max(1, p99 - p1)
    print(f"suggested   : window {center} {width}   (centre and width from foreground p1..p99; MRI units are arbitrary)")
    limit = device_pixel_limit()
    if limit:
        print(f"Pico limit  : {w * h} pixels vs the firmware's {limit}: "
              + ("FITS the current whole-image firmware" if w * h <= limit else "TOO BIG for the current whole-image firmware (needs streaming)"))

    if not a.info_only:
        write_pgm(a.output, w, h, maxval, [ivals[r * w:(r + 1) * w] for r in range(h)])
        print(f"wrote       : {a.output}  ({w} x {h}, maxval {maxval})")


if __name__ == "__main__":
    main()
