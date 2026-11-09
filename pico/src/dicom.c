/* dicom.c - the allow-list DICOM reader (see dicom.h). Mirrors scripts/dicom_slice.py. */
#include "dicom.h"

/* ---------------------------------------------------------------- the allow-list */
enum kind { K_UI, K_CS, K_LO, K_US, K_DS, K_IS, K_BOOL };
enum slot {
    S_TS, S_IMAGE_TYPE, S_SOP, S_MODALITY, S_MANUFACTURER, S_MODEL, S_ID_REMOVED, S_CONTRAST, S_SCAN_SEQ,
    S_SEQ_VAR, S_ACQ_TYPE, S_THICKNESS, S_TR, S_TE, S_TI, S_FIELD, S_SOFTWARE, S_PROTOCOL, S_FLIP, S_SERIES_UID,
    S_INSTANCE, S_IPP, S_IOP, S_SAMPLES, S_PHOTOMETRIC, S_PLANAR, S_FRAMES, S_ROWS, S_COLS, S_PIXEL_SPACING,
    S_BITS_ALLOC, S_BITS_STORED, S_HIGH_BIT, S_PIXEL_REP, S_BURNED_IN, S_WC, S_WW, S_RINT, S_RSLOPE
};
typedef struct { uint16_t g, e; uint8_t kind, slot; } wanted_t;

/* The ONLY tags whose values are ever read. Identifying tags are not in this table. */
static const wanted_t WANTED[] = {
    {0x0002, 0x0010, K_UI, S_TS},          {0x0008, 0x0008, K_CS, S_IMAGE_TYPE},
    {0x0008, 0x0016, K_UI, S_SOP},         {0x0008, 0x0060, K_CS, S_MODALITY},
    {0x0008, 0x0070, K_LO, S_MANUFACTURER}, {0x0008, 0x1090, K_LO, S_MODEL},
    {0x0012, 0x0062, K_CS, S_ID_REMOVED},  {0x0018, 0x0010, K_BOOL, S_CONTRAST},
    {0x0018, 0x0020, K_CS, S_SCAN_SEQ},    {0x0018, 0x0021, K_CS, S_SEQ_VAR},
    {0x0018, 0x0023, K_CS, S_ACQ_TYPE},    {0x0018, 0x0050, K_DS, S_THICKNESS},
    {0x0018, 0x0080, K_DS, S_TR},          {0x0018, 0x0081, K_DS, S_TE},
    {0x0018, 0x0082, K_DS, S_TI},          {0x0018, 0x0087, K_DS, S_FIELD},
    {0x0018, 0x1020, K_LO, S_SOFTWARE},    {0x0018, 0x1030, K_LO, S_PROTOCOL},
    {0x0018, 0x1314, K_DS, S_FLIP},        {0x0020, 0x000E, K_UI, S_SERIES_UID},
    {0x0020, 0x0013, K_IS, S_INSTANCE},    {0x0020, 0x0032, K_DS, S_IPP},
    {0x0020, 0x0037, K_DS, S_IOP},         {0x0028, 0x0002, K_US, S_SAMPLES},
    {0x0028, 0x0004, K_CS, S_PHOTOMETRIC}, {0x0028, 0x0006, K_US, S_PLANAR},
    {0x0028, 0x0008, K_IS, S_FRAMES},      {0x0028, 0x0010, K_US, S_ROWS},
    {0x0028, 0x0011, K_US, S_COLS},        {0x0028, 0x0030, K_DS, S_PIXEL_SPACING},
    {0x0028, 0x0100, K_US, S_BITS_ALLOC},  {0x0028, 0x0101, K_US, S_BITS_STORED},
    {0x0028, 0x0102, K_US, S_HIGH_BIT},    {0x0028, 0x0103, K_US, S_PIXEL_REP},
    {0x0028, 0x0301, K_CS, S_BURNED_IN},   {0x0028, 0x1050, K_DS, S_WC},
    {0x0028, 0x1051, K_DS, S_WW},          {0x0028, 0x1052, K_DS, S_RINT},
    {0x0028, 0x1053, K_DS, S_RSLOPE},
};
#define N_WANTED (sizeof WANTED / sizeof WANTED[0])

