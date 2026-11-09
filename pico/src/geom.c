/* geom.c - stack geometry (see geom.h). Mirrors Stack in scripts/dicom_slice.py operation for operation. */
#include "geom.h"

#define ORTHO_TOL    1e-3        /* |r|^2 and |c|^2 within 2*ORTHO_TOL of 1, |r.c| within ORTHO_TOL of 0 */
#define IOP_SAME_TOL 1e-4        /* every slice has the first slice's orientation, per component */
#define STACK_TOL    0.01        /* a slice may be off its place in the regular stack by 1% of the step ... */
#define STACK_TOL_MM 1e-4        /* ... plus this many mm */

static const char *const GEOM_NAMES[GEOM_E_COUNT] = {"OK", "NO_GEOMETRY", "ORIENTATION", "MISMATCH", "SERIES",
                                                     "NOT_SORTED", "UNEVEN"};
const char *geom_err_name(int err) { return err >= 0 && err < GEOM_E_COUNT ? GEOM_NAMES[err] : "?"; }

/* ---------------------------------------------------------------- arithmetic helpers */
static double fabs_d(double x) { return x < 0 ? -x : x; }

static double dot3(const double *a, const double *b) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }

static void cross3(const double *a, const double *b, double *o)
{
    o[0] = a[1] * b[2] - a[2] * b[1];
    o[1] = a[2] * b[0] - a[0] * b[2];
    o[2] = a[0] * b[1] - a[1] * b[0];
}

/* Newton's method from an exponent-based guess, six iterations: within one unit in the last place of
 * the correctly rounded root. The same iteration runs in scripts/dicom_slice.py (_sqrt). */
static double sqrt_d(double x)
{
    union { double d; uint64_t u; } v;
    double g;
    int i;
    if (!(x > 0)) return 0.0;
    v.d = x;
    v.u = (v.u >> 1) + ((uint64_t)1023 << 51);
    g = v.d;
    for (i = 0; i < 6; i++) g = 0.5 * (g + x / g);
    return g;
}

/* m * 10^e. Exact (correctly rounded, one operation) when |m| < 2^53 and |e| <= 22, which covers every
 * DICOM decimal string of up to 16 characters; longer, non-conforming ones may be off in the last bit. */
static double dec_to_double(dcm_dec d)
{
    static const double P10[23] = {1e0, 1e1, 1e2, 1e3, 1e4, 1e5, 1e6, 1e7, 1e8, 1e9, 1e10, 1e11, 1e12, 1e13, 1e14,
                                   1e15, 1e16, 1e17, 1e18, 1e19, 1e20, 1e21, 1e22};
    int neg = d.m < 0, e = d.e;
    uint64_t m = neg ? (uint64_t)(-(d.m + 1)) + 1u : (uint64_t)d.m;
    double v;
    if (m == 0) return 0.0;
    v = (double)m;
    while (e > 22) { v *= P10[22]; e -= 22; }
    if (e > 0) v *= P10[e];
    while (e < -22) { v /= P10[22]; e += 22; }
    if (e < 0) v /= P10[-e];
    return neg ? -v : v;
}

double geom_dec_to_double(dcm_dec d) { return dec_to_double(d); }

static int same_uid(const geom_state *g, const dcm_str *s)
{
    uint32_t i;
    if (s->over || s->len != g->uid_len) return 0;
    for (i = 0; i < g->uid_len; i++)
        if (s->s[i] != g->uid[i]) return 0;
    return 1;
}

/* ---------------------------------------------------------------- the stack */
void geom_init(geom_state *g)
{
    uint8_t *p = (uint8_t *)g;
    uint32_t i;
    for (i = 0; i < sizeof *g; i++) p[i] = 0;
}

