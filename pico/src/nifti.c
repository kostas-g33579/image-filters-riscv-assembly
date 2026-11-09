/* nifti.c - the NIfTI-1 header and voxel re-packing of the on-device anonymiser (see nifti.h). */
#include "nifti.h"

static const char *const NII_NAMES[NII_E_COUNT] = {"OK", "PHOTOMETRIC", "FORMAT", "RESCALE", "TOO_LARGE"};
const char *nii_err_name(int err) { return err >= 0 && err < NII_E_COUNT ? NII_NAMES[err] : "?"; }

/* The only free text in the header. It says what the file is and nothing else. */
static const char DESCRIP[] = "sanitised DICOM: pixels and geometry only";

void nii_init(nii_state *s)
{
    uint8_t *p = (uint8_t *)s;
    uint32_t i;
    for (i = 0; i < sizeof *s; i++) p[i] = 0;
}

static int is_monochrome2(const dcm_str *p)
{
    static const char lit[] = "MONOCHROME2";
    unsigned i;
    if (p->over || p->len != sizeof lit - 1) return 0;
    for (i = 0; i < sizeof lit - 1; i++)
        if (p->s[i] != lit[i]) return 0;
    return 1;
}

int nii_add(nii_state *s, const dcm_info *t)
{
    uint16_t ba = t->bits_alloc;
    uint16_t bs = (t->has & DCM_H_BITS_STORED) ? t->bits_stored : ba;
    uint16_t hb = (t->has & DCM_H_HIGH_BIT) ? t->high_bit : (uint16_t)(bs - 1);
    double slope = t->rescale_slope.n > 0 ? geom_dec_to_double(t->rescale_slope.v[0]) : 1.0;
    double inter = t->rescale_intercept.n > 0 ? geom_dec_to_double(t->rescale_intercept.v[0]) : 0.0;

    if (!is_monochrome2(&t->photometric)) return NII_E_PHOTOMETRIC;
    if (slope == 0) return NII_E_RESCALE;                    /* a zero slope is meaningless in DICOM */
    if (s->count == 0) {
        s->rows = t->rows;
        s->cols = t->cols;
        s->bits_alloc = ba;
        s->bits_stored = bs;
        s->high_bit = hb;
        s->pixel_rep = t->pixel_rep;
        s->slope = slope;
        s->inter = inter;
        s->slope_given = t->rescale_slope.n > 0;
        s->inter_given = t->rescale_intercept.n > 0;
    } else {
        if (t->rows != s->rows || t->cols != s->cols || ba != s->bits_alloc || bs != s->bits_stored ||
            hb != s->high_bit || t->pixel_rep != s->pixel_rep)
            return NII_E_FORMAT;
        if (slope != s->slope || inter != s->inter) return NII_E_RESCALE;
    }
    s->count++;
    return NII_OK;
}

uint32_t nii_sample_bytes(const nii_state *s) { return s->bits_alloc / 8u; }

void nii_convert(const nii_state *s, const uint8_t *pix, uint8_t *out)
{
    uint32_t n = (uint32_t)s->rows * s->cols, i;
    uint32_t shift = (uint32_t)s->high_bit + 1u - s->bits_stored;
    uint32_t mask = (1u << s->bits_stored) - 1u;
    uint32_t sign = 1u << (s->bits_stored - 1u);
    for (i = 0; i < n; i++) {
        uint32_t raw = s->bits_alloc == 8 ? pix[i] : (uint32_t)pix[2 * i] | ((uint32_t)pix[2 * i + 1] << 8);
        uint32_t v = (raw >> shift) & mask;
        if (s->pixel_rep && (v & sign)) v |= ~mask;                  /* sign-extend from Bits Stored */
        if (s->bits_alloc == 8) {
            out[i] = (uint8_t)v;
        } else {
            out[2 * i] = (uint8_t)(v & 0xFF);
            out[2 * i + 1] = (uint8_t)((v >> 8) & 0xFF);
        }
    }
}

int nii_datatype(const nii_state *s, int *bitpix)
{
    *bitpix = s->bits_alloc;
    if (s->bits_alloc == 8) return s->pixel_rep ? 256 : 2;          /* int8 : uint8 */
    return s->pixel_rep ? 4 : 512;                                  /* int16 : uint16 */
}

/* ---------------------------------------------------------------- the header */
static void put16(uint8_t *h, unsigned off, uint16_t v) { h[off] = (uint8_t)v; h[off + 1] = (uint8_t)(v >> 8); }
static void put32(uint8_t *h, unsigned off, uint32_t v)
{
    h[off] = (uint8_t)v; h[off + 1] = (uint8_t)(v >> 8); h[off + 2] = (uint8_t)(v >> 16); h[off + 3] = (uint8_t)(v >> 24);
}
static void putf(uint8_t *h, unsigned off, double d)
{
    union { float f; uint32_t u; } v;
    v.f = (float)d;
    put32(h, off, v.u);
}

int nii_header(const nii_state *s, const geom_result *g, uint8_t h[NII_HEADER_BYTES])
{
    unsigned i;
    int bitpix, dt = nii_datatype(s, &bitpix);
    if (g->cols > 32767 || g->rows > 32767 || g->count > 32767) return NII_E_TOO_LARGE;   /* dim[] is a signed short */
    for (i = 0; i < NII_HEADER_BYTES; i++) h[i] = 0;
    put32(h, 0, 348);                                               /* sizeof_hdr */
    put16(h, 40, 3);                                                /* dim[0]: three dimensions */
    put16(h, 42, g->cols);
    put16(h, 44, g->rows);
    put16(h, 46, (uint16_t)g->count);
    put16(h, 48, 1); put16(h, 50, 1); put16(h, 52, 1); put16(h, 54, 1);
    put16(h, 70, (uint16_t)dt);
    put16(h, 72, (uint16_t)bitpix);
    putf(h, 76, 1.0);                                               /* pixdim[0] = qfac */
    putf(h, 80, g->pixdim[0]);
    putf(h, 84, g->pixdim[1]);
    putf(h, 88, g->pixdim[2]);
    putf(h, 108, 352.0);                                            /* vox_offset: right after the 4 extension bytes */
    putf(h, 112, s->slope);                                         /* scl_slope: true = slope * stored + inter */
    putf(h, 116, s->inter);
    h[123] = 2;                                                     /* xyzt_units: millimetres, no time unit */
    for (i = 0; i + 1 < sizeof DESCRIP; i++) h[148 + i] = (uint8_t)DESCRIP[i];
    put16(h, 252, 0);                                               /* qform_code: none; the sform is used */
    put16(h, 254, 1);                                               /* sform_code: scanner anatomical */
    for (i = 0; i < 12; i++) putf(h, 280 + 4 * i, g->affine[i]);    /* srow_x, srow_y, srow_z */
    h[344] = 'n'; h[345] = '+'; h[346] = '1'; h[347] = 0;           /* magic; h[348..351] stay zero: no extensions */
    return NII_OK;
}