/* Tags that identify a person or a site: only their PRESENCE (a non-empty value) is recorded. */
typedef struct { uint16_t g, e; const char *name; } ident_t;
static const ident_t IDENT[DCM_N_IDENTIFYING] = {
    {0x0008, 0x0020, "StudyDate"}, {0x0008, 0x0021, "SeriesDate"}, {0x0008, 0x0022, "AcquisitionDate"},
    {0x0008, 0x0023, "ContentDate"}, {0x0008, 0x0050, "AccessionNumber"}, {0x0008, 0x0080, "InstitutionName"},
    {0x0008, 0x0081, "InstitutionAddress"}, {0x0008, 0x0090, "ReferringPhysicianName"},
    {0x0008, 0x1030, "StudyDescription"}, {0x0008, 0x1050, "PerformingPhysicianName"},
    {0x0008, 0x1070, "OperatorsName"}, {0x0008, 0x1010, "StationName"}, {0x0018, 0x1000, "DeviceSerialNumber"},
    {0x0008, 0x103E, "SeriesDescription"}, {0x0010, 0x0010, "PatientName"}, {0x0010, 0x0020, "PatientID"},
    {0x0010, 0x0030, "PatientBirthDate"}, {0x0010, 0x0040, "PatientSex"}, {0x0010, 0x1010, "PatientAge"},
    {0x0010, 0x1020, "PatientSize"}, {0x0010, 0x1030, "PatientWeight"}, {0x0010, 0x1040, "PatientAddress"},
    {0x0010, 0x2154, "PatientTelephoneNumbers"}, {0x0010, 0x4000, "PatientComments"},
};

const char *dcm_identifying_name(unsigned i) { return i < DCM_N_IDENTIFYING ? IDENT[i].name : (const char *)0; }

static const char *const ERR_NAMES[DCM_E_COUNT] = {
    "OK", "PREAMBLE", "TOO_MANY", "NO_TS", "TS", "TRUNC_HEADER", "BAD_VR", "DEPTH", "NOT_ITEM", "RUNS_PAST",
    "PIXEL_TRUNC", "ENCAPSULATED", "VR_MISMATCH", "MALFORMED", "ONLY_META", "NO_PIXEL", "MISSING_TAG", "COLOUR",
    "PHOTOMETRIC", "MULTIFRAME", "SIZE", "BITS_ALLOC", "BITS_INCONSISTENT", "PIXEL_REP", "PIXEL_SHORT",
};
const char *dcm_err_name(int err) { return err >= 0 && err < DCM_E_COUNT ? ERR_NAMES[err] : "?"; }

/* ---------------------------------------------------------------- small helpers (no libc) */
static uint16_t rd16(const uint8_t *p) { return (uint16_t)(p[0] | (p[1] << 8)); }
static uint32_t rd32(const uint8_t *p) { return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24); }

/* the only whitespace ever stripped: ASCII space, tab, newline, vertical tab, form feed, carriage return */
static int is_ws(uint8_t c) { return c == ' ' || (c >= 9 && c <= 13); }

static void zero(void *dst, uint32_t n) { uint8_t *d = (uint8_t *)dst; while (n--) *d++ = 0; }

/* Text value: cut at the first NUL, drop trailing spaces, strip ASCII whitespace at both ends.
 * *cut_out receives the end of the NUL/space-trimmed bytes (before the whitespace strip), which
 * the number parsers need. Keeps at most DCM_STR_MAX bytes; .len is the full length. */
static uint32_t text_cut(const uint8_t *v, uint32_t n)
{
    uint32_t cut = n, i;
    for (i = 0; i < n; i++)
        if (v[i] == 0) { cut = i; break; }
    while (cut > 0 && (v[cut - 1] == ' ' || v[cut - 1] == 0)) cut--;
    return cut;
}

static void get_text(const uint8_t *v, uint32_t n, dcm_str *o)
{
    uint32_t cut = text_cut(v, n), lo = 0, hi = cut, len, i;
    while (lo < hi && is_ws(v[lo])) lo++;
    while (hi > lo && is_ws(v[hi - 1])) hi--;
    len = hi - lo;
    o->over = len > DCM_STR_MAX;
    o->len = (uint8_t)(len > 255 ? 255 : len);
    for (i = 0; i < len && i < DCM_STR_MAX; i++) o->s[i] = (char)v[lo + i];
    o->s[i] = 0;
}

static int str_is(const dcm_str *s, const char *lit)
{
    uint32_t i = 0;
    if (s->over) return 0;
    while (lit[i]) i++;
    if (s->len != i) return 0;
    for (i = 0; i < s->len; i++)
        if (s->s[i] != lit[i]) return 0;
    return 1;
}

