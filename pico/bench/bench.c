/* bench.c - the device side of the USB throughput benchmark (see bench.h). */
#include "bench.h"

#define CMD_MAGIC  0x48434E42u   /* "BNCH" */
#define DONE_MAGIC 0x454E4F44u   /* "DONE" */

/* One 64 KB buffer holding the 256-byte pattern repeated. A transfer that starts at
 * offset `off` of the stream is just buf + (off % 256), so no per-chunk fill is needed
 * (filling would put CPU work between writes and distort the measurement). */
static uint8_t buf[BENCH_MAX_CHUNK + 256] __attribute__((aligned(4)));
/* Received data goes to its OWN buffer: receiving into `buf` would overwrite the pattern
 * that a later SOURCE test sends (found on the chip: 64 bad bytes per write). */
static uint8_t rx[BENCH_MAX_CHUNK] __attribute__((aligned(4)));
static int buf_ready;

static void prepare(void)
{
    if (buf_ready) return;
    for (uint32_t i = 0; i < sizeof buf; i++) buf[i] = (uint8_t)(i * 7u);
    buf_ready = 1;
}

static int send_done(const bench_io *io, uint32_t status, uint32_t bytes, uint32_t us, uint32_t errors)
{
    uint32_t r[8] = { DONE_MAGIC, status, bytes, us, errors, io->info[0], io->info[1], io->info[2] };
    return io->write(io->ctx, (const uint8_t *)r, sizeof r);
}

static int read_exact(const bench_io *io, uint8_t *dst, uint32_t n, int32_t timeout_ms)
{
    uint32_t got = 0;
    while (got < n) {
        int r = io->read(io->ctx, dst + got, n - got, timeout_ms);
        if (r < 0) return -1;
        if (r == 0) return 0;
        got += (uint32_t)r;
    }
    return 1;
}

int bench_serve_one(const bench_io *io)
{
    prepare();

    uint32_t win = 0;                                   /* find the magic, skipping noise */
    for (;;) {
        uint8_t b;
        int r = io->read(io->ctx, &b, 1, -1);
        if (r < 0) return -1;
        if (r == 0) continue;
        win = (win >> 8) | ((uint32_t)b << 24);
        if (win == CMD_MAGIC) break;
    }
    uint32_t cmd[4];                                    /* mode, total, chunk, flags */
    int rc = read_exact(io, (uint8_t *)cmd, sizeof cmd, 1000);
    if (rc < 0) return -1;
    if (rc == 0) { send_done(io, BS_TIMEOUT, 0, 0, 0); return 0; }
    uint32_t mode = cmd[0], total = cmd[1], chunk = cmd[2], flush_each = cmd[3] & 1u;
    if (mode < BENCH_SINK || mode > BENCH_ECHO || total == 0 || total > BENCH_MAX_TOTAL ||
        chunk == 0 || chunk > BENCH_MAX_CHUNK) {
        return send_done(io, BS_BAD_ARGS, 0, 0, 0) < 0 ? -1 : 0;
    }

    uint32_t done = 0, errors = 0, status = BS_OK;
    uint64_t t_first = 0, t_last = 0;

    if (mode == BENCH_SOURCE) {
        t_first = io->now_us(io->ctx);
        while (done < total) {
            uint32_t n = total - done < chunk ? total - done : chunk;
            if (io->write(io->ctx, buf + (done & 255u), n) < 0) return -1;
            if (flush_each && io->flush) io->flush(io->ctx);
            done += n;
        }
        if (io->flush) io->flush(io->ctx);
        t_last = io->now_us(io->ctx);
    } else {
        while (done < total) {
            uint32_t n = total - done < chunk ? total - done : chunk;
            int r = io->read(io->ctx, rx, n, 3000);
            if (r < 0) return -1;
            if (r == 0) { status = BS_TIMEOUT; break; }
            uint64_t now = io->now_us(io->ctx);
            if (!done) t_first = now;
            t_last = now;
            for (int i = 0; i < r; i++)
                if (rx[i] != (uint8_t)((done + (uint32_t)i) * 7u)) errors++;
            if (mode == BENCH_ECHO) {
                if (io->write(io->ctx, rx, (uint32_t)r) < 0) return -1;
                if (flush_each && io->flush) io->flush(io->ctx);
            }
            done += (uint32_t)r;
        }
        if (mode == BENCH_ECHO && io->flush) io->flush(io->ctx);
    }
    return send_done(io, status, done, (uint32_t)(t_last - t_first), errors) < 0 ? -1 : 0;
}