int geom_add(geom_state *g, const dcm_info *t)
{
    double ps[2], ipp[3], iop[6];
    unsigned i;
    int has_uid = (t->has & DCM_H_SERIES_UID) != 0;

    if (t->pixel_spacing.n != 2 || t->ipp.n != 3 || t->iop.n != 6) return GEOM_E_NO_GEOMETRY;
    for (i = 0; i < 2; i++) ps[i] = dec_to_double(t->pixel_spacing.v[i]);
    for (i = 0; i < 3; i++) ipp[i] = dec_to_double(t->ipp.v[i]);
    for (i = 0; i < 6; i++) iop[i] = dec_to_double(t->iop.v[i]);

    if (g->count == 0) {
        double rr, cc, rc;
        if (has_uid && t->series_uid.over) return GEOM_E_SERIES;
        if (!(ps[0] > 0) || !(ps[1] > 0)) return GEOM_E_NO_GEOMETRY;
        for (i = 0; i < 3; i++) { g->r[i] = iop[i]; g->c[i] = iop[3 + i]; }
        rr = dot3(g->r, g->r);
        cc = dot3(g->c, g->c);
        rc = dot3(g->r, g->c);
        if (fabs_d(rr - 1) > 2 * ORTHO_TOL || fabs_d(cc - 1) > 2 * ORTHO_TOL || fabs_d(rc) > ORTHO_TOL)
            return GEOM_E_ORIENTATION;
        g->rows = t->rows;
        g->cols = t->cols;
        g->ps_row = ps[0];
        g->ps_col = ps[1];
        for (i = 0; i < 6; i++) g->iop[i] = iop[i];
        cross3(g->r, g->c, g->n);
        for (i = 0; i < 3; i++) g->ipp0[i] = ipp[i];
        g->t_prev = dot3(g->ipp0, g->n);
        g->has_thickness = t->slice_thickness.n > 0;
        g->thickness = g->has_thickness ? dec_to_double(t->slice_thickness.v[0]) : 0.0;
        g->uid_len = has_uid ? t->series_uid.len : 0;
        for (i = 0; i < g->uid_len; i++) g->uid[i] = t->series_uid.s[i];
        g->uid[i] = 0;
        g->has_step = 0;
    } else {
        double tpos;
        dcm_str none;
        none.len = 0; none.over = 0; none.s[0] = 0;
        if (!same_uid(g, has_uid ? &t->series_uid : &none)) return GEOM_E_SERIES;
        if (t->rows != g->rows || t->cols != g->cols || ps[0] != g->ps_row || ps[1] != g->ps_col) return GEOM_E_MISMATCH;
        for (i = 0; i < 6; i++)
            if (fabs_d(iop[i] - g->iop[i]) > IOP_SAME_TOL) return GEOM_E_MISMATCH;
        tpos = dot3(ipp, g->n);
        if (!(tpos > g->t_prev)) return GEOM_E_NOT_SORTED;
        if (g->count == 1) {
            for (i = 0; i < 3; i++) g->step[i] = ipp[i] - g->ipp0[i];
            g->step_len = sqrt_d(dot3(g->step, g->step));
            g->has_step = 1;
        } else {
            double k = (double)g->count, tol = STACK_TOL * g->step_len + STACK_TOL_MM;
            for (i = 0; i < 3; i++)
                if (fabs_d(ipp[i] - (g->ipp0[i] + k * g->step[i])) > tol) return GEOM_E_UNEVEN;
        }
        g->t_prev = tpos;
    }
    g->count++;
    return GEOM_OK;
}

int geom_finish(geom_state *g, geom_result *o)
{
    double ax, ay, az, *s, *r = g->r, *c = g->c, *org = g->ipp0;
    unsigned i;
    o->thickness_assumed = 0;
    if (g->count == 0) return GEOM_E_NO_GEOMETRY;
    if (g->count == 1) {
        double L = (g->has_thickness && g->thickness > 0) ? g->thickness : 1.0;
        o->thickness_assumed = 1;
        for (i = 0; i < 3; i++) g->step[i] = g->n[i] * L;
        g->step_len = L;
    }
    s = g->step;
    /* voxel (i, j, k) -> position, converted from LPS to NIfTI's RAS by negating x and y */
    o->affine[0] = -(g->ps_col * r[0]); o->affine[1] = -(g->ps_row * c[0]); o->affine[2] = -s[0]; o->affine[3] = -org[0];
    o->affine[4] = -(g->ps_col * r[1]); o->affine[5] = -(g->ps_row * c[1]); o->affine[6] = -s[1]; o->affine[7] = -org[1];
    o->affine[8] = g->ps_col * r[2];    o->affine[9] = g->ps_row * c[2];    o->affine[10] = s[2]; o->affine[11] = org[2];
    ax = fabs_d(g->n[0]); ay = fabs_d(g->n[1]); az = fabs_d(g->n[2]);
    o->plane = (ax > ay && ax > az) ? 0 : (ay > az ? 1 : 2);
    o->thickness_mismatch = 0;
    if (g->count > 1 && g->has_thickness)
        o->thickness_mismatch = fabs_d(g->thickness - g->step_len) > STACK_TOL * g->step_len;
    o->cols = g->cols;
    o->rows = g->rows;
    o->count = g->count;
    o->pixdim[0] = g->ps_col;
    o->pixdim[1] = g->ps_row;
    o->pixdim[2] = g->step_len;
    return GEOM_OK;
}