/* One decimal string -> exact (m, e). Grammar: [+-]? (digits [. digits*] | . digits) ([eE] [+-]? 1-3 digits)?
 * with at most 18 mantissa digits and at most 64 characters. Returns 0 on success, -1 if malformed. */
static int parse_dec(const uint8_t *s, uint32_t n, dcm_dec *out)
{
    uint32_t i = 0, ndig = 0, frac = 0;
    uint64_t m = 0;
    int neg = 0, seen_point = 0, ex = 0, exneg = 0, exdig = 0;
    if (n == 0 || n > 64) return -1;
    if (s[i] == '+' || s[i] == '-') { neg = s[i] == '-'; i++; }
    for (; i < n; i++) {
        if (s[i] >= '0' && s[i] <= '9') {
            if (++ndig > 18) return -1;
            m = m * 10 + (uint64_t)(s[i] - '0');
            if (seen_point) frac++;
        } else if (s[i] == '.' && !seen_point) {
            seen_point = 1;
        } else {
            break;
        }
    }
    if (ndig == 0) return -1;                                 /* "." and "e5" have no digits */
    if (i < n && (s[i] == 'e' || s[i] == 'E')) {
        i++;
        if (i < n && (s[i] == '+' || s[i] == '-')) { exneg = s[i] == '-'; i++; }
        for (; i < n && s[i] >= '0' && s[i] <= '9'; i++) {
            if (++exdig > 3) return -1;
            ex = ex * 10 + (s[i] - '0');
        }
        if (exdig == 0) return -1;
    }
    if (i != n) return -1;
    {
        int e = (exneg ? -ex : ex) - (int)frac;
        if (m == 0) { out->m = 0; out->e = 0; return 0; }
        while (m % 10 == 0) { m /= 10; e++; }
        out->m = neg ? -(int64_t)m : (int64_t)m;
        out->e = (int16_t)e;
    }
    return 0;
}

/* A DS value: backslash-separated decimals; empty or whitespace-only parts are skipped. */
static int get_decimals(const uint8_t *v, uint32_t n, dcm_declist *o)
{
    uint32_t cut = text_cut(v, n), i = 0;
    o->n = 0;
    while (i <= cut) {
        uint32_t j = i, lo, hi;
        while (j < cut && v[j] != '\\') j++;
        lo = i; hi = j;
        while (lo < hi && is_ws(v[lo])) lo++;
        while (hi > lo && is_ws(v[hi - 1])) hi--;
        if (hi > lo) {
            dcm_dec d;
            if (parse_dec(v + lo, hi - lo, &d)) return -1;
            if (o->n < DCM_DEC_MAX) o->v[o->n] = d;
            if (o->n < 255) o->n++;
        }
        i = j + 1;
    }
    return 0;
}

/* An IS value: only the FIRST non-empty part is read. [+-]? 1-12 digits. *present = 0 for no parts. */
static int get_integer(const uint8_t *v, uint32_t n, int64_t *out, int *present)
{
    uint32_t cut = text_cut(v, n), i = 0;
    *present = 0;
    while (i <= cut) {
        uint32_t j = i, lo, hi, k, nd = 0;
        int neg = 0;
        int64_t acc = 0;
        while (j < cut && v[j] != '\\') j++;
        lo = i; hi = j;
        while (lo < hi && is_ws(v[lo])) lo++;
        while (hi > lo && is_ws(v[hi - 1])) hi--;
        if (hi > lo) {
            k = lo;
            if (v[k] == '+' || v[k] == '-') { neg = v[k] == '-'; k++; }
            for (; k < hi; k++) {
                if (v[k] < '0' || v[k] > '9') return -1;
                acc = acc * 10 + (v[k] - '0');
                nd++;
            }
            if (nd < 1 || nd > 12) return -1;
            *out = neg ? -acc : acc;
            *present = 1;
            return 0;
        }
        i = j + 1;
    }
    return 0;
}

/* ---------------------------------------------------------------- element walk */
typedef struct { uint16_t g, e; uint8_t vr0, vr1; uint32_t len, q; } elem_t;

