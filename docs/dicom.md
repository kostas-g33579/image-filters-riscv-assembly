# Reading DICOM: `scripts/dicom_slice.py`

Scanners and hospital archives produce DICOM, not PGM. This tool turns one DICOM slice
(or one slice out of a series folder) into the 16-bit PGM the rest of the project reads,
and tells you what the file said about itself, including the window the scanner suggests.

```
python3 -I scripts/dicom_slice.py data/dicom/SERIES_FOLDER --info-only
python3 -I scripts/dicom_slice.py data/dicom/SERIES_FOLDER --index 96 -o slice.pgm
python3 -I scripts/dicom_slice.py one_file.dcm -o slice.pgm
sudo python3 pico/tools/pico_filter.py slice.pgm out.pgm sharpen --window 600 1273 --verify
```
The `window C W` printed on the `file window` line is the scanner's own display setting
converted into the PGM's units, so it can be passed straight to the client. The
`suggested` line is a fallback computed from the picture itself.

## What it reads, and what it refuses

Reads: uncompressed pixel data, Implicit or Explicit VR Little Endian, one frame, one
greyscale sample per pixel, 8 or 16 bits allocated (any bits stored / high bit), signed
or unsigned, MONOCHROME1 (inverted so 0 is black) or MONOCHROME2.

Refuses, each time naming what it found and never guessing: JPEG, JPEG-LS, JPEG 2000 and
RLE compressed data, deflate, big endian, encapsulated pixel data, colour, palette,
multi-frame, other bit depths, a file that is truncated or whose lengths do not fit, and
anything without the `DICM` preamble. Compressed data is the common case in some
archives; decoding it would need a real decoder, which is outside this project.

Values: the Rescale Slope/Intercept are applied (`--raw` to skip). Negative results are
shifted up by 1024 when that is enough (the CT convention, so `HU + 1024`), otherwise by
the minimum, because a PGM cannot hold negatives; the window centre is shifted by the
same amount. Whole numbers are written unchanged with the smallest maxval of 255, 4095 or
65535 that fits; anything else is rescaled linearly and the window converted with it.
The tool prints which of these it did.

## Series

Given a folder it skips files that are not DICOM (and says how many), groups the rest by
Series Instance UID (several series: it lists them and you pick with `--series K`),
checks that the slices agree on size and bit depth, and orders them by position along the
slice normal (cross product of the Image Orientation vectors, dotted with Image
Position). Without geometry it falls back to Instance Number, and without that to file
name order, and says so. Duplicate positions and uneven spacing give warnings. `--index N`
counts from 0 in that order; the default is the middle slice.

## Privacy

DICOM headers carry patient name, ID, dates, sex, age, institution. The tool keeps only a
fixed list of technical tags in memory (`WANTED` in the source); the identifying ones are
only *noted as present*, their values are never read, printed, or copied, and the output
lists the field names so you know the file is not safe to share. This is tested with
sentinel strings planted in eight identifying tags and checked against everything the
tool prints and writes, and against the actual values in the real files. It also warns
about `BurnedInAnnotation`. The flag `PatientIdentityRemoved=YES` does not make a file
safe: the real files below carry it and still have a non-empty patient name, ID, study
date, sex and age.

## Real data used in the tests

BraTS's own DICOM originals on TCIA are behind a signed usage agreement (full heads,
re-identification risk), so they are not used. The public alternative is TCIA's
**UPENN-GBM** collection (630 patients, CC BY 4.0, no login), which is one of the sources
of BraTS 2021; its `Processed_CaPTk` series are skull-stripped DICOM. Twelve series (three patients, all four
sequences each) were downloaded into the git-ignored `data/dicom/`:

