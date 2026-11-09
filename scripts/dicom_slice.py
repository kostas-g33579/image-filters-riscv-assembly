#!/usr/bin/env python3
"""Describe a DICOM image or series and extract one slice as a PGM image.

usage: dicom_slice.py PATH [--series K] [--index N] [-o SLICE.pgm] [--maxval M]
                      [--raw] [--info-only]

* PATH is one DICOM file or a folder holding a series (one file per slice).
  In a folder, files without the DICOM preamble (for example a LICENSE file) are
  skipped and counted. If the folder holds several series, the tool lists them and
  you pick one with --series (1 = the largest).
* Slices are ordered by their position along the slice normal (from Image Position
  and Image Orientation (Patient)); without those, by Instance Number. The tool says
  which, and warns about duplicate positions and uneven spacing. --index counts
  from 0 in that order and defaults to the middle slice.
* Supported: uncompressed pixel data, Implicit VR Little Endian and Explicit VR
  Little Endian, one frame, one sample per pixel (greyscale), 8 or 16 bits
  allocated, signed or unsigned, MONOCHROME1 (inverted to MONOCHROME2) and
  MONOCHROME2. Anything else (compressed JPEG / JPEG 2000 / RLE, deflate, big endian,
  colour, multi-frame) is refused with a message naming what it is. Nothing is guessed.
* The file's Rescale Slope / Intercept are applied unless --raw is given. If the
  result has negative values it is shifted up by a constant (1024 when that is
  enough, the usual CT convention, otherwise by the minimum) so that it fits an
  unsigned PGM; whole numbers are written unchanged, anything else is rescaled
  linearly. The tool says which, and converts the file's own Window Center / Width
  to the same units, so `window C W` can be passed straight to the Pico client.
* PRIVACY: only a short list of technical tags is ever read into memory; patient
  name, ID, dates, sex, age and the like are never stored, printed or copied. The tool
  names which identifying fields are present and warns about burned-in annotation, so
  you know a file is not safe to share, but it never shows their values.

The input is untrusted: every length is checked against the file, nesting and
element counts are limited, and nothing in a file is executed. Standard library
only. Run it as `python3 -I`.
"""
import argparse
import importlib.util
import math
import os
import re
import struct
import sys

MAX_FILE = 256 * 1024 ** 2          # refuse a single file larger than this
MAX_PIXELS = 100_000_000
MAX_ELEMENTS = 2_000_000            # per file, counting nested ones
MAX_DEPTH = 16                      # nesting of sequences
MAX_FILES = 20000                   # in one folder

TS_IMPLICIT, TS_EXPLICIT = "1.2.840.10008.1.2", "1.2.840.10008.1.2.1"
TS_REFUSED = {
    "1.2.840.10008.1.2.1.99": "Deflated Explicit VR Little Endian (deflate-compressed data set)",
    "1.2.840.10008.1.2.2": "Explicit VR Big Endian (retired)",
    "1.2.840.10008.1.2.5": "RLE Lossless (compressed pixel data)",
    "1.2.840.10008.1.2.4.50": "JPEG Baseline (lossy compressed pixel data)",
    "1.2.840.10008.1.2.4.51": "JPEG Extended (lossy compressed pixel data)",
    "1.2.840.10008.1.2.4.57": "JPEG Lossless (compressed pixel data)",
    "1.2.840.10008.1.2.4.70": "JPEG Lossless SV1 (compressed pixel data)",
    "1.2.840.10008.1.2.4.80": "JPEG-LS Lossless (compressed pixel data)",
    "1.2.840.10008.1.2.4.81": "JPEG-LS Near-Lossless (compressed pixel data)",
    "1.2.840.10008.1.2.4.90": "JPEG 2000 Lossless (compressed pixel data)",
    "1.2.840.10008.1.2.4.91": "JPEG 2000 (compressed pixel data)",
}