/* 0 = not a value representation, 1 = 2-byte length, 2 = 4-byte length (after 2 reserved bytes) */
static int vr_class(uint8_t a, uint8_t b)
{
    static const char LONGV[][2] = {{'O','B'},{'O','W'},{'O','F'},{'S','Q'},{'U','T'},{'U','N'},{'O','D'},{'O','L'},
                                    {'U','C'},{'U','R'},{'O','V'},{'S','V'},{'U','V'}};
    static const char SHORTV[][2] = {{'A','E'},{'A','S'},{'A','T'},{'C','S'},{'D','A'},{'D','S'},{'D','T'},{'F','L'},
                                     {'F','D'},{'I','S'},{'L','O'},{'L','T'},{'P','N'},{'S','H'},{'S','L'},{'S','S'},
                                     {'S','T'},{'T','M'},{'U','I'},{'U','L'},{'U','S'}};
    unsigned i;
    for (i = 0; i < sizeof LONGV / sizeof LONGV[0]; i++)
        if (LONGV[i][0] == (char)a && LONGV[i][1] == (char)b) return 2;
    for (i = 0; i < sizeof SHORTV / sizeof SHORTV[0]; i++)
        if (SHORTV[i][0] == (char)a && SHORTV[i][1] == (char)b) return 1;
    return 0;
}

static int read_element(const uint8_t *b, uint32_t p, int explicit_vr, uint32_t end, elem_t *el)
{
    int cls;
    if ((uint64_t)p + 8 > end) return DCM_E_TRUNC_HEADER;
    el->g = rd16(b + p);
    el->e = rd16(b + p + 2);
    el->vr0 = el->vr1 = 0;
    if (el->g == 0xFFFE) {                                  /* item / delimiter: always 8 bytes, implicit style */
        el->len = rd32(b + p + 4);
        el->q = p + 8;
        return DCM_OK;
    }
    if (explicit_vr) {
        el->vr0 = b[p + 4];
        el->vr1 = b[p + 5];
        cls = vr_class(el->vr0, el->vr1);
        if (!cls) return DCM_E_BAD_VR;
        if (cls == 2) {
            if ((uint64_t)p + 12 > end) return DCM_E_TRUNC_HEADER;
            el->len = rd32(b + p + 8);
            el->q = p + 12;
        } else {
            el->len = rd16(b + p + 6);
            el->q = p + 8;
        }
        return DCM_OK;
    }
    el->len = rd32(b + p + 4);
    el->q = p + 8;
    return DCM_OK;
}

/* p is just after an undefined-length element header: skip its items up to the sequence delimiter. */
static int skip_undefined(const uint8_t *b, uint32_t p, int explicit_vr, uint32_t end, int depth,
                          uint32_t *counter, uint32_t *newp)
{
    elem_t it, el;
    int r;
    if (depth > DCM_MAX_DEPTH) return DCM_E_DEPTH;
    for (;;) {
        if (++*counter > DCM_MAX_ELEMENTS) return DCM_E_TOO_MANY;
        r = read_element(b, p, 0, end, &it);
        if (r) return r;
        if (it.g == 0xFFFE && it.e == 0xE0DD) { *newp = it.q; return DCM_OK; }
        if (!(it.g == 0xFFFE && it.e == 0xE000)) return DCM_E_NOT_ITEM;
        if (it.len == 0xFFFFFFFFu) {                        /* item of undefined length: elements up to its delimiter */
            p = it.q;
            for (;;) {
                if (++*counter > DCM_MAX_ELEMENTS) return DCM_E_TOO_MANY;
                r = read_element(b, p, explicit_vr, end, &el);
                if (r) return r;
                if (el.g == 0xFFFE && el.e == 0xE00D) { p = el.q; break; }
                if (el.len == 0xFFFFFFFFu) {
                    r = skip_undefined(b, el.q, explicit_vr, end, depth + 1, counter, &p);
                    if (r) return r;
                } else {
                    if ((uint64_t)el.q + el.len > end) return DCM_E_RUNS_PAST;
                    p = el.q + el.len;
                }
            }
        } else {
            if ((uint64_t)it.q + it.len > end) return DCM_E_RUNS_PAST;
            p = it.q + it.len;
        }
    }
}

