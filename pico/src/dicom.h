/* dicom.h - an allow-list DICOM reader for the Pico (and, as a Linux test build, the host).
 *
 * dcm_parse() reads ONE DICOM Part 10 file held in memory and keeps only the values of a short,
 * fixed list of technical tags (the WANTED table in dicom.c). Tags that identify a person or a
 * site are only NOTED AS PRESENT (a bit in `identifying`); their values are never read, stored or
 * returned. Nothing in a file is executed, every length is checked against the file, and nesting
 * and element counts are limited.
 *
 * It mirrors scripts/dicom_slice.py exactly (same element walk, same error order, same number
 * grammar): tests/test_dicom_c.py runs both on the same inputs and requires identical results.
 *
 * No libc, no floating point (the Hazard3 cores have no FPU): decimal numbers are kept EXACT as
 * mantissa and power of ten, so there is nothing to round.
 */
#ifndef DICOM_H
#define DICOM_H
#include <stdint.h>

#define DCM_MAX_ELEMENTS 2000000u        /* per file, counting nested ones */
#define DCM_MAX_DEPTH    16              /* nesting of sequences of undefined length */
#define DCM_MAX_PIXELS   100000000ull
#define DCM_STR_MAX      64              /* longest string kept; longer ones set .over */
#define DCM_DEC_MAX      6               /* decimal values kept per tag (the count keeps counting) */

enum dcm_err {
    DCM_OK = 0,
    /* dcm_parse */
    DCM_E_PREAMBLE, DCM_E_TOO_MANY, DCM_E_NO_TS, DCM_E_TS, DCM_E_TRUNC_HEADER, DCM_E_BAD_VR, DCM_E_DEPTH,
    DCM_E_NOT_ITEM, DCM_E_RUNS_PAST, DCM_E_PIXEL_TRUNC, DCM_E_ENCAPSULATED, DCM_E_VR_MISMATCH,
    DCM_E_MALFORMED, DCM_E_ONLY_META, DCM_E_NO_PIXEL,
    /* dcm_check */
    DCM_E_MISSING_TAG, DCM_E_COLOUR, DCM_E_PHOTOMETRIC, DCM_E_MULTIFRAME, DCM_E_SIZE, DCM_E_BITS_ALLOC,
    DCM_E_BITS_INCONSISTENT, DCM_E_PIXEL_REP, DCM_E_PIXEL_SHORT,
    /* dcm_parse_head only */
    DCM_E_HEADER_TOO_LARGE,
    DCM_E_COUNT
};
const char *dcm_err_name(int err);

/* an exact decimal number: m * 10^e, canonical (no trailing zeros in m; zero is m = 0, e = 0) */
typedef struct { int64_t m; int16_t e; } dcm_dec;
/* up to DCM_DEC_MAX values of a multi-valued decimal tag; n counts ALL values (saturating at 255) */
typedef struct { dcm_dec v[DCM_DEC_MAX]; uint8_t n; } dcm_declist;
/* a short text value: bytes 0..len-1 of the stripped value, cut at DCM_STR_MAX (then .over = 1) */
typedef struct { char s[DCM_STR_MAX + 1]; uint8_t len; uint8_t over; } dcm_str;

/* one bit per tag in the WANTED table (dicom.c): set when the tag was present and decoded */
enum {
    DCM_H_TS = 1u << 0, DCM_H_IMAGE_TYPE = 1u << 1, DCM_H_SOP = 1u << 2, DCM_H_MODALITY = 1u << 3,
    DCM_H_MANUFACTURER = 1u << 4, DCM_H_MODEL = 1u << 5, DCM_H_ID_REMOVED = 1u << 6, DCM_H_CONTRAST = 1u << 7,
    DCM_H_SCAN_SEQ = 1u << 8, DCM_H_SEQ_VAR = 1u << 9, DCM_H_ACQ_TYPE = 1u << 10, DCM_H_SERIES_UID = 1u << 11,
    DCM_H_PHOTOMETRIC = 1u << 12, DCM_H_SOFTWARE = 1u << 13, DCM_H_PROTOCOL = 1u << 14, DCM_H_BURNED_IN = 1u << 15,
    DCM_H_SAMPLES = 1u << 16, DCM_H_PLANAR = 1u << 17, DCM_H_ROWS = 1u << 18, DCM_H_COLS = 1u << 19,
    DCM_H_BITS_ALLOC = 1u << 20, DCM_H_BITS_STORED = 1u << 21, DCM_H_HIGH_BIT = 1u << 22, DCM_H_PIXEL_REP = 1u << 23,
    DCM_H_FRAMES = 1u << 24, DCM_H_INSTANCE = 1u << 25
};

typedef struct {
    uint32_t has;                       /* DCM_H_* */
    uint8_t  explicit_vr;               /* the data set is Explicit VR (else Implicit VR), little endian */
    uint32_t pix_off, pix_len;          /* the pixel data inside the file */
    uint32_t identifying;               /* bit i = identifying field i was present (dcm_identifying_name) */

    dcm_str ts, image_type, sop_class, modality, manufacturer, model, identity_removed,
            scan_seq, seq_var, acq_type, series_uid, photometric, software, protocol, burned_in;
    uint8_t contrast_declared;          /* ContrastBolusAgent present and non-empty (the name is NOT kept) */

    uint16_t samples, planar, rows, cols, bits_alloc, bits_stored, high_bit, pixel_rep;
    int64_t  frames, instance_number;   /* IS values (valid when the DCM_H_ bit is set) */

    dcm_declist slice_thickness, pixel_spacing, ipp, iop, tr, te, ti, flip, field_strength,
                rescale_slope, rescale_intercept, window_center, window_width;
} dcm_info;

#define DCM_N_IDENTIFYING 24
const char *dcm_identifying_name(unsigned i);       /* NULL when i is out of range */

/* Parse one file. Returns DCM_OK or a DCM_E_* from the parse group; *info is valid only on DCM_OK. */
int dcm_parse(const uint8_t *buf, uint32_t len, dcm_info *info);

/* Parse a file of which only the first `avail` bytes are in memory (`total` is its full length, avail <= total). The
 * pixel data need not be present: pix_off and pix_len describe where it is in the whole file, and the check that it
 * lies inside the file uses `total`. If the header does not end within the first `avail` bytes (the walk reaches
 * the end of what is held before it finds the pixel data) the result is DCM_E_HEADER_TOO_LARGE, which replaces
 * TRUNC_HEADER, RUNS_PAST, ONLY_META and NO_PIXEL in that situation only. dcm_parse(b, n, i) is parse_head(b, n, n, i). */
int dcm_parse_head(const uint8_t *buf, uint32_t avail, uint32_t total, dcm_info *info);

/* Validate the pixel-related tags (greyscale, one frame, 8 or 16 bits, consistent bit fields, enough
 * pixel data). Returns DCM_OK or a DCM_E_* from the check group. */
int dcm_check(const dcm_info *info);

#endif