# The ONLY tags whose values are read. (group, element) -> (name, implicit-VR type)
# types: US unsigned short, DS decimal string(s), IS integer string(s), CS/UI/LO text
WANTED = {
    (0x0002, 0x0010): ("TransferSyntax", "UI"),
    (0x0008, 0x0008): ("ImageType", "CS"),
    (0x0008, 0x0016): ("SOPClass", "UI"),
    (0x0008, 0x0060): ("Modality", "CS"),
    (0x0008, 0x0070): ("Manufacturer", "LO"),
    (0x0008, 0x1090): ("ManufacturerModelName", "LO"),
    (0x0012, 0x0062): ("PatientIdentityRemoved", "CS"),
    (0x0018, 0x0010): ("ContrastBolusAgent", "LO"),      # only "declared or not" is ever used
    (0x0018, 0x0020): ("ScanningSequence", "CS"),
    (0x0018, 0x0021): ("SequenceVariant", "CS"),
    (0x0018, 0x0023): ("MRAcquisitionType", "CS"),
    (0x0018, 0x0050): ("SliceThickness", "DS"),
    (0x0018, 0x0080): ("RepetitionTime", "DS"),
    (0x0018, 0x0081): ("EchoTime", "DS"),
    (0x0018, 0x0082): ("InversionTime", "DS"),
    (0x0018, 0x0087): ("MagneticFieldStrength", "DS"),
    (0x0018, 0x1020): ("SoftwareVersions", "LO"),
    (0x0018, 0x1030): ("ProtocolName", "LO"),
    (0x0018, 0x1314): ("FlipAngle", "DS"),
    (0x0020, 0x000E): ("SeriesInstanceUID", "UI"),     # grouping only; never printed
    (0x0020, 0x0013): ("InstanceNumber", "IS"),
    (0x0020, 0x0032): ("ImagePositionPatient", "DS"),
    (0x0020, 0x0037): ("ImageOrientationPatient", "DS"),
    (0x0028, 0x0002): ("SamplesPerPixel", "US"),
    (0x0028, 0x0004): ("Photometric", "CS"),
    (0x0028, 0x0006): ("PlanarConfiguration", "US"),
    (0x0028, 0x0008): ("NumberOfFrames", "IS"),
    (0x0028, 0x0010): ("Rows", "US"),
    (0x0028, 0x0011): ("Columns", "US"),
    (0x0028, 0x0030): ("PixelSpacing", "DS"),
    (0x0028, 0x0100): ("BitsAllocated", "US"),
    (0x0028, 0x0101): ("BitsStored", "US"),
    (0x0028, 0x0102): ("HighBit", "US"),
    (0x0028, 0x0103): ("PixelRepresentation", "US"),
    (0x0028, 0x0301): ("BurnedInAnnotation", "CS"),
    (0x0028, 0x1050): ("WindowCenter", "DS"),
    (0x0028, 0x1051): ("WindowWidth", "DS"),
    (0x0028, 0x1052): ("RescaleIntercept", "DS"),
    (0x0028, 0x1053): ("RescaleSlope", "DS"),
}
# Present-or-not only. The VALUES of these are never read.
IDENTIFYING = {
    (0x0008, 0x0020): "StudyDate", (0x0008, 0x0021): "SeriesDate", (0x0008, 0x0022): "AcquisitionDate",
    (0x0008, 0x0023): "ContentDate", (0x0008, 0x0050): "AccessionNumber", (0x0008, 0x0080): "InstitutionName",
    (0x0008, 0x0081): "InstitutionAddress", (0x0008, 0x0090): "ReferringPhysicianName",
    (0x0008, 0x1030): "StudyDescription", (0x0008, 0x1050): "PerformingPhysicianName",
    (0x0008, 0x1070): "OperatorsName", (0x0008, 0x1010): "StationName", (0x0018, 0x1000): "DeviceSerialNumber",
    (0x0008, 0x103E): "SeriesDescription", (0x0010, 0x0010): "PatientName", (0x0010, 0x0020): "PatientID",
    (0x0010, 0x0030): "PatientBirthDate", (0x0010, 0x0040): "PatientSex", (0x0010, 0x1010): "PatientAge",
    (0x0010, 0x1020): "PatientSize", (0x0010, 0x1030): "PatientWeight", (0x0010, 0x1040): "PatientAddress",
    (0x0010, 0x2154): "PatientTelephoneNumbers", (0x0010, 0x4000): "PatientComments",
}
LONG_VR = {b"OB", b"OW", b"OF", b"SQ", b"UT", b"UN", b"OD", b"OL", b"UC", b"UR", b"OV", b"SV", b"UV"}
KNOWN_VR = LONG_VR | {b"AE", b"AS", b"AT", b"CS", b"DA", b"DS", b"DT", b"FL", b"FD", b"IS", b"LO", b"LT", b"PN",
                      b"SH", b"SL", b"SS", b"ST", b"TM", b"UI", b"UL", b"US"}


class DicomError(Exception):
    pass


class Image:
    """What the tool keeps from one file. No identifying values are ever stored here."""
    def __init__(self):
        self.tags = {}
        self.identifying = set()
        self.pix_off = self.pix_len = None
        self.explicit = True
        self.path = ""


_WS = " \t\n\x0b\x0c\r"                     # the only whitespace that is ever stripped (ASCII)
_DS_RE = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,3})?\Z")
_IS_RE = re.compile(r"[+-]?[0-9]{1,12}\Z")


def _text(v):
    return v.split(b"\0")[0].rstrip(b" \0").decode("latin-1")