| series | scanner | slices | size (cols x rows), spacing | notes |
|---|---|---:|---|---|
| UPENN-GBM-00001: T1c, T1, FLAIR, T2 | Siemens | 192, 192, 60, 192 | 192 x 256 at 0.98 mm (FLAIR 0.94 mm, 3 mm slices); T2 is **sagittal**, 256 x 320 at 0.90 mm | Implicit VR, unsigned 16 bit |
| UPENN-GBM-00002: T1c, T1, FLAIR, T2 | Siemens | 192, 192, 60, 192 | the same four geometries | a second complete set |
| UPENN-GBM-00283: T1c (3D), T1, FLAIR, T2 | GE | 120, 32, 32, 32 | 256 x 256 at 0.94 mm (1.5 mm slices) and 0.86 mm (5 mm slices) | Implicit VR, **signed** 16 bit |

Three patients with all four sequences each, in 12 folders (a manifest listing them is in
`data/dicom/MANIFEST.txt`, git-ignored with the data). The different slice counts, thicknesses
and orientations between the sequences of one patient are exactly the cross-series mapping
problem a mask-drawing tool has to solve. The sagittal T2 also caught a wrong assumption in
the test itself: its oracle sorted slices by z, which only makes sense for axial series (the
sagittal slice positions differ in x and all share one z). The tool was right; the oracle now
works out the slice normal from the orientation vectors on its own. The BraTS NIfTI file
`BRATS_001.nii.gz` holds all four sequences as channels (FLAIR, T1w, t1gd, T2w) of one file.

Neither was explicit VR, and one was signed: the kind of detail that synthetic files
written by the same author as the reader tend to miss. To fetch more (the API answers on
`services.cancerimagingarchive.net`; the `.nih.gov` host did not resolve):

```
curl -s 'https://services.cancerimagingarchive.net/nbia-api/services/v1/getSeries?Collection=UPENN-GBM&format=json' -o series.json
# pick a SeriesInstanceUID (ImageCount, FileSize, SeriesDescription and the licence are in the JSON), then
curl -L -o case.zip 'https://services.cancerimagingarchive.net/nbia-api/services/v1/getImage?SeriesInstanceUID=UID'
```
Unpack into its own folder under `data/dicom/` after checking the names inside the zip (the
archive is untrusted input), and keep the `LICENSE` file that comes with it. CC BY 4.0
requires attribution when you publish results; the files themselves must not be committed.

## Tests

`make test-dicom` (`tests/test_dicom.py`): an independent DICOM writer (in the test file,
not shared with the reader) builds files with known pixels; the tool must return them
exactly in both encodings, at 8, 12 and 16 bits stored, signed and unsigned, with every
rescale/window/inversion/ordering path; sequences of undefined and defined length,
nested, private tags and odd padding must be skipped correctly; every refusal above is
checked for its message; 1,200 randomly corrupted files must each either work or end with
a clean `error:`; the privacy test described above; and the real series are decoded by a
second, independent method (the pixel data is the last element of those files) and
compared slice by slice. The same slice was also run through the device model
(streamed, 192 x 256) and matched the harness byte for byte.

The test was itself tested: 47 deliberately injected bugs in the tool (a missing mask, a
wrong sign bit, an off-by-one in a threshold, a wrong cross product, a skipped length
check, a leaked identifying value, ...) were each caught. The first round caught 38 of
them; the gaps it showed (boundaries at exactly 255/256/4095/4096, a small negative
minimum, a general oblique orientation, signed MONOCHROME1, a pixel block one value too
short, a wrong-but-valid value representation) became tests, and a refusal that was only
checked as "refused" now also checks the reason. One more change, comparing the minimum
with -1024 using > instead of >=, cannot be told apart by any test because both choices
give the same shift at exactly -1024.

## On the chip

