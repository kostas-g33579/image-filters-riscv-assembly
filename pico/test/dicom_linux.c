/* dicom_linux.c - runs the allow-list DICOM reader (dicom.c) under qemu-riscv32 for testing.
 *
 * stdin : a stream of records, each a little-endian u32 length followed by that many bytes (one
 *         DICOM file). A length with bit 31 set is a PARTIAL record: the low 31 bits are the length of the
 *         whole file, then comes a u32 `avail` and only that many bytes (the start of the file); it is read
 *         with dcm_parse_head(). End of input ends the run.
 * stdout: for every record a canonical text dump, ending with a line "END". The dump is the
 *         contract tests/test_dicom_c.py compares with the Python reader. Strings are escaped
 *         (\xHH for bytes outside 0x20..0x7e and for backslash) so a value never breaks a line.
 */
#include <stdint.h>
#include "dicom.h"

static long sys3(long n, long a, long b, long c)
{
    register long a7 __asm__("a7") = n;
    register long a0 __asm__("a0") = a;
    register long a1 __asm__("a1") = b;
    register long a2 __asm__("a2") = c;
    __asm__ volatile("ecall" : "+r"(a0) : "r"(a7), "r"(a1), "r"(a2) : "memory");
    return a0;
}

/* gcc may turn loops into calls to these even when freestanding */
__attribute__((optimize("no-tree-loop-distribute-patterns"))) void *memset(void *d, int c, unsigned long n)
{ unsigned char *p = d; while (n--) *p++ = (unsigned char)c; return d; }
__attribute__((optimize("no-tree-loop-distribute-patterns"))) void *memcpy(void *d, const void *s, unsigned long n)
{ unsigned char *p = d; const unsigned char *q = s; while (n--) *p++ = *q++; return d; }

#define MAX_FILE (20u << 20)       /* the element-count test is a 16 MB file */
static uint8_t file_buf[MAX_FILE];
static dcm_info info;

static char out[16384];
static unsigned out_n;

static void flush_out(void)
{
    unsigned off = 0;
    while (off < out_n) {
        long r = sys3(64, 1, (long)(out + off), (long)(out_n - off));
        if (r <= 0) break;
        off += (unsigned)r;
    }
    out_n = 0;
}
static void put_c(char c) { if (out_n == sizeof out) flush_out(); out[out_n++] = c; }
static void put_s(const char *s) { while (*s) put_c(*s++); }
static void put_u(uint64_t v)
{
    char t[24];
    int n = 0;
    if (!v) { put_c('0'); return; }
    while (v) { t[n++] = (char)('0' + v % 10); v /= 10; }
    while (n) put_c(t[--n]);
}
static void put_i(int64_t v) { if (v < 0) { put_c('-'); put_u((uint64_t)(-(v + 1)) + 1u); } else put_u((uint64_t)v); }

static void put_esc(const dcm_str *s)
{
    static const char HEX[] = "0123456789abcdef";
    unsigned i;
    if (s->over) { put_s("~LONG"); return; }
    for (i = 0; i < s->len; i++) {
        unsigned char c = (unsigned char)s->s[i];
        if (c >= 0x20 && c <= 0x7e && c != '\\') put_c((char)c);
        else { put_s("\\x"); put_c(HEX[c >> 4]); put_c(HEX[c & 15]); }
    }
}
static void line_str(const char *name, const dcm_str *s) { put_s("str:"); put_s(name); put_c('='); put_esc(s); put_c('\n'); }
static void line_int(const char *name, int64_t v) { put_s("int:"); put_s(name); put_c('='); put_i(v); put_c('\n'); }
static void line_dec(const char *name, const dcm_declist *d)
{
    unsigned i;
    if (!d->n) return;
    put_s("dec:"); put_s(name); put_c('='); put_u(d->n); put_c(':');
    for (i = 0; i < d->n && i < DCM_DEC_MAX; i++) {
        if (i) put_c(',');
        put_i(d->v[i].m); put_c('@'); put_i(d->v[i].e);
    }
    put_c('\n');
}