def _ds_value(x):
    """One decimal string: strict grammar, at most 18 mantissa digits. No nan, inf, '1_0', unicode digits."""
    x = x.strip(_WS)
    m = _DS_RE.match(x)
    if not m or len(x) > 64 or sum(c.isdigit() for c in x.split("e")[0].split("E")[0]) > 18:
        raise ValueError("bad decimal string")
    return float(x)


def _decode(v, kind, where):
    try:
        if kind == "US":
            if len(v) < 2:
                raise ValueError("empty")
            return struct.unpack_from("<H", v)[0]
        s = _text(v)
        if kind == "DS":
            return [_ds_value(x) for x in s.split("\\") if x.strip(_WS)]
        if kind == "IS":
            parts = [x for x in s.split("\\") if x.strip(_WS)]
            if not parts:
                return None
            if not _IS_RE.match(parts[0].strip(_WS)):
                raise ValueError("bad integer string")
            return int(parts[0].strip(_WS))
        return s.strip(_WS)
    except (ValueError, struct.error):
        raise DicomError("%s: value of an expected tag is malformed" % where)


def _read_element(b, p, explicit, end, where):
    """Header of the element at p. Returns (group, element, vr, length, value_offset)."""
    if p + 8 > end:
        raise DicomError("%s: file ends inside an element header" % where)
    g, e = struct.unpack_from("<HH", b, p)
    if g == 0xFFFE:                                    # item / delimiter: always implicit-style, 8 bytes
        return g, e, b"", struct.unpack_from("<I", b, p + 4)[0], p + 8
    if explicit:
        vr = bytes(b[p + 4:p + 6])
        if vr not in KNOWN_VR:
            raise DicomError("%s: unknown value representation %r at offset %d" % (where, vr, p))
        if vr in LONG_VR:
            if p + 12 > end:
                raise DicomError("%s: file ends inside an element header" % where)
            return g, e, vr, struct.unpack_from("<I", b, p + 8)[0], p + 12
        return g, e, vr, struct.unpack_from("<H", b, p + 6)[0], p + 8
    return g, e, b"", struct.unpack_from("<I", b, p + 4)[0], p + 8


def _skip_undefined(b, p, explicit, end, depth, counter, where):
    """p is just after an undefined-length element header. Skip items to the sequence delimiter."""
    if depth > MAX_DEPTH:
        raise DicomError("%s: sequences nested deeper than %d" % (where, MAX_DEPTH))
    while True:
        counter[0] += 1
        if counter[0] > MAX_ELEMENTS:
            raise DicomError("%s: too many elements" % where)
        g, e, _, ln, q = _read_element(b, p, False, end, where)
        if (g, e) == (0xFFFE, 0xE0DD):                 # sequence delimiter
            return q
        if (g, e) != (0xFFFE, 0xE000):
            raise DicomError("%s: expected a sequence item at offset %d" % (where, p))
        if ln == 0xFFFFFFFF:                           # item of undefined length: elements until item delimiter
            p = q
            while True:
                counter[0] += 1
                if counter[0] > MAX_ELEMENTS:
                    raise DicomError("%s: too many elements" % where)
                g2, e2, vr2, ln2, q2 = _read_element(b, p, explicit, end, where)
                if (g2, e2) == (0xFFFE, 0xE00D):
                    p = q2
                    break
                if ln2 == 0xFFFFFFFF:
                    p = _skip_undefined(b, q2, explicit, end, depth + 1, counter, where)
                else:
                    if q2 + ln2 > end:
                        raise DicomError("%s: an element runs past the end of the file" % where)
                    p = q2 + ln2
        else:
            if q + ln > end:
                raise DicomError("%s: a sequence item runs past the end of the file" % where)
            p = q + ln


def parse(b, where="file", total=None):
    """Parse one DICOM file held in `b`. Returns an Image.

    `total` (default len(b)) is the length of the whole file when only its first len(b) bytes are given, as on the
    device, which never holds a whole file: the pixel data need not be present, the check that it lies inside the
    file uses `total`, and a header that does not end within `b` is refused as HEADER_TOO_LARGE."""
    total = len(b) if total is None else total
    try:
        return _parse(b, where, total)
    except DicomError as e:
        m = str(e)
        cut_by_end = ("file ends inside an element header" in m or "only meta information" in m or "no pixel data" in m
                      or ("runs past the end" in m and "pixel data runs past" not in m))
        if len(b) < total and cut_by_end:
            raise DicomError("%s: the header does not end within the first %d bytes of the file" % (where, len(b)))
        raise