/* ---------------------------------------------------------------- storing a decoded value */
static dcm_str *str_slot(dcm_info *o, int slot, uint32_t *bit)
{
    switch (slot) {
    case S_TS: *bit = DCM_H_TS; return &o->ts;
    case S_IMAGE_TYPE: *bit = DCM_H_IMAGE_TYPE; return &o->image_type;
    case S_SOP: *bit = DCM_H_SOP; return &o->sop_class;
    case S_MODALITY: *bit = DCM_H_MODALITY; return &o->modality;
    case S_MANUFACTURER: *bit = DCM_H_MANUFACTURER; return &o->manufacturer;
    case S_MODEL: *bit = DCM_H_MODEL; return &o->model;
    case S_ID_REMOVED: *bit = DCM_H_ID_REMOVED; return &o->identity_removed;
    case S_SCAN_SEQ: *bit = DCM_H_SCAN_SEQ; return &o->scan_seq;
    case S_SEQ_VAR: *bit = DCM_H_SEQ_VAR; return &o->seq_var;
    case S_ACQ_TYPE: *bit = DCM_H_ACQ_TYPE; return &o->acq_type;
    case S_SERIES_UID: *bit = DCM_H_SERIES_UID; return &o->series_uid;
    case S_PHOTOMETRIC: *bit = DCM_H_PHOTOMETRIC; return &o->photometric;
    case S_SOFTWARE: *bit = DCM_H_SOFTWARE; return &o->software;
    case S_PROTOCOL: *bit = DCM_H_PROTOCOL; return &o->protocol;
    case S_BURNED_IN: *bit = DCM_H_BURNED_IN; return &o->burned_in;
    default: return (dcm_str *)0;
    }
}

static dcm_declist *dec_slot(dcm_info *o, int slot)
{
    switch (slot) {
    case S_THICKNESS: return &o->slice_thickness;
    case S_TR: return &o->tr;
    case S_TE: return &o->te;
    case S_TI: return &o->ti;
    case S_FIELD: return &o->field_strength;
    case S_FLIP: return &o->flip;
    case S_IPP: return &o->ipp;
    case S_IOP: return &o->iop;
    case S_PIXEL_SPACING: return &o->pixel_spacing;
    case S_WC: return &o->window_center;
    case S_WW: return &o->window_width;
    case S_RINT: return &o->rescale_intercept;
    case S_RSLOPE: return &o->rescale_slope;
    default: return (dcm_declist *)0;
    }
}

static int store(dcm_info *o, const wanted_t *w, const uint8_t *v, uint32_t n)
{
    uint32_t bit = 0;
    dcm_str *s;
    dcm_declist *d;
    switch (w->kind) {
    case K_UI: case K_CS: case K_LO:
        s = str_slot(o, w->slot, &bit);
        get_text(v, n, s);
        o->has |= bit;
        return DCM_OK;
    case K_BOOL: {
        dcm_str tmp;
        get_text(v, n, &tmp);
        o->contrast_declared = tmp.len > 0;
        o->has |= DCM_H_CONTRAST;
        return DCM_OK;
    }
    case K_DS:
        d = dec_slot(o, w->slot);
        return get_decimals(v, n, d) ? DCM_E_MALFORMED : DCM_OK;
    case K_IS: {
        int64_t val = 0;
        int present;
        if (get_integer(v, n, &val, &present)) return DCM_E_MALFORMED;
        bit = w->slot == S_INSTANCE ? DCM_H_INSTANCE : DCM_H_FRAMES;
        if (!present) { o->has &= ~bit; return DCM_OK; }    /* an empty IS replaces an earlier value with "none" */
        if (w->slot == S_INSTANCE) o->instance_number = val; else o->frames = val;
        o->has |= bit;
        return DCM_OK;
    }
    case K_US: {
        uint16_t val;
        if (n < 2) return DCM_E_MALFORMED;
        val = rd16(v);
        switch (w->slot) {
        case S_SAMPLES: o->samples = val; bit = DCM_H_SAMPLES; break;
        case S_PLANAR: o->planar = val; bit = DCM_H_PLANAR; break;
        case S_ROWS: o->rows = val; bit = DCM_H_ROWS; break;
        case S_COLS: o->cols = val; bit = DCM_H_COLS; break;
        case S_BITS_ALLOC: o->bits_alloc = val; bit = DCM_H_BITS_ALLOC; break;
        case S_BITS_STORED: o->bits_stored = val; bit = DCM_H_BITS_STORED; break;
        case S_HIGH_BIT: o->high_bit = val; bit = DCM_H_HIGH_BIT; break;
        default: o->pixel_rep = val; bit = DCM_H_PIXEL_REP; break;
        }
        o->has |= bit;
        return DCM_OK;
    }
    }
    return DCM_OK;
}

