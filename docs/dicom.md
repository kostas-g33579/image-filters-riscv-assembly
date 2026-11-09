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