def _parse(b, where, total):
    if len(b) < 132 or b[128:132] != b"DICM":
        raise DicomError("%s: no DICOM preamble ('DICM' at byte 128); not a DICOM Part 10 file" % where)
    img = Image()
    end = len(b)
    p = 132
    counter = [0]
    in_meta = True
    explicit = True
    while p < end:
        counter[0] += 1
        if counter[0] > MAX_ELEMENTS:
            raise DicomError("%s: too many elements" % where)
        if in_meta and p + 2 <= end and struct.unpack_from("<H", b, p)[0] != 2:
            in_meta = False
            ts = img.tags.get("TransferSyntax")
            if ts is None:
                raise DicomError("%s: no Transfer Syntax UID in the file meta information" % where)
            if ts in TS_REFUSED:
                raise DicomError("%s: transfer syntax %s = %s is not supported (only uncompressed Implicit/Explicit VR "
                                 "Little Endian)" % (where, ts, TS_REFUSED[ts]))
            if ts not in (TS_IMPLICIT, TS_EXPLICIT):
                raise DicomError("%s: transfer syntax %s is not supported (only uncompressed Implicit/Explicit VR "
                                 "Little Endian); it is probably a compressed format" % (where, ts))
            explicit = ts == TS_EXPLICIT
            img.explicit = explicit
        g, e, vr, ln, q = _read_element(b, p, explicit or in_meta, end, where)
        key = (g, e)
        if key == (0x7FE0, 0x0010):
            if ln == 0xFFFFFFFF:
                raise DicomError("%s: pixel data is encapsulated (compressed); not supported" % where)
            if q + ln > total:
                raise DicomError("%s: pixel data runs past the end of the file (truncated file?): needs %d bytes at "
                                 "offset %d, file has %d" % (where, ln, q, total))
            img.pix_off, img.pix_len = q, ln
            break
        if ln == 0xFFFFFFFF:
            p = _skip_undefined(b, q, explicit or in_meta, end, 1, counter, where)
            continue
        if q + ln > end:
            raise DicomError("%s: element (%04X,%04X) runs past the end of the file" % (where, g, e))
        if key in WANTED:
            name, kind = WANTED[key]
            if (g == 2 or explicit) and vr and name not in ("PatientIdentityRemoved",):
                want_vr = {"US": b"US", "DS": b"DS", "IS": b"IS"}.get(kind)
                if want_vr and vr != want_vr:
                    raise DicomError("%s: %s has value representation %s, expected %s"
                                     % (where, name, vr.decode("latin-1"), want_vr.decode()))
            img.tags[name] = _decode(b[q:q + ln], kind, where)
        elif key in IDENTIFYING and ln:
            img.identifying.add(IDENTIFYING[key])      # presence only; the value is not read
        p = q + ln
    if in_meta:
        raise DicomError("%s: the file has only meta information, no data set" % where)
    if img.pix_off is None:
        raise DicomError("%s: no pixel data (not an image, or the pixel data is not in the standard place)" % where)
    return img


