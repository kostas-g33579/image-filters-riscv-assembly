/* sidecar.c - the JSON that goes next to the sanitised NIfTI (see sidecar.h). */
#include "sidecar.h"

/* ---------------------------------------------------------------- a tiny JSON writer (no libc) */
typedef struct { char *p; uint32_t n, cap; int err; } W;

static void wc(W *w, char c)
{
    if (w->n + 1 >= w->cap) { w->err = 1; return; }
    w->p[w->n++] = c;
}
static void ws(W *w, const char *s) { while (*s) wc(w, *s++); }
static void wu(W *w, uint64_t v)
{
    char t[24];
    int n = 0;
    if (!v) { wc(w, '0'); return; }
    while (v) { t[n++] = (char)('0' + v % 10); v /= 10; }
    while (n) wc(w, t[--n]);
}
static void wi(W *w, int64_t v)
{
    if (v < 0) { wc(w, '-'); wu(w, (uint64_t)(-(v + 1)) + 1u); } else wu(w, (uint64_t)v);
}
static void nl(W *w, int indent) { wc(w, '\n'); while (indent--) wc(w, ' '); }

/* m * 10^e exactly, as a plain decimal when the exponent is small, else as a JSON exponent number */
static void w_dec(W *w, int64_t m, int e)
{
    char d[24];
    int L = 0, i;
    uint64_t a;
    if (m == 0) { wc(w, '0'); return; }
    if (m < 0) { wc(w, '-'); a = (uint64_t)(-(m + 1)) + 1u; } else a = (uint64_t)m;
    while (a) { d[L++] = (char)('0' + a % 10); a /= 10; }          /* d[L-1] is the most significant digit */
    if (e > 20 || e < -20) {
        for (i = L - 1; i >= 0; i--) wc(w, d[i]);
        wc(w, 'e'); wi(w, e);
        return;
    }
    if (e >= 0) {
        for (i = L - 1; i >= 0; i--) wc(w, d[i]);
        for (i = 0; i < e; i++) wc(w, '0');
    } else {
        int k = -e;
        if (k < L) {
            for (i = L - 1; i >= k; i--) wc(w, d[i]);
            wc(w, '.');
            for (i = k - 1; i >= 0; i--) wc(w, d[i]);
        } else {
            wc(w, '0'); wc(w, '.');
            for (i = 0; i < k - L; i++) wc(w, '0');
            for (i = L - 1; i >= 0; i--) wc(w, d[i]);
        }
    }
}
static void w_decv(W *w, dcm_dec d) { w_dec(w, d.m, d.e); }

/* A measured value: rounded to 9 decimals with integer arithmetic, trailing zeros dropped. */
static void w_meas(W *w, double x)
{
    int neg = x < 0;
    double ax = neg ? -x : x;
    uint64_t n = (uint64_t)(ax * 1e9 + 0.5), ip = n / 1000000000u, fr = n % 1000000000u;
    int i;
    char d[9];
    if (n == 0) { wc(w, '0'); return; }
    if (neg) wc(w, '-');
    wu(w, ip);
    if (fr) {
        for (i = 8; i >= 0; i--) { d[i] = (char)('0' + fr % 10); fr /= 10; }
        for (i = 8; i >= 0 && d[i] == '0'; i--) {}
        wc(w, '.');
        for (int k = 0; k <= i; k++) wc(w, d[k]);
    }
}

