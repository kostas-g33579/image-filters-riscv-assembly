"""dicomgen.py - an independent DICOM WRITER for the tests (shares no code with the readers).

Builds files with known pixels and tags: Explicit and Implicit VR Little Endian, sequences of
defined and undefined length, private tags, odd padding, and fields to plant identifying
sentinels. Used by tests/test_dicom.py (the Python reader) and tests/test_dicom_c.py (the C reader).
"""
import struct

IMPL, EXPL = "1.2.840.10008.1.2", "1.2.840.10008.1.2.1"


def pad(v, fill=b" "):
    return v + fill if len(v) % 2 else v


def enc(g, e, vr, val, explicit):
    """One element. val is bytes (already padded to even length by the caller or here)."""
    val = pad(val, b"\0" if vr in (b"UI", b"OB", b"UN") else b" ")
    if not explicit:
        return struct.pack("<HHI", g, e, len(val)) + val
    if vr in (b"OB", b"OW", b"SQ", b"UN", b"UT"):
        return struct.pack("<HH2sHI", g, e, vr, 0, len(val)) + val
    return struct.pack("<HH2sH", g, e, vr, len(val)) + val


def US(v): return struct.pack("<H", v)
def DS(*v): return "\\".join(("%.10g" % x) for x in v).encode()


def seq_undefined(g, e, explicit, items, item_undefined=True):
    """A sequence of undefined length whose items hold the given pre-encoded element bytes."""
    out = struct.pack("<HH2sHI", g, e, b"SQ", 0, 0xFFFFFFFF) if explicit else struct.pack("<HHI", g, e, 0xFFFFFFFF)
    for body in items:
        if item_undefined:
            out += struct.pack("<HHI", 0xFFFE, 0xE000, 0xFFFFFFFF) + body + struct.pack("<HHI", 0xFFFE, 0xE00D, 0)
        else:
            out += struct.pack("<HHI", 0xFFFE, 0xE000, len(body)) + body
    return out + struct.pack("<HHI", 0xFFFE, 0xE0DD, 0)


def seq_defined(g, e, explicit, items):
    body = b"".join(struct.pack("<HHI", 0xFFFE, 0xE000, len(i)) + i for i in items)
    return (struct.pack("<HH2sHI", g, e, b"SQ", 0, len(body)) if explicit else struct.pack("<HHI", g, e, len(body))) + body


def pack_pixels(vals, alloc, stored, high, signed):
    shift = high + 1 - stored
    out = []
    for v in vals:
        raw = (v & ((1 << stored) - 1)) if True else v
        raw <<= shift
        if shift == 0 and stored < alloc:
            raw |= 0xF000 if alloc == 16 else 0xF0       # garbage in the unused high bits: must be masked off
        out.append(raw)
    return struct.pack("<%d%s" % (len(out), "H" if alloc == 16 else "B"), *out)


SENT = {"PatientName": b"ZZSENTINELNAME^ALICE", "PatientID": b"ZZSENTINELID99", "StudyDate": b"19991231",
        "InstitutionName": b"ZZSENTINELHOSPITAL", "PatientBirthDate": b"19500101", "PatientSex": b"ZZSENTINELSEX",
        "StudyDescription": b"ZZSENTINELDESC", "SeriesDescription": b"ZZSENTINELSERIES"}


