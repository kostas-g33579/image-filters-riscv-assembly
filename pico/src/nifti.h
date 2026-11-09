/* nifti.h - the NIfTI-1 side of the on-device anonymiser: what every output byte may be.
 *
 * The 352-byte header (348 bytes + the 4-byte "no extensions" flag) is built from constants and from
 * numbers that were parsed and validated by dicom.c / geom.c: sizes, voxel sizes, the voxel-to-world
 * matrix, the data type and the rescale slope and intercept. Every text field is either zero or a fixed
 * constant, so nothing from the source file can reach the output through the header. The voxel data are
 * the file's own integers, re-packed to a fixed type; they are NOT rescaled, only shifted/masked/sign-
 * extended according to Bits Stored and High Bit, which is the identity for the usual 16-bit case.
 *
 * Usage per series: nii_init(); for each slice (already accepted by dcm_check and geom_add) nii_add()
 * and nii_convert(); after geom_finish(), nii_header().
 */
#ifndef NIFTI_H
#define NIFTI_H
#include <stdint.h>
#include "dicom.h"
#include "geom.h"

#define NII_HEADER_BYTES 352

enum nii_err { NII_OK = 0, NII_E_PHOTOMETRIC, NII_E_FORMAT, NII_E_RESCALE, NII_E_TOO_LARGE, NII_E_COUNT };
const char *nii_err_name(int err);

typedef struct {
    uint32_t count;
    uint16_t rows, cols, bits_alloc, bits_stored, high_bit, pixel_rep;
    double slope, inter;                    /* DICOM Rescale Slope / Intercept (1 and 0 when absent) */
    uint8_t slope_given, inter_given;       /* the tags were present */
} nii_state;

void nii_init(nii_state *s);
/* t must have passed dcm_check(). MONOCHROME1 is refused; every slice must have the first slice's size, bit
 * fields, signedness and rescale values (a NIfTI file has one slope and one intercept for the whole volume). */
int nii_add(nii_state *s, const dcm_info *t);

/* bytes per output sample: 1 or 2 */
uint32_t nii_sample_bytes(const nii_state *s);
/* Re-pack one slice (rows * cols samples as stored in the file) into the output type, little endian.
 * `pix` holds rows*cols*bits_alloc/8 bytes, `out` the same number. */
void nii_convert(const nii_state *s, const uint8_t *pix, uint8_t *out);

/* The NIfTI datatype code and bits per voxel for this series: uint8 2, int16 4, int8 256, uint16 512. */
int nii_datatype(const nii_state *s, int *bitpix);

/* Build the header from the accepted series and its geometry. Returns NII_OK or NII_E_TOO_LARGE. */
int nii_header(const nii_state *s, const geom_result *g, uint8_t out[NII_HEADER_BYTES]);

#endif
