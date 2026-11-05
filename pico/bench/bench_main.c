/* bench_main.c - USB throughput benchmark firmware for the Pico 2 / 2 W.
 *
 * One source, three builds (see CMakeLists.txt):
 *   usb_bench_stdio : the path the filter firmware uses today (stdio_usb, default FIFOs)
 *   usb_bench_fifo  : the same path, TinyUSB CDC FIFOs enlarged (BENCH_FIFO bytes)
 *   usb_bench_raw   : enlarged FIFOs and direct tud_cdc_read / tud_cdc_write
 *                     (BENCH_RAW), skipping the stdio layer
 * The measuring logic lives in bench.c and is shared with the Linux test. */
#include <stdint.h>
#include "pico/stdlib.h"
#include "bench.h"
#ifdef BENCH_RAW
#include "tusb.h"
#include "hardware/sync.h"
/* TinyUSB is not safe to call from two contexts at once. stdio_usb runs tud_task() from a
 * background interrupt and protects its own calls with a mutex; calling tud_cdc_* directly
 * from main would race with it (tud_cdc_read even re-arms the endpoint), so each direct call
 * runs with interrupts off. They are short; the pending USB work runs right after. */
#define ATOMIC(stmt) do { uint32_t irq_state_ = save_and_disable_interrupts(); stmt; restore_interrupts(irq_state_); } while (0)
#endif

static int io_read(void *ctx, uint8_t *buf, uint32_t n, int32_t timeout_ms)
{
    (void)ctx;
    absolute_time_t until = timeout_ms < 0 ? at_the_end_of_time
                                           : make_timeout_time_ms((uint32_t)timeout_ms);
#ifdef BENCH_RAW
    /* return whatever is there (at least 1 byte), like a read(2) */
    for (;;) {
        int got = 0;
        ATOMIC(if (tud_cdc_available()) got = (int)tud_cdc_read(buf, n));
        if (got) return got;
        if (time_reached(until)) return 0;
        tight_loop_contents();
    }
#else
    /* exactly what the filter firmware (main.c) does: stdio_get_until returns as soon as it
     * has ANY data (at most what the FIFO holds), like a read(2) */
    int r = stdio_get_until((char *)buf, (int)n, until);
    return r == PICO_ERROR_TIMEOUT ? 0 : r;
#endif
}

static int io_write(void *ctx, const uint8_t *buf, uint32_t n)
{
    (void)ctx;
#ifdef BENCH_RAW
    while (n) {
        uint32_t k = 0;
        ATOMIC(k = tud_cdc_write(buf, n));
        buf += k; n -= k;
        if (!k) { ATOMIC(tud_cdc_write_flush()); tight_loop_contents(); }
    }
#else
    stdio_put_string((const char *)buf, (int)n, false, false);
#endif
    return 0;
}

static void io_flush(void *ctx)
{
    (void)ctx;
#ifdef BENCH_RAW
    ATOMIC(tud_cdc_write_flush());
#else
    stdio_flush();
#endif
}

static uint64_t io_now(void *ctx) { (void)ctx; return time_us_64(); }

int main(void)
{
    stdio_init_all();
#ifdef BENCH_RAW
    const uint32_t mode = 2;
#else
    const uint32_t mode = 1;
#endif
#ifdef BENCH_FIFO
    const uint32_t fifo = BENCH_FIFO;
#else
    const uint32_t fifo = 64;
#endif
    /* DONE reports: variant (1 stdio, 2 raw), rx fifo, tx fifo */
    const bench_io io = { io_read, io_write, io_flush, io_now, { mode, fifo, fifo }, 0 };
    for (;;) bench_serve_one(&io);
}
