/* geom_linux.c - runs the stack geometry (geom.c) under qemu-riscv32 for testing.
 *
 * stdin : a stream of series; each is a little-endian u32 file count N followed by N records of
 *         (u32 length, file bytes), the files in the order they are to be added. End of input ends the run.
 * stdout: per series a canonical dump ending in "END". Doubles are printed as the 16 hex digits of their
 *         IEEE bit pattern so tests/test_geom_c.py can compare them to the last bit.
 */
#include <stdint.h>
#include "dicom.h"
#include "geom.h"

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
static dcm_info info;
static geom_state gs;
static geom_result res;

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
static void put_hex(double d)
{
    union { double d; uint64_t u; } v;
    int i;
    v.d = d;
    for (i = 60; i >= 0; i -= 4) put_c("0123456789abcdef"[(v.u >> i) & 15]);
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

void run(void)
{
    uint32_t nfiles;
    while (!read_u32(&nfiles)) {
        uint32_t i;
        int err = 0, bad_stream = 0;
        geom_init(&gs);
        put_s("SERIES\n");
        for (i = 0; i < nfiles; i++) {
            uint32_t len;
            if (read_u32(&len) || len > MAX_FILE || read_full(file_buf, len)) { bad_stream = 1; break; }
            if (err) continue;                                   /* keep reading the stream, stop analysing */
            put_s("F"); put_u(i); put_s("=");
            if (dcm_parse(file_buf, len, &info)) { put_s("ERR:PARSE\n"); err = 1; continue; }
            if (dcm_check(&info)) { put_s("ERR:CHECK\n"); err = 1; continue; }
            {
                int r = geom_add(&gs, &info);
                if (r) { put_s("ERR:"); put_s(geom_err_name(r)); put_c('\n'); err = 1; continue; }
            }
            put_s("OK\n");
        }
        if (bad_stream) { put_s("HARNESS\nEND\n"); flush_out(); break; }
        if (!err) {
            int r = geom_finish(&gs, &res);
            if (r) { put_s("RESULT=ERR:"); put_s(geom_err_name(r)); put_c('\n'); }
            else {
                unsigned k;
                put_s("RESULT=OK\ncols="); put_u(res.cols);
                put_s("\nrows="); put_u(res.rows);
                put_s("\ncount="); put_u(res.count);
                put_s("\nplane="); put_u(res.plane);
                put_s("\nthickness_assumed="); put_u(res.thickness_assumed);
                put_s("\nthickness_mismatch="); put_u(res.thickness_mismatch);
                put_s("\npixdim=");
                for (k = 0; k < 3; k++) { if (k) put_c(','); put_hex(res.pixdim[k]); }
                put_s("\naffine=");
                for (k = 0; k < 12; k++) { if (k) put_c(','); put_hex(res.affine[k]); }
                put_c('\n');
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
