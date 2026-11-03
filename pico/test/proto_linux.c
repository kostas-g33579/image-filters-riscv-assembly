/* proto_linux.c - runs the device protocol (proto.c) + the assembly kernel as
 * a Linux process under qemu-riscv32, reading requests on stdin and writing
 * replies on stdout. This is how the protocol is tested without hardware. */
#include <stdint.h>
#include "proto.h"

static long sys3(long n, long a, long b, long c)
{
    register long a7 __asm__("a7") = n;
    register long a0 __asm__("a0") = a;
    register long a1 __asm__("a1") = b;
    register long a2 __asm__("a2") = c;
    __asm__ volatile("ecall" : "+r"(a0) : "r"(a7), "r"(a1), "r"(a2) : "memory");
    return a0;
}

static int lx_read(void *ctx, uint8_t *buf, uint32_t n, int32_t timeout_ms)
{
    (void)ctx;
    long r = sys3(63, 0, (long)buf, (long)n);            /* read(0, ...) */
    if (r > 0) return (int)r;
    /* EOF: an idle wait (forever) means the link closed; a bounded wait means
     * the host stopped mid-message, which the device reports as a timeout. */
    return timeout_ms < 0 ? -1 : 0;
}

static int lx_write(void *ctx, const uint8_t *buf, uint32_t n)
{
    (void)ctx;
    while (n) {
        long r = sys3(64, 1, (long)buf, (long)n);        /* write(1, ...) */
        if (r <= 0) return -1;
        buf += r; n -= (uint32_t)r;
    }
    return 0;
}

static uint32_t zero(void) { return 0; }

void run(void)
{
    const proto_io io = { lx_read, lx_write, zero, zero, 0 };
    while (proto_serve_one(&io) == 0) {}
    sys3(93, 0, 0, 0);                                   /* exit(0) */
    for (;;) {}
}

__attribute__((naked)) void _start(void)
{
    __asm__ volatile("call run");
}
