/* bench.h - a USB throughput benchmark, independent of any hardware.
 *
 * The host sends a 20-byte command, the device runs one test and answers with a 32-byte
 * "DONE" message. All integers are little-endian 32-bit.
 *
 *   command: "BNCH", mode, total_bytes, chunk_bytes, flags
 *   mode 1 SINK    host -> device: the host sends total bytes; the device reads and checks them
 *   mode 2 SOURCE  device -> host: the device sends total bytes in writes of chunk bytes
 *   mode 3 ECHO    both ways at once: the device writes back every chunk it reads
 *   flags bit 0:   flush after every write (what the filter firmware does)
 *   DONE:    "DONE", status, bytes, elapsed_us, errors, ep_bufsize, rx_fifo, tx_fifo
 *
 * The data is a repeating pattern, byte i = (i * 7) mod 256, so a dropped, repeated or
 * reordered 64-byte packet is detected. elapsed_us is measured by the DEVICE's clock from
 * the first byte moved to the last, so it can be compared with the host's own timing.
 *
 * No SDK and no libc: runs on the Pico and under qemu-riscv32 (pico/test/bench_linux.c).
 */
#ifndef BENCH_H
#define BENCH_H
#include <stdint.h>

#define BENCH_MAX_CHUNK 65536u
#define BENCH_MAX_TOTAL (1u << 30)

enum { BENCH_SINK = 1, BENCH_SOURCE = 2, BENCH_ECHO = 3 };
enum { BS_OK = 0, BS_BAD_ARGS = 1, BS_TIMEOUT = 8 };

typedef struct bench_io {
    int (*read)(void *ctx, uint8_t *buf, uint32_t n, int32_t timeout_ms);   /* >=1, 0 timeout, <0 closed */
    int (*write)(void *ctx, const uint8_t *buf, uint32_t n);                /* 0 ok, <0 failure */
    void (*flush)(void *ctx);                                               /* may be NULL */
    uint64_t (*now_us)(void *ctx);
    uint32_t info[3];                                                       /* reported back in DONE */
    void *ctx;
} bench_io;

/* Wait for one command and run it. 0 = handled (even if answered with an error), <0 = link closed. */
int bench_serve_one(const bench_io *io);

#endif