def check_image(img, where):
    """Validate the pixel-related tags and return (rows, cols, bits_alloc, bits_stored, signed, shift)."""
    t = img.tags
    for need in ("Rows", "Columns", "BitsAllocated", "PixelRepresentation"):
        if t.get(need) is None:
            raise DicomError("%s: required tag %s is missing" % (where, need))
    if t.get("SamplesPerPixel", 1) != 1:
        raise DicomError("%s: SamplesPerPixel=%s: colour images are not supported (greyscale only)"
                         % (where, t.get("SamplesPerPixel")))
    ph = t.get("Photometric")
    if ph not in ("MONOCHROME1", "MONOCHROME2"):
        raise DicomError("%s: PhotometricInterpretation %r is not supported (only MONOCHROME1 / MONOCHROME2)"
                         % (where, ph))
    nf = t.get("NumberOfFrames")
    if nf not in (None, 1):
        raise DicomError("%s: %s frames in one file: multi-frame images are not supported" % (where, nf))
    rows, cols = t["Rows"], t["Columns"]
    if rows < 1 or cols < 1 or rows * cols > MAX_PIXELS:
        raise DicomError("%s: image size %d x %d is outside 1..%d pixels" % (where, cols, rows, MAX_PIXELS))
    ba = t["BitsAllocated"]
    if ba not in (8, 16):
        raise DicomError("%s: BitsAllocated=%s is not supported (8 or 16)" % (where, ba))
    bs = t.get("BitsStored", ba)
    hb = t.get("HighBit", bs - 1)
    if not (1 <= bs <= ba) or hb < bs - 1 or hb > ba - 1:
        raise DicomError("%s: inconsistent BitsAllocated/BitsStored/HighBit = %s/%s/%s" % (where, ba, bs, hb))
    pr = t["PixelRepresentation"]
    if pr not in (0, 1):
        raise DicomError("%s: PixelRepresentation=%s is invalid" % (where, pr))
    need = rows * cols * (ba // 8)
    if img.pix_len < need:
        raise DicomError("%s: pixel data holds %d bytes but %d x %d x %d bits needs %d"
                         % (where, img.pix_len, cols, rows, ba, need))
    return rows, cols, ba, bs, bool(pr), hb + 1 - bs


def decode_pixels(b, img, where="file"):
    """The stored values of one file, as a list of ints (sign-extended, inverted if MONOCHROME1)."""
    rows, cols, ba, bs, signed, shift = check_image(img, where)
    n = rows * cols
    raw = b[img.pix_off:img.pix_off + n * (ba // 8)]
    if ba == 8:
        vals = list(raw)
    else:
        vals = list(struct.unpack("<%dH" % n, raw))
    mask = (1 << bs) - 1
    if shift or bs < ba:
        vals = [(v >> shift) & mask for v in vals]
    if signed:
        sign, full = 1 << (bs - 1), 1 << bs
        vals = [v - full if v & sign else v for v in vals]
    if img.tags.get("Photometric") == "MONOCHROME1":
        top = (1 << bs) - 1
        vals = [(-1 - v) if signed else (top - v) for v in vals]
    return rows, cols, vals


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def order_slices(images):
    """Sort Images into slice order. Returns (sorted list, method text, positions or None, warnings)."""
    warns = []
    geo = all(len(i.tags.get("ImagePositionPatient") or []) == 3 and len(i.tags.get("ImageOrientationPatient") or []) == 6
              for i in images)
    if geo:
        iop = images[0].tags["ImageOrientationPatient"]
        if any(max(abs(x - y) for x, y in zip(i.tags["ImageOrientationPatient"], iop)) > 1e-3 for i in images):
            warns.append("slices have different Image Orientation; ordering by the first slice's normal may be wrong")
        normal = _cross(iop[:3], iop[3:])
        norm = math.sqrt(_dot(normal, normal))
        if norm < 0.5:
            geo = False
            warns.append("Image Orientation vectors are degenerate; falling back to Instance Number")
        else:
            normal = tuple(x / norm for x in normal)
            pos = [(_dot(i.tags["ImagePositionPatient"], normal), n) for n, i in enumerate(images)]
            pos.sort()
            order = [n for _, n in pos]
            return [images[n] for n in order], "position along the slice normal (Image Position/Orientation)", \
                [p for p, _ in pos], warns
    inst = [i.tags.get("InstanceNumber") for i in images]
    if any(v is None for v in inst):
        if len(images) > 1:
            warns.append("no usable position and no Instance Number on every slice; kept the file-name order")
        return list(images), "file-name order (no geometry, no Instance Number)", None, warns
    order = sorted(range(len(images)), key=lambda n: (inst[n], n))
    return [images[n] for n in order], "Instance Number (no usable Image Position/Orientation)", None, warns


# ---------------------------------------------------------------------------- stack geometry
# The geometry of a series of slices, worked out slice by slice the way the Pico does it (the C version in
# pico/src/geom.c does the same operations in the same order, so the two agree to the last bit).
# DICOM geometry: ImageOrientationPatient = (row direction r, column direction c) as unit vectors in the
# patient's left-posterior-superior (LPS) frame; ImagePositionPatient = position of the centre of the first
# pixel; PixelSpacing = (distance between rows, distance between columns). A pixel (column i, row j) of a
# slice sits at  IPP + i*ps_col*r + j*ps_row*c .  The slice-to-slice step vector is taken from the positions.
ORTHO_TOL = 1e-3          # |r|^2, |c|^2 within 2e-3 of 1 and |r.c| within 1e-3 of 0
IOP_SAME_TOL = 1e-4       # every slice must have the first slice's orientation, per component
STACK_TOL = 0.01          # a slice may be off its place in the regular stack by 1% of the step ...
STACK_TOL_MM = 1e-4       # ... plus this many mm
GEOM_ERRORS = ("NO_GEOMETRY", "ORIENTATION", "MISMATCH", "SERIES", "NOT_SORTED", "UNEVEN")


class GeomError(Exception):
    def __init__(self, cls):
        self.cls = cls
        super().__init__(cls)


def _dot3(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross3(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


class Stack:
    """add() every slice IN ORDER (ascending along the slice normal), then finish()."""

    def __init__(self):
        self.count = 0

    def add(self, img):
        t = img.tags
        ps, ipp, iop = t.get("PixelSpacing"), t.get("ImagePositionPatient"), t.get("ImageOrientationPatient")
        if not ps or not ipp or not iop or len(ps) != 2 or len(ipp) != 3 or len(iop) != 6:
            raise GeomError("NO_GEOMETRY")
        uid = t.get("SeriesInstanceUID") or ""
        rows, cols = t["Rows"], t["Columns"]
        if self.count == 0:
            if len(uid) > 64 or not ps[0] > 0 or not ps[1] > 0:
                raise GeomError("SERIES" if len(uid) > 64 else "NO_GEOMETRY")
            r, c = tuple(iop[0:3]), tuple(iop[3:6])
            if abs(_dot3(r, r) - 1) > 2 * ORTHO_TOL or abs(_dot3(c, c) - 1) > 2 * ORTHO_TOL or abs(_dot3(r, c)) > ORTHO_TOL:
                raise GeomError("ORIENTATION")
            self.rows, self.cols, self.ps, self.uid, self.iop = rows, cols, tuple(ps), uid, tuple(iop)
            self.r, self.c, self.n = r, c, _cross3(r, c)
            self.ipp0 = tuple(ipp)
            self.t_prev = _dot3(self.ipp0, self.n)
            self.thickness = (t.get("SliceThickness") or [None])[0]
            self.step = None
        else:
            if uid != self.uid:
                raise GeomError("SERIES")
            if rows != self.rows or cols != self.cols or tuple(ps) != self.ps:
                raise GeomError("MISMATCH")
            for a, b in zip(iop, self.iop):
                if abs(a - b) > IOP_SAME_TOL:
                    raise GeomError("MISMATCH")
            tpos = _dot3(ipp, self.n)
            if not tpos > self.t_prev:
                raise GeomError("NOT_SORTED")
            if self.count == 1:
                self.step = (ipp[0] - self.ipp0[0], ipp[1] - self.ipp0[1], ipp[2] - self.ipp0[2])
                self.step_len = _sqrt(_dot3(self.step, self.step))
            else:
                k = float(self.count)
                tol = STACK_TOL * self.step_len + STACK_TOL_MM
                for a in range(3):
                    if abs(ipp[a] - (self.ipp0[a] + k * self.step[a])) > tol:
                        raise GeomError("UNEVEN")
            self.t_prev = tpos
        self.count += 1

    def finish(self):
        if self.count == 0:
            raise GeomError("NO_GEOMETRY")
        thickness_assumed = 0
        if self.count == 1:
            L = self.thickness if (self.thickness is not None and self.thickness > 0) else 1.0
            thickness_assumed = 1
            self.step = (self.n[0] * L, self.n[1] * L, self.n[2] * L)
            self.step_len = L
        ps_row, ps_col = self.ps
        r, c, s, o = self.r, self.c, self.step, self.ipp0
        # voxel (i, j, k) -> position, converted from LPS to NIfTI's RAS by negating x and y
        affine = [-(ps_col * r[0]), -(ps_row * c[0]), -s[0], -o[0],
                  -(ps_col * r[1]), -(ps_row * c[1]), -s[1], -o[1],
                  ps_col * r[2], ps_row * c[2], s[2], o[2]]
        ax, ay, az = abs(self.n[0]), abs(self.n[1]), abs(self.n[2])
        plane = 0 if (ax > ay and ax > az) else (1 if ay > az else 2)          # 0 sagittal, 1 coronal, 2 axial
        mismatch = 0
        if self.count > 1 and self.thickness is not None:
            mismatch = int(abs(self.thickness - self.step_len) > STACK_TOL * self.step_len)
        return {"cols": self.cols, "rows": self.rows, "count": self.count, "pixdim": (ps_col, ps_row, self.step_len),
                "affine": affine, "plane": plane, "thickness_assumed": thickness_assumed, "thickness_mismatch": mismatch}


def _sqrt(x):
    """Newton's method, the same iteration the C version runs (the chip has no libm to lean on)."""
    if not x > 0:
        return 0.0
    bits = struct.unpack("<Q", struct.pack("<d", x))[0]
    g = struct.unpack("<d", struct.pack("<Q", (bits >> 1) + (1023 << 51)))[0]
    for _ in range(6):
        g = 0.5 * (g + x / g)
    return g


def device_pixel_limit():
    try:
        s = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pico", "src", "proto.h")).read()
        import re
        return int(re.search(r"#define PROTO_MAX_PIXELS (\d+)u", s).group(1))
    except (OSError, AttributeError, ValueError):
        return None


_NIFTI = []


def _nifti_helpers():
    """percentile() and write_pgm() are shared with the NIfTI tool (loaded once)."""
    if not _NIFTI:
        spec = importlib.util.spec_from_file_location(
            "nifti_slice", os.path.join(os.path.dirname(os.path.abspath(__file__)), "nifti_slice.py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        _NIFTI.append(m)
    return _NIFTI[0]


def load_series(path):
    """Returns (list of (filename, Image, bytes-free), skipped_count). Images keep offsets only."""
    if os.path.isdir(path):
        names = sorted(n for n in os.listdir(path) if os.path.isfile(os.path.join(path, n)))
        if len(names) > MAX_FILES:
            raise DicomError("%s: more than %d files in the folder" % (path, MAX_FILES))
        paths = [os.path.join(path, n) for n in names]
    else:
        paths = [path]
    out, skipped = [], 0
    for fp in paths:
        size = os.path.getsize(fp)
        if size > MAX_FILE:
            raise DicomError("%s: file is larger than %d MB" % (fp, MAX_FILE >> 20))
        with open(fp, "rb") as f:
            head = f.read(132)
        if len(head) < 132 or head[128:132] != b"DICM":
            if os.path.isdir(path):
                skipped += 1
                continue
        with open(fp, "rb") as f:
            b = f.read()
        img = parse(b, os.path.basename(fp))
        img.path = fp
        check_image(img, os.path.basename(fp))
        out.append(img)
    if not out:
        raise DicomError("%s: no DICOM files found" % path)
    return out, skipped


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path")
    ap.add_argument("--series", type=int, default=None, help="which series of a folder with several (1 = largest)")
    ap.add_argument("--index", type=int, help="slice number in slice order, from 0 (default: the middle)")
    ap.add_argument("-o", "--output", help="PGM file to write")
    ap.add_argument("--maxval", type=int, help="force the PGM maxval (must hold the largest value)")
    ap.add_argument("--raw", action="store_true", help="ignore Rescale Slope / Intercept")
    ap.add_argument("--info-only", action="store_true", help="describe the series and slice, write nothing")
    a = ap.parse_args(argv)
    if not a.info_only and not a.output:
        ap.error("give -o OUTPUT.pgm, or --info-only")
    nl = _nifti_helpers()

    try:
        images, skipped = load_series(a.path)
        groups = {}
        for im in images:
            groups.setdefault(im.tags.get("SeriesInstanceUID") or "(none)", []).append(im)
        glist = sorted(groups.values(), key=lambda g: -len(g))
        if len(glist) > 1:
            if a.series is None:
                lines = ["%s holds %d series; choose one with --series K:" % (a.path, len(glist))]
                for k, g in enumerate(glist, 1):
                    t = g[0].tags
                    lines.append("  --series %d : %d slices, %s x %s, %s" % (k, len(g), t.get("Columns"), t.get("Rows"),
                                                                           t.get("Modality")))
                raise DicomError("\n".join(lines))
            if not 1 <= a.series <= len(glist):
                raise DicomError("--series %d is out of range 1..%d" % (a.series, len(glist)))
        series = glist[(a.series or 1) - 1]
        t0 = series[0].tags
        for im in series[1:]:
            for k in ("Rows", "Columns", "BitsAllocated", "BitsStored", "PixelRepresentation", "Photometric"):
                if im.tags.get(k) != t0.get(k):
                    raise DicomError("slices of one series disagree on %s (%s vs %s in %s)"
                                     % (k, t0.get(k), im.tags.get(k), os.path.basename(im.path)))
        ordered, method, positions, warns = order_slices(series)
        n = len(ordered)
        index = a.index if a.index is not None else n // 2
        if not 0 <= index < n:
            raise DicomError("--index %d is out of range 0..%d" % (index, n - 1))
        chosen = ordered[index]
        with open(chosen.path, "rb") as f:
            b = f.read()
        rows, cols, stored = decode_pixels(b, chosen, os.path.basename(chosen.path))
    except DicomError as e:
        sys.exit("error: %s" % e)
    except (OSError, struct.error) as e:
        sys.exit("error: cannot read %s: %s" % (a.path, e))

    t = chosen.tags
    ba, bs, pr = t["BitsAllocated"], t.get("BitsStored", t["BitsAllocated"]), t["PixelRepresentation"]
    print(f"path        : {a.path}" + (f"  ({skipped} non-DICOM file(s) skipped)" if skipped else ""))
    print(f"series      : {n} slice(s), {cols} x {rows} pixels, {ba} bits allocated / {bs} stored, "
          f"{'signed' if pr else 'unsigned'}, {t.get('Photometric')}, modality {t.get('Modality')}, "
          f"{'explicit' if chosen.explicit else 'implicit'} VR little endian")
    ps, th = t.get("PixelSpacing"), t.get("SliceThickness")
    print(f"geometry    : pixel spacing (row, col) mm {ps if ps else 'not given'}, slice thickness mm "
          f"{th[0] if th else 'not given'}")
    print(f"slice order : by {method}")
    if positions and n > 1:
        gaps = [positions[i + 1] - positions[i] for i in range(n - 1)]
        med = sorted(gaps)[len(gaps) // 2]
        print(f"              spacing between slices: median {med:.3f} mm, min {min(gaps):.3f}, max {max(gaps):.3f}")
        if min(gaps) < 1e-6:
            warns.append("two or more slices share a position (duplicates?)")
        elif med and (max(gaps) - min(gaps)) / abs(med) > 0.05:
            warns.append("uneven slice spacing (gap or missing slice?)")
    if len(glist) > 1:
        print(f"              this is series {a.series} of {len(glist)} in the folder")
    for w in warns:
        print(f"warning     : {w}")
    ident = sorted(set().union(*[im.identifying for im in series]))
    if ident:
        print(f"privacy     : identifying fields are present in the file(s) (values never read or shown): "
              f"{', '.join(ident)}" + ("; the file says identity was removed" if t.get("PatientIdentityRemoved") == "YES"
                                       else "") + ". Do not share or commit these files.")
    if t.get("BurnedInAnnotation") == "YES":
        print("warning     : BurnedInAnnotation=YES, patient details may be drawn into the pixels themselves")
    if t.get("Photometric") == "MONOCHROME1":
        print("note        : MONOCHROME1 (0 = white): values inverted so that 0 is black, as in MONOCHROME2")
    print(f"slice       : index {index} of 0..{n - 1}  ->  {cols} x {rows} = {cols * rows} pixels")

    slope = (t.get("RescaleSlope") or [1.0])[0]
    inter = (t.get("RescaleIntercept") or [0.0])[0]
    applied = (not a.raw) and (slope, inter) != (1.0, 0.0) and math.isfinite(slope) and slope != 0
    vals = [v * slope + inter for v in stored] if applied else list(stored)
    print(f"rescale     : slope={slope:g} intercept={inter:g}  ->  "
          f"{'applied' if applied else 'not applied (identity, zero, or --raw)'}")
    lo, hi = min(vals), max(vals)
    wc = (t.get("WindowCenter") or [None])[0]
    ww = (t.get("WindowWidth") or [None])[0]
    if t.get("Photometric") == "MONOCHROME1" and wc is not None:
        wc = None
        print("note        : the file's window is not converted for MONOCHROME1; ignoring it")
    offset = 0
    exact = all(v == int(v) for v in vals)
    if exact and lo < 0:
        offset = 1024 if lo >= -1024 else -int(lo)
        print(f"values      : negative values {int(lo)}..{int(hi)}: shifted up by {offset} so that they fit an unsigned PGM "
              f"(window centres must be shifted by the same {offset})")
        vals = [v + offset for v in vals]
        lo, hi = lo + offset, hi + offset
    if exact and 0 <= lo and hi <= 65535:
        ivals = [int(v) for v in vals]
        need = 255 if hi <= 255 else 4095 if hi <= 4095 else 65535
        maxval = a.maxval or need
        if maxval < hi or maxval > 65535 or maxval < 1:
            sys.exit("error: --maxval %d cannot hold the largest value %d" % (maxval, hi))
        print(f"values      : whole numbers {int(lo)}..{int(hi)}  ->  written unchanged, PGM maxval {maxval}")
        out_c = None if wc is None else wc + offset
        out_w = ww
    else:
        maxval = a.maxval or 65535
        if not 1 <= maxval <= 65535:
            sys.exit("error: --maxval must be 1..65535")
        span = (hi - lo) or 1.0
        ivals = [int(round((v - lo) / span * maxval)) for v in vals]
        print(f"values      : {lo:g}..{hi:g} are NOT whole numbers in 0..65535  ->  RESCALED linearly to 0..{maxval} "
              f"(out = round((v - {lo:g}) / {span:g} * {maxval})); windows are converted the same way")
        out_c = None if wc is None else (wc - lo) / span * maxval
        out_w = None if ww is None else ww / span * maxval
    fg = sorted(v for v in ivals if v > 0)
    zero_frac = 1 - len(fg) / len(ivals)
    pool = fg if zero_frac >= 0.05 and len(fg) >= 20 else sorted(ivals)
    p1, p50, p99 = nl.percentile(pool, 1), nl.percentile(pool, 50), nl.percentile(pool, 99)
    print(f"statistics  : {100 * zero_frac:.1f}% of pixels are zero; foreground p1={p1} median={p50} p99={p99}")
    if out_c is not None and out_w is not None and out_w >= 1:
        c, w = int(round(out_c)), max(1, int(round(out_w)))
        print(f"file window : centre {wc:g} width {ww:g} in the file's units  ->  window {c} {w}   "
              f"(the scanner's own display setting, in this PGM's units)")
    center, width = (p1 + p99) // 2, max(1, p99 - p1)
    print(f"suggested   : window {center} {width}   (from foreground p1..p99; use the file window above if present)")
    limit = device_pixel_limit()
    if limit:
        print(f"Pico limit  : {cols * rows} pixels vs the firmware's {limit}: "
              + ("FITS the whole-image firmware" if cols * rows <= limit else "needs streaming mode (the client chooses it)"))

    if not a.info_only:
        nl.write_pgm(a.output, cols, rows, maxval, [ivals[r * cols:(r + 1) * cols] for r in range(rows)])
        print(f"wrote       : {a.output}  ({cols} x {rows}, maxval {maxval})")


if __name__ == "__main__":
    main()
