/* bench_linux.c - runs the benchmark logic (bench.c) over stdin/stdout as a freestanding
 * Linux process under qemu-riscv32, so it can be tested without hardware. */
#include <stdint.h>
#include "bench.h"

static long sys3(long n, long a, long b, long c)
{
    register long a7 __asm__("a7") = n;
    register long a0 __asm__("a0") = a;
    register long a1 __asm__("a1") = b;
    register long a2 __asm__("a2") = c;
    __asm__ volatile("ecall" : "+r"(a0) : "r"(a7), "r"(a1), "r"(a2) : "memory");
    return a0;
}

static long sys5(long n, long a, long b, long c, long d, long e)
{
    register long a7 __asm__("a7") = n;
    register long a0 __asm__("a0") = a;
    register long a1 __asm__("a1") = b;
    register long a2 __asm__("a2") = c;
    register long a3 __asm__("a3") = d;
    register long a4 __asm__("a4") = e;
    __asm__ volatile("ecall" : "+r"(a0) : "r"(a7), "r"(a1), "r"(a2), "r"(a3), "r"(a4) : "memory");
    return a0;
}

static int lx_read(void *ctx, uint8_t *buf, uint32_t n, int32_t timeout_ms)
{
    (void)ctx;
    if (timeout_ms >= 0) {                         /* ppoll_time64 on stdin, like the chip's timed read */
        struct { int fd; short events, revents; } p = { 0, 1 /* POLLIN */, 0 };
        int64_t ts[2] = { timeout_ms / 1000, (int64_t)(timeout_ms % 1000) * 1000000 };
        long r = sys5(414, (long)&p, 1, (long)ts, 0, 8);
        if (r == 0) return 0;                      /* timed out */
    }
    long r = sys3(63, 0, (long)buf, (long)n);
    return r > 0 ? (int)r : -1;                    /* EOF = link closed */
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

static uint64_t lx_now(void *ctx)
{
    (void)ctx;
    int64_t ts[2] = { 0, 0 };                      /* struct __kernel_timespec (64-bit fields) */
    sys3(403, 1 /* CLOCK_MONOTONIC */, (long)ts, 0);   /* clock_gettime64 */
    return (uint64_t)ts[0] * 1000000u + (uint64_t)ts[1] / 1000u;
}

void run(void)
{
    const bench_io io = { lx_read, lx_write, 0, lx_now, { 0, 0, 0 }, 0 };
    while (bench_serve_one(&io) == 0) {}
    sys3(93, 0, 0, 0);
    for (;;) {}
}

__attribute__((naked)) void _start(void) { __asm__ volatile("call run"); }