/* ---------------------------------------------------------------- the public functions */
int dcm_parse(const uint8_t *b, uint32_t len, dcm_info *info)
{
    uint32_t p = 132, end = len, counter = 0;
    int in_meta = 1, explicit_vr = 1, have_pix = 0, r;
    elem_t el;

    zero(info, sizeof *info);
    if (len < 132 || b[128] != 'D' || b[129] != 'I' || b[130] != 'C' || b[131] != 'M') return DCM_E_PREAMBLE;

    while (p < end) {
        const wanted_t *w = (const wanted_t *)0;
        unsigned i;
        if (++counter > DCM_MAX_ELEMENTS) return DCM_E_TOO_MANY;
        if (in_meta && (uint64_t)p + 2 <= end && rd16(b + p) != 2) {
            in_meta = 0;
            if (!(info->has & DCM_H_TS)) return DCM_E_NO_TS;
            if (str_is(&info->ts, "1.2.840.10008.1.2.1")) explicit_vr = 1;
            else if (str_is(&info->ts, "1.2.840.10008.1.2")) explicit_vr = 0;
            else return DCM_E_TS;                           /* refused (compressed, deflate, big endian) or unknown */
            info->explicit_vr = (uint8_t)explicit_vr;
        }
        r = read_element(b, p, explicit_vr || in_meta, end, &el);
        if (r) return r;
        if (el.g == 0x7FE0 && el.e == 0x0010) {
            if (el.len == 0xFFFFFFFFu) return DCM_E_ENCAPSULATED;
            if ((uint64_t)el.q + el.len > end) return DCM_E_PIXEL_TRUNC;
            info->pix_off = el.q;
            info->pix_len = el.len;
            have_pix = 1;
            break;
        }
        if (el.len == 0xFFFFFFFFu) {
            r = skip_undefined(b, el.q, explicit_vr || in_meta, end, 1, &counter, &p);
            if (r) return r;
            continue;
        }
        if ((uint64_t)el.q + el.len > end) return DCM_E_RUNS_PAST;
        for (i = 0; i < N_WANTED; i++)
            if (WANTED[i].g == el.g && WANTED[i].e == el.e) { w = &WANTED[i]; break; }
        if (w) {
            if ((el.g == 2 || explicit_vr) && el.vr0) {
                char a = 0, c = 0;
                if (w->kind == K_US) { a = 'U'; c = 'S'; }
                else if (w->kind == K_DS) { a = 'D'; c = 'S'; }
                else if (w->kind == K_IS) { a = 'I'; c = 'S'; }
                if (a && !(el.vr0 == (uint8_t)a && el.vr1 == (uint8_t)c)) return DCM_E_VR_MISMATCH;
            }
            r = store(info, w, b + el.q, el.len);
            if (r) return r;
        } else if (el.len) {
            for (i = 0; i < DCM_N_IDENTIFYING; i++)
                if (IDENT[i].g == el.g && IDENT[i].e == el.e) { info->identifying |= 1u << i; break; }
        }
        p = el.q + el.len;
    }
    if (in_meta) return DCM_E_ONLY_META;
    if (!have_pix) return DCM_E_NO_PIXEL;
    return DCM_OK;
}

int dcm_check(const dcm_info *t)
{
    int32_t ba, bs, hb;
    uint64_t need;
    if (!(t->has & DCM_H_ROWS) || !(t->has & DCM_H_COLS) || !(t->has & DCM_H_BITS_ALLOC) || !(t->has & DCM_H_PIXEL_REP))
        return DCM_E_MISSING_TAG;
    if (((t->has & DCM_H_SAMPLES) ? t->samples : 1) != 1) return DCM_E_COLOUR;
    if (!(t->has & DCM_H_PHOTOMETRIC) || !(str_is(&t->photometric, "MONOCHROME1") || str_is(&t->photometric, "MONOCHROME2")))
        return DCM_E_PHOTOMETRIC;
    if ((t->has & DCM_H_FRAMES) && t->frames != 1) return DCM_E_MULTIFRAME;
    if (t->rows < 1 || t->cols < 1 || (uint64_t)t->rows * t->cols > DCM_MAX_PIXELS) return DCM_E_SIZE;
    ba = t->bits_alloc;
    if (ba != 8 && ba != 16) return DCM_E_BITS_ALLOC;
    bs = (t->has & DCM_H_BITS_STORED) ? (int32_t)t->bits_stored : ba;
    hb = (t->has & DCM_H_HIGH_BIT) ? (int32_t)t->high_bit : bs - 1;
    if (!(1 <= bs && bs <= ba) || hb < bs - 1 || hb > ba - 1) return DCM_E_BITS_INCONSISTENT;
    if (t->pixel_rep != 0 && t->pixel_rep != 1) return DCM_E_PIXEL_REP;
    need = (uint64_t)t->rows * t->cols * (uint64_t)(ba / 8);
    if (t->pix_len < need) return DCM_E_PIXEL_SHORT;
    return DCM_OK;
}
