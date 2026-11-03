/* main.c - the Pico 2 / Pico 2 W firmware: serve filter requests over USB
 * serial, running the assembly kernel (src/kernel.s) on the RISC-V core. */
#include <stdint.h>
#include "pico/stdlib.h"
#include "proto.h"

static int usb_read(void *ctx, uint8_t *buf, uint32_t n, int32_t timeout_ms)
{
    (void)ctx;
    absolute_time_t until = timeout_ms < 0 ? at_the_end_of_time
                                           : make_timeout_time_ms((uint32_t)timeout_ms);
    int r = stdio_get_until((char *)buf, (int)n, until);
    return r == PICO_ERROR_TIMEOUT ? 0 : r;
}

static int usb_write(void *ctx, const uint8_t *buf, uint32_t n)
{
    (void)ctx;
    stdio_put_string((const char *)buf, (int)n, false, false);  /* binary: no CR/LF translation */
    stdio_flush();
    return 0;
}

static uint32_t read_mcycle(void)
{
    uint32_t v;
    __asm__ volatile("csrr %0, mcycle" : "=r"(v));
    return v;
}

static uint32_t read_minstret(void)
{
    uint32_t v;
    __asm__ volatile("csrr %0, minstret" : "=r"(v));
    return v;
}

int main(void)
{
    /* Hazard3 holds mcycle/minstret stopped after reset (mcountinhibit = 0b101,
     * "to save power"); clear it so the counters run. */
    __asm__ volatile("csrw 0x320, zero");

    stdio_init_all();

    const proto_io io = { usb_read, usb_write, read_mcycle, read_minstret, 0 };
    for (;;) proto_serve_one(&io);
}
