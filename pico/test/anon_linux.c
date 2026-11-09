/* anon_linux.c - runs the anonymiser protocol (anon.c) under qemu-riscv32: requests on stdin, replies on stdout.
 * Built twice by the Makefile: with the real sizes, and with a tiny head buffer and chunk (-DANON_HDR_MAX=...
 * -DANON_CHUNK=...) and -DSLOW_READS, which hands the protocol 1 to 13 bytes at a time, so that cuts inside a
 * sample, partial reads and many chunk boundaries are exercised even on small files. */
#include <stdint.h>
#include "anon.h"

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

static int lx_read(void *ctx, uint8_t *buf, uint32_t n, int32_t timeout_ms)
{
    long r;
    (void)ctx;
#ifdef SLOW_READS
    static uint32_t k;
    uint32_t cap = 1 + (k++ * 5) % 13;
    if (n > cap) n = cap;
#endif
    r = sys3(63, 0, (long)buf, (long)n);
    if (r > 0) return (int)r;
    return timeout_ms < 0 ? -1 : 0;         /* EOF while idle = the link closed; EOF mid-message = the host stopped */
}

static int lx_write(void *ctx, const uint8_t *buf, uint32_t n)
{
    (void)ctx;
    while (n) {
        long r = sys3(64, 1, (long)buf, (long)n);
        if (r <= 0) return -1;
        buf += r; n -= (uint32_t)r;
    }
    return 0;
}

static uint32_t zero(void) { return 0; }
static anon_work work;

void run(void)
{
    const proto_io io = { lx_read, lx_write, zero, zero, 0 };
    while (anon_serve_one(&io, &work) == 0) {}
    sys3(93, 0, 0, 0);
    for (;;) {}
}

__attribute__((naked)) void _start(void) { __asm__ volatile("call run"); }