/* ---------------------------------------------------------------- checks on values */
static int ok_label_char(char c) { return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_' || c == '-'; }

int sc_valid_label(const char *s)
{
    unsigned n = 0;
    while (s[n]) { if (!ok_label_char(s[n])) return 0; if (++n > SC_LABEL_MAX) return 0; }
    return n >= 1;
}

/* free text (LO): letters, digits, space and  _ . + - : * / ( ) \   up to 64 characters */
static int ok_text(const dcm_str *s)
{
    unsigned i;
    if (s->over || s->len == 0) return 0;
    for (i = 0; i < s->len; i++) {
        char c = s->s[i];
        int ok = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == ' ' || c == '_' ||
                 c == '.' || c == '+' || c == '-' || c == ':' || c == '*' || c == '/' || c == '(' || c == ')' || c == '\\';
        if (!ok) return 0;
    }
    return 1;
}
/* code strings (CS): A-Z 0-9 space _ \  */
static int ok_code(const dcm_str *s)
{
    unsigned i;
    if (s->over || s->len == 0) return 0;
    for (i = 0; i < s->len; i++) {
        char c = s->s[i];
        if (!((c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == ' ' || c == '_' || c == '\\')) return 0;
    }
    return 1;
}
static void w_str(W *w, const dcm_str *s)
{
    unsigned i;
    wc(w, '"');
    for (i = 0; i < s->len; i++) {
        if (s->s[i] == '\\') ws(w, "\\\\"); else wc(w, s->s[i]);
    }
    wc(w, '"');
}
static void w_cstr(W *w, const char *s) { wc(w, '"'); ws(w, s); wc(w, '"'); }

static int in_range(dcm_dec d, double lo, double hi)
{
    double v = geom_dec_to_double(d);
    return v >= lo && v <= hi;
}
#define PRESENT(t, bit, str) (((t)->has & (bit)) && (str).len > 0)

static dcm_dec shift10(dcm_dec d, int by) { d.e = (int16_t)(d.e + by); return d; }

/* ---------------------------------------------------------------- state */
void sc_init(sc_state *s)
{
    uint8_t *p = (uint8_t *)s;
    uint32_t i;
    for (i = 0; i < sizeof *s; i++) p[i] = 0;
}

static int tag_class(const dcm_info *t, uint32_t bit, const dcm_str *v)     /* 0 YES, 1 NO, 2 other, 3 absent */
{
    if (!(t->has & bit)) return 3;
    if (!v->over && v->len == 3 && v->s[0] == 'Y' && v->s[1] == 'E' && v->s[2] == 'S') return 0;
    if (!v->over && v->len == 2 && v->s[0] == 'N' && v->s[1] == 'O') return 1;
    return 2;
}

void sc_add(sc_state *s, const dcm_info *t)
{
    unsigned i;
    if (s->n == 0) s->first = *t;
    s->n++;
    for (i = 0; i < DCM_N_IDENTIFYING; i++)
        if (t->identifying & (1u << i)) s->ident[i]++;
    s->burned[tag_class(t, DCM_H_BURNED_IN, &t->burned_in)]++;
    s->removed[tag_class(t, DCM_H_ID_REMOVED, &t->identity_removed)]++;
}

/* ---------------------------------------------------------------- the document */
static void w_member(W *w, int *first, int indent, const char *key)
{
    if (!*first) wc(w, ',');
    *first = 0;
    nl(w, indent);
    w_cstr(w, key);
    ws(w, ": ");
}

/* the separator before the next array element: a comma unless it is the first, then a new line */
static void w_sep(W *w, int *first, int indent)
{
    if (!*first) wc(w, ',');
    *first = 0;
    nl(w, indent);
}

static void w_dec3(W *w, const double *v)
{
    wc(w, '[');
    w_meas(w, v[0]); ws(w, ", "); w_meas(w, v[1]); ws(w, ", "); w_meas(w, v[2]);
    wc(w, ']');
}

static void w_counts(W *w, const uint32_t *c)
{
    ws(w, "{\"YES\": "); wu(w, c[0]); ws(w, ", \"NO\": "); wu(w, c[1]);
    ws(w, ", \"other\": "); wu(w, c[2]); ws(w, ", \"absent\": "); wu(w, c[3]); wc(w, '}');
}

static const char *const TEXT_FIELDS[7] = {"Manufacturer", "ManufacturersModelName", "SoftwareVersions", "ProtocolName",
                                           "ScanningSequence", "SequenceVariant", "MRAcquisitionType"};
static const char *const RANGE_FIELDS[5] = {"RepetitionTime", "EchoTime", "InversionTime", "FlipAngle", "SliceThickness"};

int sc_build(const sc_state *s, const sc_labels *lab, const nii_state *ns, const geom_result *g, char *out, uint32_t cap)
{
    W w;
    const dcm_info *t = &s->first;
    int first, bitpix, dt = nii_datatype(ns, &bitpix), i;
    unsigned text_rej = 0, range_rej = 0;
    int fs_status = 0;                       /* 0 absent, 1 tesla, 2 converted from gauss, 3 rejected */
    dcm_dec fs_used = {0, 0};
    static const char *const PLANE[3] = {"sagittal", "coronal", "axial"};
    const char *dtname = dt == 2 ? "uint8" : dt == 256 ? "int8" : dt == 4 ? "int16" : "uint16";
    uint32_t cols = g->cols, rows = g->rows;

    w.p = out; w.n = 0; w.cap = cap; w.err = 0;
    wc(&w, '{');
    first = 1;
    w_member(&w, &first, 2, "tool");
    ws(&w, "{\"name\": \"dicom-sanitiser\", \"format_version\": 1}");

    w_member(&w, &first, 2, "labels");
    {
        int f2 = 1;
        wc(&w, '{');
        if (lab->subject[0])  { w_member(&w, &f2, 4, "subject");  w_cstr(&w, lab->subject); }
        if (lab->sequence[0]) { w_member(&w, &f2, 4, "sequence"); w_cstr(&w, lab->sequence); }
        if (lab->site[0])     { w_member(&w, &f2, 4, "site");     w_cstr(&w, lab->site); }
        if (lab->scanner[0])  { w_member(&w, &f2, 4, "scanner");  w_cstr(&w, lab->scanner); }
        nl(&w, 2); wc(&w, '}');
    }

    /* ---- measured */
    w_member(&w, &first, 2, "measured");
    {
        int f2 = 1;
        double fov[3];
        wc(&w, '{');
        w_member(&w, &f2, 4, "size");
        wc(&w, '['); wu(&w, cols); ws(&w, ", "); wu(&w, rows); ws(&w, ", "); wu(&w, g->count); wc(&w, ']');
        w_member(&w, &f2, 4, "voxel_size_mm"); w_dec3(&w, g->pixdim);
        w_member(&w, &f2, 4, "plane"); w_cstr(&w, PLANE[g->plane]);
        fov[0] = (double)cols * g->pixdim[0]; fov[1] = (double)rows * g->pixdim[1]; fov[2] = (double)g->count * g->pixdim[2];
        w_member(&w, &f2, 4, "field_of_view_mm"); w_dec3(&w, fov);
        w_member(&w, &f2, 4, "voxel_to_ras");
        wc(&w, '[');
        for (i = 0; i < 12; i++) { if (i) ws(&w, ", "); w_meas(&w, g->affine[i]); }
        wc(&w, ']');
        w_member(&w, &f2, 4, "datatype"); w_cstr(&w, dtname);
        w_member(&w, &f2, 4, "bits_stored"); wu(&w, ns->bits_stored);
        nl(&w, 2); wc(&w, '}');
    }

    /* ---- from_tags */
    w_member(&w, &first, 2, "from_tags");
    {
        int f2 = 1;
        wc(&w, '{');
        if (t->tr.n > 0) {
            if (in_range(t->tr.v[0], 0, 1e6)) { w_member(&w, &f2, 4, "RepetitionTime"); w_decv(&w, shift10(t->tr.v[0], -3)); }
            else range_rej |= 1u << 0;
        }
        if (t->te.n > 0) {
            if (in_range(t->te.v[0], 0, 1e5)) { w_member(&w, &f2, 4, "EchoTime"); w_decv(&w, shift10(t->te.v[0], -3)); }
            else range_rej |= 1u << 1;
        }
        if (t->ti.n > 0) {
            if (in_range(t->ti.v[0], 0, 1e5)) { w_member(&w, &f2, 4, "InversionTime"); w_decv(&w, shift10(t->ti.v[0], -3)); }
            else range_rej |= 1u << 2;
        }
        if (t->flip.n > 0) {
            if (in_range(t->flip.v[0], 0, 360)) { w_member(&w, &f2, 4, "FlipAngle"); w_decv(&w, t->flip.v[0]); }
            else range_rej |= 1u << 3;
        }
        if (t->field_strength.n > 0) {
            dcm_dec d = t->field_strength.v[0];
            if (in_range(d, 0.1, 12)) { fs_status = 1; fs_used = d; }
            else if (in_range(d, 1000, 120000)) { fs_status = 2; fs_used = shift10(d, -4); }      /* gauss -> tesla */
            else fs_status = 3;
            if (fs_status == 1 || fs_status == 2) { w_member(&w, &f2, 4, "MagneticFieldStrength"); w_decv(&w, fs_used); }
        }
        if (PRESENT(t, DCM_H_SCAN_SEQ, t->scan_seq)) {
            if (ok_code(&t->scan_seq)) { w_member(&w, &f2, 4, "ScanningSequence"); w_str(&w, &t->scan_seq); } else text_rej |= 1u << 4;
        }
        if (PRESENT(t, DCM_H_SEQ_VAR, t->seq_var)) {
            if (ok_code(&t->seq_var)) { w_member(&w, &f2, 4, "SequenceVariant"); w_str(&w, &t->seq_var); } else text_rej |= 1u << 5;
        }
        if (PRESENT(t, DCM_H_ACQ_TYPE, t->acq_type)) {
            if (ok_code(&t->acq_type)) { w_member(&w, &f2, 4, "MRAcquisitionType"); w_str(&w, &t->acq_type); } else text_rej |= 1u << 6;
        }
        w_member(&w, &f2, 4, "ContrastAgentDeclared");
        ws(&w, t->contrast_declared ? "true" : "null");         /* null: the scanner did not say; NOT "no contrast" */
        if (PRESENT(t, DCM_H_MANUFACTURER, t->manufacturer)) {
            if (ok_text(&t->manufacturer)) { w_member(&w, &f2, 4, "Manufacturer"); w_str(&w, &t->manufacturer); } else text_rej |= 1u << 0;
        }
        if (PRESENT(t, DCM_H_MODEL, t->model)) {
            if (ok_text(&t->model)) { w_member(&w, &f2, 4, "ManufacturersModelName"); w_str(&w, &t->model); } else text_rej |= 1u << 1;
        }
        if (PRESENT(t, DCM_H_SOFTWARE, t->software)) {
            if (ok_text(&t->software)) { w_member(&w, &f2, 4, "SoftwareVersions"); w_str(&w, &t->software); } else text_rej |= 1u << 2;
        }
        if (PRESENT(t, DCM_H_PROTOCOL, t->protocol)) {
            if (ok_text(&t->protocol)) { w_member(&w, &f2, 4, "ProtocolName"); w_str(&w, &t->protocol); } else text_rej |= 1u << 3;
        }
        if (t->slice_thickness.n > 0) {
            if (in_range(t->slice_thickness.v[0], 1e-6, 1000)) { w_member(&w, &f2, 4, "SliceThickness"); w_decv(&w, t->slice_thickness.v[0]); }
            else range_rej |= 1u << 4;
        }
        w_member(&w, &f2, 4, "RescaleSlope");
        if (t->rescale_slope.n > 0) w_decv(&w, t->rescale_slope.v[0]); else wc(&w, '1');
        w_member(&w, &f2, 4, "RescaleIntercept");
        if (t->rescale_intercept.n > 0) w_decv(&w, t->rescale_intercept.v[0]); else wc(&w, '0');
        nl(&w, 2); wc(&w, '}');
    }

    /* ---- discrepancies */
    w_member(&w, &first, 2, "discrepancies");
    {
        int f2 = 1;
        wc(&w, '[');
        if (fs_status == 2) {
            w_sep(&w, &f2, 4);
            ws(&w, "{\"what\": \"field_strength_converted_from_gauss\", \"tag\": "); w_decv(&w, t->field_strength.v[0]);
            ws(&w, ", \"used_tesla\": "); w_decv(&w, fs_used); wc(&w, '}');
        } else if (fs_status == 3) {
            w_sep(&w, &f2, 4);
            ws(&w, "{\"what\": \"field_strength_out_of_range\", \"tag\": "); w_decv(&w, t->field_strength.v[0]); wc(&w, '}');
        }
        for (i = 0; i < 7; i++)
            if (text_rej & (1u << i)) {
                w_sep(&w, &f2, 4);
                ws(&w, "{\"what\": \"text_rejected\", \"field\": "); w_cstr(&w, TEXT_FIELDS[i]); wc(&w, '}');
            }
        for (i = 0; i < 5; i++)
            if (range_rej & (1u << i)) {
                w_sep(&w, &f2, 4);
                ws(&w, "{\"what\": \"value_out_of_range\", \"field\": "); w_cstr(&w, RANGE_FIELDS[i]); wc(&w, '}');
            }
        if (g->thickness_mismatch) {
            w_sep(&w, &f2, 4);
            ws(&w, "{\"what\": \"slice_thickness_differs_from_spacing\", \"tag_mm\": "); w_decv(&w, t->slice_thickness.v[0]);
            ws(&w, ", \"measured_mm\": "); w_meas(&w, g->pixdim[2]); wc(&w, '}');
        }
        if (g->thickness_assumed) {
            w_sep(&w, &f2, 4);
            ws(&w, "{\"what\": \"slice_spacing_assumed_from_thickness\", \"used_mm\": "); w_meas(&w, g->pixdim[2]); wc(&w, '}');
        }
        if (!f2) nl(&w, 2);
        wc(&w, ']');
    }

    /* ---- audit */
    w_member(&w, &first, 2, "audit");
    {
        int f2 = 1, f3;
        wc(&w, '{');
        w_member(&w, &f2, 4, "slices"); wu(&w, s->n);
        w_member(&w, &f2, 4, "identifying_fields_present_values_never_read");
        wc(&w, '{');
        f3 = 1;
        for (i = 0; i < DCM_N_IDENTIFYING; i++)
            if (s->ident[i]) { w_member(&w, &f3, 6, dcm_identifying_name((unsigned)i)); wu(&w, s->ident[i]); }
        nl(&w, 4);
        wc(&w, '}');
        w_member(&w, &f2, 4, "all_other_tags"); w_cstr(&w, "dropped");
        w_member(&w, &f2, 4, "pixels");
        w_cstr(&w, "the file's own integers, re-packed to the stated type; not filtered, not rescaled");
        w_member(&w, &f2, 4, "burned_in_text"); w_cstr(&w, "not detected and not removed");
        w_member(&w, &f2, 4, "faces"); w_cstr(&w, "not removed; a 3D head volume can allow a face to be reconstructed");
        w_member(&w, &f2, 4, "burned_in_annotation_tag_by_slice"); w_counts(&w, s->burned);
        w_member(&w, &f2, 4, "patient_identity_removed_tag_by_slice"); w_counts(&w, s->removed);
        w_member(&w, &f2, 4, "free_text_copied");
        wc(&w, '[');
        {
            int f4 = 1;
            for (i = 0; i < 4; i++) {
                int emitted = 0;
                switch (i) {
                case 0: emitted = PRESENT(t, DCM_H_MANUFACTURER, t->manufacturer) && !(text_rej & (1u << 0)); break;
                case 1: emitted = PRESENT(t, DCM_H_MODEL, t->model) && !(text_rej & (1u << 1)); break;
                case 2: emitted = PRESENT(t, DCM_H_SOFTWARE, t->software) && !(text_rej & (1u << 2)); break;
                default: emitted = PRESENT(t, DCM_H_PROTOCOL, t->protocol) && !(text_rej & (1u << 3)); break;
                }
                if (emitted) { if (!f4) ws(&w, ", "); f4 = 0; w_cstr(&w, TEXT_FIELDS[i]); }
            }
        }
        wc(&w, ']');
        nl(&w, 2); wc(&w, '}');
    }
    nl(&w, 0);
    wc(&w, '}');
    wc(&w, '\n');
    if (w.err) return -1;
    w.p[w.n] = 0;
    return (int)w.n;
}