UPENN-GBM-00001, slice 96 (`--index 96`, 192 x 256, maxval 4095, the scanner's window 600 1273),
sharpen, Pico 2 W, both byte-identical to the Linux harness:

| mode | core cycles | instructions | IPC | round trip |
|---|---:|---:|---:|---:|
| whole image | 2,320,148 | 2,166,620 | 0.93 | 0.25 s |
| streamed | 3,725,403 | 2,735,291 | 0.73 | 0.16 s |

The whole-image figures are the same 47.2 cycles and 44.1 instructions per pixel as on the
240 x 240 slice, so the slice's shape and origin change nothing about the filter. The
streamed counters are higher because USB interrupt work falls inside the measured window
(see `docs/streaming.md`); the streamed run is nevertheless faster on the clock because
upload and download overlap.


## The C reader (`pico/src/dicom.c`) - the first piece of the on-device anonymiser

The plan is for the Pico to be the trust boundary: the PC sends raw DICOM files and the device
returns only a NIfTI file built from values it has parsed and validated itself. `dicom.c` is the
parsing half. It reads one file held in memory and keeps only the values of a fixed allow-list of
technical tags; the tags that identify a person or a site (name, ID, dates, sex, age, institution,
station, serial number, descriptions, ...) are only *noted as present* (one bit each) and their
values are never read. It has no libc and no floating point (the Hazard3 cores have no FPU):
decimal numbers are kept **exact**, as a mantissa and a power of ten, so there is nothing to round.

It mirrors `scripts/dicom_slice.py` step for step: the same element walk, the same sequence
handling with the same limits (nesting 16, 2,000,000 elements), the same checks in the same
order, the same strict number grammar. That grammar replaced Python's `float()`, which also
accepts `nan`, `inf`, `1_0` and unicode digits: a decimal is `[+-]?(digits[.digits*]|.digits)`
with an optional exponent of 1 to 3 digits, at most 18 mantissa digits and 64 characters; an
integer is `[+-]?` and 1 to 12 digits; whitespace means ASCII whitespace only.

`make test-dicom-c` (`tests/test_dicom_c.py`) is a **differential test**: the C reader (built as
`build32/dicom_test`, run under qemu-riscv32) and the Python reader are given the same files and
must agree exactly on the status or the error class (24 classes), every kept value, the list of
identifying fields present, the pixel-data position and the verdict of the pixel-tag checks.
The corpus: 458 synthetic files from an independent writer (`tests/dicomgen.py`, both encodings,
nested sequences, private tags, adversarial number strings, every error class), the element-count
limit probed at exactly 2,000,000 and 2,000,001 elements, all 1,500 real files, and 7,500
randomly corrupted files. It also requires the corpus to *reach every error class*, and the decimals
are compared against a third, independent routine written from the grammar.

The test was tested: 64 deliberately injected bugs in `dicom.c` (an off-by-one in a limit, a dropped
sign, a swapped table entry, a skipped check, a wrong bit, ...) were each compiled into their own
binary and run through the suite; 62 were caught and 2 cannot change any output (a redundant
64-character bound that no valid decimal can reach, and a UI/CS distinction the reader never uses).
The first sweep caught 58 and showed four gaps in the corpus (carriage returns, strings of exactly
256 bytes, PixelRepresentation 2, the exact element-count boundary), which became test cases.

Not part of the firmware yet: `dicom.c` is built only for the tests. Next come the geometry (slice
order, spacing from the positions, orientation), the NIfTI writer and the USB protocol.

## Stack geometry (`pico/src/geom.c`) - slice order, spacing and the voxel-to-world matrix

A NIfTI file needs to know where its voxels are. `geom.c` works that out from the slices, one slice
at a time, keeping no pixels (a few hundred bytes of state). The Python version is `Stack` in
`scripts/dicom_slice.py`; the two do the same operations in the same order in IEEE double precision
(the chip has no FPU; the compiler's software floating point costs a few milliseconds for a whole
series), so they agree to the last bit.

**What DICOM says.** `ImageOrientationPatient` gives two unit vectors, the direction of increasing
column (r) and of increasing row (c), in the patient's left-posterior-superior frame;
`ImagePositionPatient` is the centre of a slice's first pixel; `PixelSpacing` is
(distance between rows, distance between columns). Pixel (column i, row j) of a slice sits at
`IPP + i * ps_col * r + j * ps_row * c`. The normal is `r x c`.

**What the device does.** The host sends the slices sorted along the normal (the Python tool can sort
them); the device only *checks*:
- every slice has the same size, pixel spacing, series UID and (to 1e-4 per component) orientation,
  else `MISMATCH` / `SERIES`; `r` and `c` must be unit length and perpendicular (to 2e-3 / 1e-3),
  else `ORIENTATION`; the geometry tags must exist with the right number of values, else `NO_GEOMETRY`;
- the position along the normal must strictly increase, else `NOT_SORTED`;
- the step vector is the difference of the first two positions, and every later slice must sit on the
  regular lattice `IPP0 + k * step` to within 1% of the step plus 0.1 micrometre, else `UNEVEN`
  (a gap, a duplicate, a slice shifted sideways, positions rounded too coarsely);
- the slice spacing is always measured from the positions, never taken from a tag: `SpacingBetweenSlices`
  said 1 mm in all twelve real series although the true spacings were 0.9, 1.0, 1.5, 3.0 and 5.0 mm. A
  stack with a gantry tilt (step not parallel to the normal) is represented exactly, because the matrix uses
  the step vector. A single slice takes its step from `SliceThickness` (1 mm if absent) and says so.

**The result.** A 3 x 4 matrix that takes (i, j, k) to NIfTI's right-anterior-superior frame (x and y of the
DICOM position negated), the voxel sizes, the plane (sagittal, coronal or axial from the normal) and two
flags: the step was assumed from the thickness, and the `SliceThickness` tag disagrees with the measured step
by more than 1%. The pixel data is **not** reordered or flipped: the matrix carries the orientation. A normal
axial scan comes out as voxel axes pointing Left, Posterior, Superior.

