/* sidecar_linux.c - runs the JSON sidecar generator (sidecar.c) under qemu-riscv32 for testing.
 *
 * stdin : a stream of series; each is four labels (subject, sequence, site, scanner) as one length byte then
 *         that many bytes each (length 0 = not given), then a little-endian u32 file count N, then N records of
 *         (u32 length, file bytes), in the order the slices are to be added. End of input ends the run.
 * stdout: per series: "LABELS=ERR:<n>" for an invalid label (nothing else is analysed); "F<i>=OK|ERR:<class>"
 *         for every file (stop at the first error); then "JSON_BEGIN", the JSON exactly as generated,
 *         "JSON_END" -- or "RESULT=ERR:<what>"; then "END".
 */
#include <stdint.h>
#include "dicom.h"
#include "geom.h"
#include "nifti.h"
#include "sidecar.h"

static long sys3(long n, long a, long b, long c)
{
    register long a7 __asm__("a7") = n;
    register long a0 __asm__("a0") = a;
    register long a1 __asm__("a1") = b;
    register long a2 __asm__("a2") = c;
    __asm__ volatile("ecall" : "+r"(a0) : "r"(a7), "r"(a1), "r"(a2) : "memory");
    return a0;
}

__attribute__((optimize("no-tree-loop-distribute-patterns"))) void *memset(void *d, int c, unsigned long n)
{ unsigned char *p = d; while (n--) *p++ = (unsigned char)c; return d; }
__attribute__((optimize("no-tree-loop-distribute-patterns"))) void *memcpy(void *d, const void *s, unsigned long n)
{ unsigned char *p = d; const unsigned char *q = s; while (n--) *p++ = *q++; return d; }

#define MAX_FILE (4u << 20)
static uint8_t file_buf[MAX_FILE];
static char json[SC_MAX_JSON];
static dcm_info info;
static geom_state gs;
static geom_result res;
static nii_state ns;
static sc_state ss;
static sc_labels lab;

static char out[8192];
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
static void put_u(uint32_t v)
{
    char t[12];
    int n = 0;
    if (!v) { put_c('0'); return; }
    while (v) { t[n++] = (char)('0' + v % 10); v /= 10; }
    while (n) put_c(t[--n]);
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
static int read_u32(uint32_t *v)
{
    uint8_t h[4];
    if (read_full(h, 4)) return -1;
    *v = (uint32_t)h[0] | ((uint32_t)h[1] << 8) | ((uint32_t)h[2] << 16) | ((uint32_t)h[3] << 24);
    return 0;
}
/* A label: one length byte, then the bytes. Length 0 means "not given". Anything else goes to sc_valid_label at its
 * FULL length (up to 255), so the length limit and the character set are the function's, not the harness's; a NUL
 * inside would cut the string short for it, so that is refused here. A valid label is copied into dst. */
static int read_label(char *dst, int *valid)
{
    uint8_t n;
    char tmp[256];
    uint32_t i;
    if (read_full(&n, 1) || (n && read_full((uint8_t *)tmp, n))) return -1;
    tmp[n] = 0;
    dst[0] = 0;
    if (n == 0) { *valid = 1; return 0; }
    for (i = 0; i < n; i++)
        if (tmp[i] == 0) { *valid = 0; return 0; }
    *valid = sc_valid_label(tmp);
    if (*valid)
        for (i = 0; i <= n; i++) dst[i] = tmp[i];               /* a valid label has at most SC_LABEL_MAX characters */
    return 0;
}

void run(void)
{
    for (;;) {
        uint32_t nfiles, i;
        int err = 0, bad_stream = 0, v[4], lab_err = 0;
        if (read_label(lab.subject, &v[0]) || read_label(lab.sequence, &v[1]) || read_label(lab.site, &v[2]) ||
            read_label(lab.scanner, &v[3]) || read_u32(&nfiles))
            break;
        put_s("SERIES\n");
        put_s("EMPTY_LABEL_VALID="); put_u((uint32_t)sc_valid_label("")); put_c('\n');
        for (i = 0; i < 4; i++)
            if (!v[i] && !lab_err) { put_s("LABELS=ERR:"); put_u(i); put_c('\n'); lab_err = 1; }
        err = lab_err;
        geom_init(&gs);
        nii_init(&ns);
        sc_init(&ss);
        for (i = 0; i < nfiles; i++) {
            uint32_t len;
            int r;
            if (read_u32(&len) || len > MAX_FILE || read_full(file_buf, len)) { bad_stream = 1; break; }
            if (err) continue;
            put_s("F"); put_u(i); put_s("=");
            if (dcm_parse(file_buf, len, &info)) { put_s("ERR:PARSE\n"); err = 1; continue; }
            if (dcm_check(&info)) { put_s("ERR:CHECK\n"); err = 1; continue; }
            r = geom_add(&gs, &info);
            if (r) { put_s("ERR:"); put_s(geom_err_name(r)); put_c('\n'); err = 1; continue; }
            r = nii_add(&ns, &info);
            if (r) { put_s("ERR:"); put_s(nii_err_name(r)); put_c('\n'); err = 1; continue; }
            sc_add(&ss, &info);
            put_s("OK\n");
        }
        if (bad_stream) { put_s("HARNESS\nEND\n"); flush_out(); break; }
        if (!err) {
            int r = geom_finish(&gs, &res);
            if (r) { put_s("RESULT=ERR:"); put_s(geom_err_name(r)); put_c('\n'); }
            else {
                int n = sc_build(&ss, &lab, &ns, &res, json, sizeof json), k;
                if (n < 0) put_s("RESULT=ERR:JSON_TOO_LONG\n");
                else {
                    put_s("JSON_BEGIN\n");
                    for (k = 0; k < n; k++) put_c(json[k]);
                    put_s("JSON_END\n");
                }
            }
        }
        put_s("END\n");
        flush_out();
    }
    flush_out();
    sys3(93, 0, 0, 0);
    for (;;) {}
}

__attribute__((naked)) void _start(void) { __asm__ volatile("call run"); }