static void dump(const dcm_info *t, int chk)
{
    unsigned i, first = 1;
    put_s("CHECK="); if (chk) { put_s("ERR:"); put_s(dcm_err_name(chk)); } else put_s("OK"); put_c('\n');
    put_s("explicit="); put_u(t->explicit_vr); put_c('\n');
    put_s("pix_off="); put_u(t->pix_off); put_c('\n');
    put_s("pix_len="); put_u(t->pix_len); put_c('\n');
    put_s("identifying=");
    for (i = 0; i < DCM_N_IDENTIFYING; i++)
        if (t->identifying & (1u << i)) { if (!first) put_c(','); put_s(dcm_identifying_name(i)); first = 0; }
    put_c('\n');
    if (t->has & DCM_H_TS) line_str("TransferSyntax", &t->ts);
    if (t->has & DCM_H_IMAGE_TYPE) line_str("ImageType", &t->image_type);
    if (t->has & DCM_H_SOP) line_str("SOPClass", &t->sop_class);
    if (t->has & DCM_H_MODALITY) line_str("Modality", &t->modality);
    if (t->has & DCM_H_MANUFACTURER) line_str("Manufacturer", &t->manufacturer);
    if (t->has & DCM_H_MODEL) line_str("ManufacturerModelName", &t->model);
    if (t->has & DCM_H_ID_REMOVED) line_str("PatientIdentityRemoved", &t->identity_removed);
    if (t->has & DCM_H_SCAN_SEQ) line_str("ScanningSequence", &t->scan_seq);
    if (t->has & DCM_H_SEQ_VAR) line_str("SequenceVariant", &t->seq_var);
    if (t->has & DCM_H_ACQ_TYPE) line_str("MRAcquisitionType", &t->acq_type);
    if (t->has & DCM_H_SERIES_UID) line_str("SeriesInstanceUID", &t->series_uid);
    if (t->has & DCM_H_PHOTOMETRIC) line_str("Photometric", &t->photometric);
    if (t->has & DCM_H_SOFTWARE) line_str("SoftwareVersions", &t->software);
    if (t->has & DCM_H_PROTOCOL) line_str("ProtocolName", &t->protocol);
    if (t->has & DCM_H_BURNED_IN) line_str("BurnedInAnnotation", &t->burned_in);
    if (t->has & DCM_H_CONTRAST) line_int("ContrastDeclared", t->contrast_declared);
    if (t->has & DCM_H_SAMPLES) line_int("SamplesPerPixel", t->samples);
    if (t->has & DCM_H_PLANAR) line_int("PlanarConfiguration", t->planar);
    if (t->has & DCM_H_ROWS) line_int("Rows", t->rows);
    if (t->has & DCM_H_COLS) line_int("Columns", t->cols);
    if (t->has & DCM_H_BITS_ALLOC) line_int("BitsAllocated", t->bits_alloc);
    if (t->has & DCM_H_BITS_STORED) line_int("BitsStored", t->bits_stored);
    if (t->has & DCM_H_HIGH_BIT) line_int("HighBit", t->high_bit);
    if (t->has & DCM_H_PIXEL_REP) line_int("PixelRepresentation", t->pixel_rep);
    if (t->has & DCM_H_FRAMES) line_int("NumberOfFrames", t->frames);
    if (t->has & DCM_H_INSTANCE) line_int("InstanceNumber", t->instance_number);
    line_dec("SliceThickness", &t->slice_thickness);
    line_dec("PixelSpacing", &t->pixel_spacing);
    line_dec("ImagePositionPatient", &t->ipp);
    line_dec("ImageOrientationPatient", &t->iop);
    line_dec("RepetitionTime", &t->tr);
    line_dec("EchoTime", &t->te);
    line_dec("InversionTime", &t->ti);
    line_dec("FlipAngle", &t->flip);
    line_dec("MagneticFieldStrength", &t->field_strength);
    line_dec("RescaleSlope", &t->rescale_slope);
    line_dec("RescaleIntercept", &t->rescale_intercept);
    line_dec("WindowCenter", &t->window_center);
    line_dec("WindowWidth", &t->window_width);
}

static int read_full(uint8_t *dst, uint32_t n)
{
    uint32_t got = 0;
    while (got < n) {
        long r = sys3(63, 0, (long)(dst + got), (long)(n - got));
        if (r <= 0) return -1;
        got += (uint32_t)r;
    }
    return 0;
}

void run(void)
{
    for (;;) {
        uint8_t hdr[4];
        uint32_t len, avail;
        int r, partial;
        if (read_full(hdr, 4)) break;
        len = (uint32_t)hdr[0] | ((uint32_t)hdr[1] << 8) | ((uint32_t)hdr[2] << 16) | ((uint32_t)hdr[3] << 24);
        partial = (int)(len >> 31);
        len &= 0x7FFFFFFFu;
        if (partial) {
            uint8_t ah[4];
            if (read_full(ah, 4)) break;
            avail = (uint32_t)ah[0] | ((uint32_t)ah[1] << 8) | ((uint32_t)ah[2] << 16) | ((uint32_t)ah[3] << 24);
            if (avail > len) { put_s("STATUS=HARNESS\nEND\n"); break; }
        } else avail = len;
        if (avail > MAX_FILE || read_full(file_buf, avail)) { put_s("STATUS=HARNESS\nEND\n"); break; }
        r = dcm_parse_head(file_buf, avail, len, &info);
        put_s("STATUS="); if (r) { put_s("ERR:"); put_s(dcm_err_name(r)); } else put_s("OK"); put_c('\n');
        if (!r) dump(&info, dcm_check(&info));
        put_s("END\n");
        flush_out();
    }
    flush_out();
    sys3(93, 0, 0, 0);
    for (;;) {}
}

__attribute__((naked)) void _start(void) { __asm__ volatile("call run"); }