**One limitation, on purpose.** Because the step comes from the first two slices, a rounding error in their
positions is multiplied by the slice number when extrapolating. Real scanner positions are precise to ~1e-7 mm
or better and pass easily (the real files deviate from the lattice by at most 0.16 micrometres, from rounding
in the files); a stack of 12 tolerates ~1e-4 mm of noise, one of 200 about 5e-6 mm, and 200 slices whose
positions were rounded to three decimals are refused. The failure is a clear refusal, never silently wrong
geometry. Refining the step from the last slice would need the whole position list and a header written after
the data.

**How it is tested** (`make test-geom-c`, `tests/test_geom_c.py`, 490 series, about 5 seconds):
- C against Python, bit for bit, on every status and every double: synthetic stacks (axial, flipped, sagittal,
  coronal, three oblique orientations, tilted, running against the normal, 1 to 40 slices, anisotropic pixels,
  round numbers), every error class, 300 random stacks with noise, and the 12 real series in order, reversed
  and shuffled;
- against the **definition**: for every accepted series the matrix must map voxels to where DICOM puts that
  pixel, computed from each slice's own position (not the step vector), sharing no code with either
  implementation;
- hand-derived answers for the voxel axis directions of the four standard orientations (L P S, R P I, P I R,
  L I P).
69 deliberate bugs in `geom.c` (a flipped sign in one matrix entry, a swapped cross-product term, a skipped
check, a wrong tolerance, transposed spacings, ...) were each built and run; 67 were caught and 2 cannot
change an output (Newton's square root converges in four iterations, the code runs six). The first sweep
caught 59 and showed eight gaps in the corpus, which became test cases.

## The NIfTI side (`pico/src/nifti.c`) - what every output byte may be

The 352-byte header (348 + the 4-byte "no extensions" flag) is built from **constants and from numbers
dicom.c and geom.c have parsed and validated**: sizes, voxel sizes, the voxel-to-world matrix, the data type and
the rescale slope and intercept. Every text field is zero or a fixed constant (the one description string says
"sanitised DICOM: pixels and geometry only"), so nothing from the source file reaches the output through the
header. Little endian, `sform_code` 1 (scanner anatomical) with the matrix from the geometry, `qform_code` 0 (the
sform is used), millimetres, three dimensions, `vox_offset` 352.

**The voxels are the file's own integers, not rescaled.** Each sample is shifted down by High Bit + 1 - Bits
Stored, masked to Bits Stored, sign-extended if the data are signed, and written little endian: the identity for
the usual full-16-bit case, a real change for 12 bits stored with garbage in the unused bits. The type follows
the file: uint8 / int8 for 8 bits allocated, uint16 / int16 for 16, by Pixel Representation.

**Rescale slope and intercept** follow what NIfTI readers expect: true value = slope x stored + intercept, with
the DICOM values written to `scl_slope` / `scl_inter` as they are and an explicit **1 and 0** when the tags are
absent. A NIfTI file has one slope and one intercept for the whole volume, so a series whose slope or intercept
changes from slice to slice is **refused** (`RESCALE`), as is a zero slope. Readers differ in whether they apply
them (nibabel does in `get_fdata()`, MATLAB's `medicalVolume` does not), which is why they are also repeated in
the JSON sidecar. Other refusals: `PHOTOMETRIC` (MONOCHROME1, i.e. inverted data), `FORMAT` (size, bit fields or
signedness change between slices), `TOO_LARGE` (a dimension above 32,767: NIfTI stores them as signed 16-bit).

**How it is tested** (`make test-nifti-c`, 77 series, about 12 seconds):
- the header byte for byte against one built here straight from the NIfTI-1 layout (offsets with `struct`), using
  the geometry the verified Python `Stack` gives; the layout itself checked against a header written by other
  software (the real BraTS NIfTI);
- a hash of every slice's re-packed bytes against the same re-packing worked out independently (8 to 16 bits
  allocated, 9 to 16 stored, high bit anywhere, signed and unsigned, garbage in the unused bits), and for the 12
  real series the first, middle and last slice;
- every refusal, including the exact 32,767 / 32,768 boundary of each dimension and a 32,768-slice series;
- the repo's own strict NIfTI reader (`scripts/nifti_slice.py`) as a second opinion on 7 written files: size, type,
  voxel sizes, slope, intercept and every voxel byte read back.
67 deliberate bugs (an offset moved, two fields swapped, a sign bit one too high, a check removed, a limit off by
one) were each built and run: 65 were caught, and 2 cannot change an output (the size checks that geometry has
already made). The first sweep caught 61 and showed four gaps: square pixels in every NIfTI test (swapped voxel
sizes were invisible), and bits stored / bits allocated each changed only together with another field.

## The JSON sidecar (`pico/src/sidecar.c`)

Next to every sanitised `.nii` goes one `.json`. Everything in it is one of: a constant; a **label** the user supplied
(`subject`, `sequence`, `site`, `scanner`: 1 to 32 characters of `A-Z a-z 0-9 _ -`, never taken from the DICOM);
a **number** that the reader parsed and a range check accepted, printed *exactly* (the decimal as it was written,
shifted to BIDS units); a **measured** number (printed with 9 decimals, trailing zeros dropped); or a short **text
value** that passed a strict character check. The blocks:

- `tool`, `labels`.
- `measured`: size, voxel sizes (mm), plane, field of view, the voxel-to-RAS matrix (12 numbers, row-major), the
  NIfTI data type and bits stored.
- `from_tags`, with BIDS key names and units (seconds, degrees, tesla): `RepetitionTime`, `EchoTime`,
  `InversionTime`, `FlipAngle`, `MagneticFieldStrength`, `ScanningSequence`, `SequenceVariant`,
  `MRAcquisitionType`, `ContrastAgentDeclared` (`true`, or `null` meaning *the scanner did not say*, which is
  **not** "no contrast"), `Manufacturer`, `ManufacturersModelName`, `SoftwareVersions`, `ProtocolName`,
  `SliceThickness`, `RescaleSlope`, `RescaleIntercept` (the effective values, 1 and 0 when absent). They come from
  the first slice. The per-slice display window (`WindowCenter` / `WindowWidth`) is deliberately **not** included:
  it differs from slice to slice, so one value would mislead.
- `discrepancies`: a list of entries with fixed wording: the field strength was in gauss and was converted
  (`15000` -> `1.5` T, as written by GE), a field strength or a number was out of range and left out, a text value
  was rejected (only the field name is given, never the text), the slice thickness tag disagrees with the measured
  spacing by more than 1%, the spacing of a single slice was assumed from its thickness.
- `audit`: the number of slices; **which identifying fields were present, by name and count, never their values**;
  that all other tags were dropped; that the pixels are the file's own integers; that burned-in text is **not**
  detected or removed and that faces are **not** removed; how many slices had each value (`YES`, `NO`, `other`,
  `absent`) of the `BurnedInAnnotation` and `PatientIdentityRemoved` tags; and which text values were copied.

**Range checks** (a value outside them is left out and listed under `discrepancies`): repetition time 0 to 10^6 ms,
echo and inversion time 0 to 10^5 ms, flip angle 0 to 360 degrees, slice thickness above 0 up to 1000 mm, field
strength 0.1 to 12 T (or 1,000 to 120,000 gauss, converted). **Text** may contain letters, digits, space and
`_ . + - : * / ( ) \` and be at most 64 characters (the code strings only capital letters, digits, space, `_`
and `\`); nothing else is ever copied, so nothing can break the JSON or carry a name through it.

**How it is tested** (`make test-sidecar-c`, 139 cases plus the 12 real series, about 2 seconds): the JSON is parsed
with a real JSON parser and compared, types included, with what the cases were *built* to say, worked out here with
exact `Decimal` arithmetic: every acquisition value in BIDS units, each range check at both sides of every boundary,
the gauss window, every kind of rejected string (a quote, braces, a control character, non-ASCII, 65 characters, a
backslash that must be escaped), the discrepancies in order, the audit counts (including look-alike values such as
`YE` or `NA`), anisotropic pixels, 8-bit and 12-bits-in-16 data, large and small exponents, a tiny negative matrix
entry (must print `0`, never `-0`), and the labels. A text-level lint looks at every number as printed (no trailing
zero, no `-0`, no `+`, no leading point or zero, plain decimals only between 1e-20 and 1e20), because a numeric
comparison would accept `0.9765625000`. No planted identifying value, and none of the real files' identifying values
(read independently), may appear anywhere in any JSON. 88 deliberate bugs in `sidecar.c` were each built and run: 86
caught, 2 cannot change an output. The first sweep caught 73 and showed 13 gaps (the lesson of the NIfTI tests
again: square pixels hid swapped field-of-view spacings), which became test cases.

## The on-chip probe (`pico/probe/anon_probe.c`)

Before the USB protocol existed, the new modules (`dicom.c`, `geom.c`, `nifti.c`, `sidecar.c`)
are exercised on the real chip by a small test firmware (`make anon-probe` builds `build-pico/anon_probe.uf2`).
It holds a built-in synthetic 8-slice series (generated by `scripts/make_probe_data.py`; no real data): oblique,
signed 12 bits stored in 16 with garbage in the unused bits, a fractional rescale slope, an inversion-recovery
sequence, a field strength in gauss, anisotropic pixels, implicit VR, planted identifying values. Send it one byte
over USB serial and it runs parse, check, geometry, NIfTI header and re-packing, and the JSON, printing a status and
a voxel checksum per slice, the 352-byte header, the JSON, and the cycle count of every stage.
`sudo python3 pico/tools/anon_probe_check.py` does exactly that and compares everything with the emulator running the
same C code on the same bytes: they must be identical.

**First run on a Pico 2:** all 8 statuses, all 8 voxel checksums, the 352-byte header and the 2060-byte JSON were
identical to the emulator's, so the chip's compiler flags and software floating point change nothing. Per 480-sample
slice: parse 55-77k cycles, geometry 13-31k, re-pack plus hash 36-43k; building the JSON 183k, once. (The SDK's USB
text mode turns every newline into CR LF; the checker undoes that, and the real protocol writes binary-safe.)

## The anonymiser protocol (`pico/src/anon.c`) and the client (`pico/tools/pico_anonymize.py`)

```
sudo python3 pico/tools/pico_anonymize.py data/dicom/SERIES_DIR --subject P017 --sequence T1c [--site S02] [--scanner C01]
```
writes `output/anonymized/P017_T1c/P017_T1c.nii` and `.json` (only those two). Labels are 1 to 32 characters of
`A-Z a-z 0-9 _ -`. A folder with several series needs `--series K`. An existing result folder is refused unless
`--overwrite`, which replaces only the two files. The results are written to a temporary folder and moved into place
only when the device says it is done and the client's own checks agree (NIfTI header sane, voxel count equal to
the dimensions, JSON plain ASCII and parseable), so a failed run leaves nothing. Run with sudo, the results belong to
the user who ran sudo. After a failure the client waits 6 s so the device finishes discarding what was sent.

**Why the file size does not matter.** The largest DICOM files in the TCIA UPENN-GBM collection are 603 KB (a 60-slice
FLAIR) and 515 KB (the 512 x 512 series); the median is 99 KB and the raw diffusion series reach 2 MB; the chip has
520 KB of RAM. The device therefore holds only the first 128 KB of a file (the header must end inside it; it is about
3 KB in the data seen) and converts and sends the pixel data in 4 KB pieces as it arrives. A header that does not end
within 128 KB is refused as `PARSE:HEADER_TOO_LARGE`. The sorting of the slices is done by the client; the device only
verifies it (a series that is not strictly increasing along the normal, or not evenly spaced, is refused), so a
wrong sort can only cause a refusal, never a wrong volume.

**Wire format** (little-endian). Request: `DCM1`, `u32 flags` (0), `u32 nfiles` (1 to 4096), four labels (subject,
sequence, site, scanner: `u8` length and bytes), then each file as `u32` length and bytes. The client waits for the
`A` frame before it sends the files. Reply: `NII1` and frames of `u8 type, u32 length, payload`: `A` accepted, `V`
voxel bytes (at most 4096, already converted), `H` the 352-byte header, `J` the JSON, `D` done, `E` error (the index
of the file, or 0xFFFFFFFF, and `STAGE:NAME` from a fixed table, for example `GEOM:UNEVEN`). The header and JSON come
after all the voxels because the geometry needs every slice; the client writes the voxels after a 352-byte gap and
fills the header in when it arrives. After an error nothing more is sent, and the device reads and discards the rest
of the request (a host that goes quiet for 5 s gets a `TIMEOUT` error) so the next request starts cleanly. What leaves
the device is therefore: the frame headers, voxel values, the header, the JSON, and fixed error names.

**How it is tested.**
`make test-anon-c` runs the protocol in two emulator builds (the real sizes; and a 1023-byte head buffer, 64-byte
chunks and reads of 1 to 13 bytes at a time, which forces cuts inside a sample, partial reads and many chunk
boundaries even on small files): 30 good series (all bit layouts, orientations, padding longer than the head buffer,
one slice, the 12 real series) with the voxels compared against an independent re-pack and the header and JSON
against the verified `nifti_test` and `sidecar_test`; 11 error cases, each followed by a good request on the same
connection; request-level errors; noise before the magic; requests cut off at 10 kinds of place (the voxels sent
before the cut must be a prefix of the right answer and never more than arrived); a header ending exactly at the
limit; 4096 files; a 70 MiB file; a later file that contains a whole request (it must be dropped, not answered);
labels of different lengths in consecutive requests; a privacy scan of every reply. 45 deliberate bugs in `anon.c`
were each built and run: 43 caught, 2 cannot change an observable result. The sweep found four gaps in the tests
(a stale list index, a drain the magic search hid, a padding series too short to reach the tail path, and no
boundary test), all fixed.
`make test-anonymize` tests the client against that same protocol code and against 18 fake devices that misbehave (not
there, not the protocol, too few voxels, headers that break exactly one property each, non-ASCII or unparseable JSON,
no JSON, an unknown frame, an error frame, a reply cut off): each must end in a clear error and leave no output. It
also checks the output tree contains exactly the two files, sorting of shuffled and reversed series, several series
in one folder, the overwrite rules, that invalid labels never reach the device, the sudo ownership of everything
created, and one real series end to end. 26 deliberate bugs in the client were built and run; the first sweep left 7 alive, because the fake headers
broke several properties at once and so each single check could be removed unnoticed, and because the label test
never proved that a bad label stays on the host. Now there is one fake header per property and a spy device that
notices a single byte.

### On the board

`make pico` builds `build-pico/image_filters.uf2`, which now serves the filters (`IMG1`, `STR1`) **and** the
anonymiser (`DCM1`) on the same USB serial port; the first four bytes of a request say which. Flash it as before
(`picotool load -f build-pico/image_filters.uf2`), then:

```
sudo python3 pico/tools/pico_anonymize.py data/dicom/<series folder> --subject P017 --sequence T1c
```

Memory: the anonymiser's work area (153,640 bytes: the 128 KB head buffer, 4 KB chunk buffers, the parser, geometry,
NIfTI and JSON state) lives inside the 420,000-byte arena the filters use, so the RAM use of the firmware did not
change (code 78.9 KB, bss 432.5 KB; the filter-only image was 32 KB of code). The arena is now 8-byte aligned (the
work area holds 64-bit integers and doubles) and a compile-time check keeps the work area inside it.
Stack: a header of nested sequences makes the reader recurse up to 16 levels of 128 bytes each; with the protocol
frames the worst case is about 2.75 KB plus a USB interrupt, more than the SDK's default 2 KB, so the firmware sets
`PICO_STACK_SIZE=0x1000` (the whole 4 KB scratch bank; no extra RAM). `make test-anonymize` also runs filter and
anonymiser requests alternately on one connection to the real dispatcher (`build32/proto_test`) and checks that every
result is unchanged, including after a refused anonymiser request.

### Fetching more data

```
python3 -I scripts/fetch_tcia.py plan --border > data/border_plan.csv     # prints what it would fetch (labels, sizes) on stderr
python3 -I scripts/fetch_tcia.py get data/border_plan.csv --yes           # downloads into data/fetched/
```

`scripts/fetch_tcia.py` downloads series of the TCIA collection UPENN-GBM (CC BY 4.0, public, no login) from a plan
file. The downloaded zips are untrusted: only plain file names (numbered `.dcm` files and one file called exactly
`LICENSE`, as in all 27 archives seen), no paths, folders or links, no member over 256 MB, a total no larger than the
catalogue says plus 10 MB, exactly the catalogue's image count, every image starting with the DICOM preamble, and
corrupt, encrypted or unsupported archives refused; everything is checked before anything is written, in a temporary
folder that is renamed into place. The default target is `data/fetched/`, **not** `data/dicom/`, which is the fixed set
of 12 series the regression tests are written for. `plan --border` selects the extremes of the collection: the largest
series (248 slices, 131 MB, GE), the largest file (617 KB), the fewest slices (25), a short GE and a Vida series, the
smallest raw series (700-image perfusion), a diffusion series, a patient with 8 processed series and one with 2.

`make test-fetch` needs no network (24 hostile or wrong archives, the size boundaries, naming,
plan and `get` behaviour; 31 injected bugs, 27 caught, 4 equivalent).
