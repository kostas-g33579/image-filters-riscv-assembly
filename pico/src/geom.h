/* geom.h - the geometry of a stack of DICOM slices, worked out slice by slice (streaming).
 *
 * Feed the slices IN ORDER (ascending along the slice normal) to geom_add(), then call
 * geom_finish(). The state is a few hundred bytes: no slice is kept. The device refuses a stack that
 * is not strictly sorted, not evenly spaced, or whose slices disagree about size, pixel spacing,
 * orientation or series; the host does the sorting, the device only checks it.
 *
 * It mirrors Stack in scripts/dicom_slice.py operation for operation, in IEEE double precision
 * through the compiler's software floating point (the Hazard3 cores have no FPU; a stack of 200
 * slices costs a few milliseconds). tests/test_geom_c.py requires the two to agree to the last bit.
 *
 * The result maps voxel index (i = column, j = row, k = slice) to NIfTI's RAS+ world frame. The pixel
 * data keeps the order it has in the files; the matrix carries the orientation.
 */
#ifndef GEOM_H
#define GEOM_H
#include <stdint.h>
#include "dicom.h"

enum geom_err { GEOM_OK = 0, GEOM_E_NO_GEOMETRY, GEOM_E_ORIENTATION, GEOM_E_MISMATCH, GEOM_E_SERIES,
                GEOM_E_NOT_SORTED, GEOM_E_UNEVEN, GEOM_E_COUNT };
const char *geom_err_name(int err);

typedef struct {
    uint32_t count;                         /* slices accepted so far */
    uint16_t rows, cols;
    double ps_row, ps_col;                  /* PixelSpacing: distance between rows, between columns (mm) */
    double iop[6], r[3], c[3], n[3];        /* orientation, row direction, column direction, normal */
    double ipp0[3], step[3], step_len, t_prev, thickness;
    uint8_t has_thickness, has_step;
    char uid[DCM_STR_MAX + 1];
    uint8_t uid_len;
} geom_state;

typedef struct {
    uint16_t cols, rows;
    uint32_t count;
    double pixdim[3];                       /* mm: between columns, between rows, between slices */
    double affine[12];                      /* 3 x 4, row-major: RAS = A * (i, j, k, 1) */
    uint8_t plane;                          /* 0 sagittal, 1 coronal, 2 axial (by the slice normal) */
    uint8_t thickness_assumed;              /* a single slice: the step is SliceThickness (or 1 mm) */
    uint8_t thickness_mismatch;             /* SliceThickness disagrees with the measured step by > 1% */
} geom_result;

/* a decimal number as a double: exact (correctly rounded) when the mantissa is below 2^53 and |exponent| <= 22 */
double geom_dec_to_double(dcm_dec d);

void geom_init(geom_state *g);
/* t must have passed dcm_check(). Returns GEOM_OK or a GEOM_E_*; after an error the state is unusable. */
int geom_add(geom_state *g, const dcm_info *t);
int geom_finish(geom_state *g, geom_result *out);

#endif