def make(rows=4, cols=5, vals=None, ts=EXPL, alloc=16, stored=None, high=None, signed=False, photometric=b"MONOCHROME2",
         wc=None, ww=None, slope=None, inter=None, pos=None, iop=None, inst=None, uid=b"1.2.3.4", thick=1.0,
         spacing=(1.0, 1.0), ident=True, extra_before=b"", extra_after_px=b"", spp=1, nframes=None, planar=None,
         pix_override=None, pix_undefined=False, drop=(), burned=None, removed=None, modality=b"MR", tsuid=None):
    explicit = ts == EXPL
    stored = stored or alloc
    high = stored - 1 if high is None else high
    vals = vals if vals is not None else [(i * 37) % (1 << min(stored, 12)) for i in range(rows * cols)]
    E = lambda g, e, vr, v: enc(g, e, vr, v, explicit)
    d = b""
    d += E(0x0008, 0x0008, b"CS", b"DERIVED\\PRIMARY")
    d += E(0x0008, 0x0016, b"UI", b"1.2.840.10008.5.1.4.1.1.4")
    if ident:
        d += E(0x0008, 0x0020, b"DA", SENT["StudyDate"])
    d += E(0x0008, 0x0060, b"CS", modality)
    d += E(0x0008, 0x0070, b"LO", b"ACME")
    if ident:
        d += E(0x0008, 0x0080, b"LO", SENT["InstitutionName"])
        d += E(0x0008, 0x1030, b"LO", SENT["StudyDescription"])
        d += E(0x0008, 0x103E, b"LO", SENT["SeriesDescription"])
        d += E(0x0010, 0x0010, b"PN", SENT["PatientName"])
        d += E(0x0010, 0x0020, b"LO", SENT["PatientID"])
        d += E(0x0010, 0x0030, b"DA", SENT["PatientBirthDate"])
        d += E(0x0010, 0x0040, b"CS", SENT["PatientSex"])
    if removed:
        d += E(0x0012, 0x0062, b"CS", removed)
    d += extra_before
    d += E(0x0018, 0x0050, b"DS", DS(thick))
    d += E(0x0020, 0x000E, b"UI", uid)
    if inst is not None:
        d += E(0x0020, 0x0013, b"IS", str(inst).encode())
    if pos is not None:
        d += E(0x0020, 0x0032, b"DS", DS(*pos))
    if iop is not None:
        d += E(0x0020, 0x0037, b"DS", DS(*iop))
    if "SamplesPerPixel" not in drop:
        d += E(0x0028, 0x0002, b"US", US(spp))
    d += E(0x0028, 0x0004, b"CS", photometric)
    if planar is not None:
        d += E(0x0028, 0x0006, b"US", US(planar))
    if nframes is not None:
        d += E(0x0028, 0x0008, b"IS", str(nframes).encode())
    if "Rows" not in drop:
        d += E(0x0028, 0x0010, b"US", US(rows))
    d += E(0x0028, 0x0011, b"US", US(cols))
    d += E(0x0028, 0x0030, b"DS", DS(*spacing))
    d += E(0x0028, 0x0100, b"US", US(alloc))
    d += E(0x0028, 0x0101, b"US", US(stored))
    d += E(0x0028, 0x0102, b"US", US(high))
    d += E(0x0028, 0x0103, b"US", US(1 if signed else 0))
    if burned:
        d += E(0x0028, 0x0301, b"CS", burned)
    if wc is not None:
        d += E(0x0028, 0x1050, b"DS", DS(*wc) if isinstance(wc, (list, tuple)) else DS(wc))
    if ww is not None:
        d += E(0x0028, 0x1051, b"DS", DS(*ww) if isinstance(ww, (list, tuple)) else DS(ww))
    if inter is not None:
        d += E(0x0028, 0x1052, b"DS", DS(inter))
    if slope is not None:
        d += E(0x0028, 0x1053, b"DS", DS(slope))
    px = pix_override if pix_override is not None else pack_pixels(vals, alloc, stored, high, signed)
    if pix_undefined:
        d += (struct.pack("<HH2sHI", 0x7FE0, 0x0010, b"OB", 0, 0xFFFFFFFF) if explicit
              else struct.pack("<HHI", 0x7FE0, 0x0010, 0xFFFFFFFF)) + b"\xfe\xff\x00\xe0\x00\x00\x00\x00"
    else:
        d += E(0x7FE0, 0x0010, b"OW", px)
    d += extra_after_px
    meta = enc(0x0002, 0x0010, b"UI", (tsuid or ts).encode(), True)
    meta = enc(0x0002, 0x0002, b"UI", b"1.2.840.10008.5.1.4.1.1.4", True) + meta
    meta = enc(0x0002, 0x0000, b"UL", struct.pack("<I", len(meta)), True) + meta
    return b"\0" * 128 + b"DICM" + meta + d
