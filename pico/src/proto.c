/* proto.c - request handling around the assembly filter core (src/kernel.s). */
#include "proto.h"
#include "anon.h"
#include "stream.h"

_Static_assert(sizeof(filter_rec) == 64, "filter record layout out of sync with records.inc");
_Static_assert(sizeof(void *) == 4, "this protocol layer targets RV32");
_Static_assert(2u * PROTO_MAX_PIXELS * 2u <= PROTO_ARENA_BYTES, "the arena must hold two whole-image buffers");

extern const filter_rec filter_table[];   /* kernel.s; ends with a null name */
extern void *filter_core(const uint16_t *src, uint16_t *scratch, uint32_t w,
                         uint32_t h, const filter_rec *rec, uint32_t passes,
                         uint32_t maxval);
extern void window_level(const uint16_t *src, uint8_t *dst, uint32_t n,
                         uint32_t center, uint32_t width);

#define REQ_MAGIC  0x31474D49u   /* "IMG1" as a little-endian word */
#define STR_MAGIC  0x31525453u   /* "STR1" */
#define RESP_MAGIC 0x3154554Fu   /* "OUT1" */
#define ROW_MAGIC  0x31574F52u   /* "ROW1": one output row of a stream */
#define REQ_WORDS  12            /* 48-byte request header */
#define RESP_WORDS 10            /* 40-byte response header */
#define PASSES_DEFAULT 0xFFFFFFFFu
#define MAX_PASSES 255u

/* One arena, two uses: IMG1 carves it into buf_a / buf_b, STR1 into the pipeline. */
/* 8-byte aligned: the anonymiser's work area (anon.h) holds 64-bit integers and doubles. The filters and the
 * anonymiser are served one request at a time, so they share this memory. */
static uint32_t arena[PROTO_ARENA_BYTES / 4] __attribute__((aligned(16)));
_Static_assert(sizeof(anon_work) <= PROTO_ARENA_BYTES, "the anonymiser's work area must fit the arena");
static stream_t g_stream;

static int read_exact(const proto_io *io, uint8_t *dst, uint32_t n, int32_t timeout_ms)
{
    uint32_t got = 0;
    while (got < n) {
        int r = io->read(io->ctx, dst + got, n - got, timeout_ms);
        if (r < 0) return -1;      /* link closed */
        if (r == 0) return 0;      /* timed out */
        got += (uint32_t)r;
    }
    return 1;
}

static int send_resp(const proto_io *io, uint32_t status, uint32_t w, uint32_t h,
                     uint32_t bps, uint32_t maxval, uint32_t cycles,
                     uint32_t instret, uint32_t payload, uint32_t reserved)
{
    uint32_t r[RESP_WORDS] = { RESP_MAGIC, status, w, h, bps, maxval, cycles,
                               instret, payload, reserved };
    return io->write(io->ctx, (const uint8_t *)r, sizeof r);
}

static int name_eq(const char *a, const char *b)
{
    while (*a && *a == *b) { a++; b++; }
    return *a == *b;
}

/* ------------------------------------------------------------------ STR1 */

typedef struct {
    const proto_io *io;
    uint32_t w, center, width;
    int window_on, failed;
    uint8_t *win_row;
} sink_ctx;

/* Called by the pipeline for every finished row: send "ROW1", the row index, then the row. */
static void row_sink(void *vctx, uint32_t row_index, const uint16_t *row)
{
    sink_ctx *c = (sink_ctx *)vctx;
    if (c->failed) return;
    uint32_t hdr[2] = { ROW_MAGIC, row_index };
    const uint8_t *payload = (const uint8_t *)row;
    uint32_t n = c->w * 2;
    if (c->window_on) {                                 /* display values: one byte per sample */
        window_level(row, c->win_row, c->w, c->center, c->width);
        payload = c->win_row;
        n = c->w;
    }
    if (c->io->write(c->io->ctx, (const uint8_t *)hdr, sizeof hdr) < 0 ||
        c->io->write(c->io->ctx, payload, n) < 0)
        c->failed = 1;
}

/* After an error the host may still be sending rows: read and discard them so the next
 * request starts on a message boundary. Gives up if the host goes quiet. */
static int drain(const proto_io *io, uint32_t bytes)
{
    uint8_t *scratch = (uint8_t *)arena;
    while (bytes) {
        uint32_t n = bytes < 4096 ? bytes : 4096;
        int rc = read_exact(io, scratch, n, 3000);
        if (rc <= 0) return rc;
        bytes -= n;
    }
    return 1;
}

static int serve_stream(const proto_io *io, const filter_rec *rec, uint32_t w, uint32_t h,
                        uint32_t maxval, uint32_t passes, int window_on, uint32_t center, uint32_t width)
{
    uint32_t out_bps = window_on ? 1u : 2u, out_maxval = window_on ? 255u : maxval;
    uint32_t need = stream_memory_needed(w, passes);
    if (!need || (uint64_t)need + (window_on ? w : 0) > PROTO_ARENA_BYTES) {
        send_resp(io, PS_NO_MEMORY, w, h, 0, maxval, 0, 0, 0, PROTO_ARENA_BYTES);
        return 0;
    }
    sink_ctx sc = { io, w, center, width, window_on, 0, (uint8_t *)arena + need };
    if (stream_begin(&g_stream, arena, PROTO_ARENA_BYTES, w, h, maxval, rec, passes, row_sink, &sc,
                     io->cycles, io->instret) < 0) {
        send_resp(io, PS_NO_MEMORY, w, h, 0, maxval, 0, 0, 0, PROTO_ARENA_BYTES);
        return 0;
    }
    /* Acknowledge: payload = bytes per output row, reserved = the arena size. */
    if (send_resp(io, PS_OK, w, h, out_bps, out_maxval, 0, 0, w * out_bps, PROTO_ARENA_BYTES) < 0) return -1;

    for (uint32_t r = 0; r < h; r++) {
        uint16_t *slot = stream_input_slot(&g_stream);
        int rc = read_exact(io, (uint8_t *)slot, w * 2, 3000);
        if (rc < 0) return -1;
        if (rc == 0) { send_resp(io, PS_TIMEOUT, w, h, 0, maxval, 0, 0, 0, 0); return 0; }
        for (uint32_t i = 0; i < w; i++)
            if (slot[i] > maxval) {
                send_resp(io, PS_BAD_SAMPLE, w, h, 0, maxval, 0, 0, 0, 0);
                return drain(io, (h - r - 1) * w * 2) < 0 ? -1 : 0;
            }
        stream_commit(&g_stream);
        if (sc.failed) return -1;
    }
    return send_resp(io, PS_OK, w, h, out_bps, out_maxval, stream_cycles(&g_stream), stream_instret(&g_stream),
                     h * w * out_bps, 0) < 0 ? -1 : 0;
}

/* ------------------------------------------------------------------ requests */

int proto_serve_one(const proto_io *io)
{
    uint32_t hdr[REQ_WORDS];

    /* 1. Find a magic number, skipping any noise, however long. */
    uint32_t win = 0;
    for (;;) {
        uint8_t b;
        int r = io->read(io->ctx, &b, 1, -1);
        if (r < 0) return -1;
        if (r == 0) continue;
        win = (win >> 8) | ((uint32_t)b << 24);
        if (win == REQ_MAGIC || win == STR_MAGIC || win == ANON_MAGIC) break;
    }
    if (win == ANON_MAGIC) return anon_serve_after_magic(io, (anon_work *)arena);
    hdr[0] = win;
    const int streaming = (win == STR_MAGIC);

    /* 2. The rest of the header. */
    int rc = read_exact(io, (uint8_t *)&hdr[1], sizeof hdr - 4, 1000);
    if (rc < 0) return -1;
    if (rc == 0) { send_resp(io, PS_TIMEOUT, 0, 0, 0, 0, 0, 0, 0, 0); return 0; }

    /* 3. Validate everything the header says before accepting any samples. */
    ((char *)&hdr[1])[15] = 0;                       /* force NUL termination */
    const char *name = (const char *)&hdr[1];
    uint32_t w = hdr[5], h = hdr[6], maxval = hdr[7], passes = hdr[8];
    uint32_t window_on = hdr[9] & 1u, center = hdr[10], width = hdr[11];

    const filter_rec *rec = filter_table;
    while (rec->name && !name_eq(rec->name, name)) rec++;

    uint32_t status = PS_OK;
    if (!rec->name) status = PS_BAD_FILTER;
    else if (streaming ? (w == 0 || h == 0 || w > PROTO_STREAM_MAX_WIDTH || h > PROTO_STREAM_MAX_HEIGHT)
                       : (w == 0 || h == 0 || w > PROTO_MAX_PIXELS || h > PROTO_MAX_PIXELS ||
                          (uint64_t)w * h > PROTO_MAX_PIXELS)) status = PS_BAD_DIMS;
    else if (maxval == 0 || maxval > 65535u) status = PS_BAD_MAXVAL;
    else if (passes != PASSES_DEFAULT && passes > MAX_PASSES) status = PS_BAD_PASSES;
    else if (window_on && (width == 0 || width > 65535u || center > 65535u)) status = PS_BAD_WINDOW;

    if (status != PS_OK) {
        if (send_resp(io, status, w, h, 0, maxval, 0, 0, 0, 0) < 0) return -1;
        return 0;                                    /* rejected: nothing follows */
    }
    if (passes == PASSES_DEFAULT) passes = (uint32_t)rec->passes;

    if (streaming) return serve_stream(io, rec, w, h, maxval, passes, (int)window_on, center, width);

    if (send_resp(io, PS_OK, w, h, 0, maxval, 0, 0, 0, 0) < 0) return -1;

    /* 4. The samples, straight into buf_a (wire format == native RV32 layout). */
    uint16_t *buf_a = (uint16_t *)arena, *buf_b = buf_a + PROTO_MAX_PIXELS;
    uint32_t n = w * h;
    rc = read_exact(io, (uint8_t *)buf_a, n * 2, 3000);
    if (rc < 0) return -1;
    if (rc == 0) { send_resp(io, PS_TIMEOUT, w, h, 0, maxval, 0, 0, 0, 0); return 0; }

    for (uint32_t i = 0; i < n; i++)
        if (buf_a[i] > maxval) {
            send_resp(io, PS_BAD_SAMPLE, w, h, 0, maxval, 0, 0, 0, 0);
            return 0;
        }

    /* 5. Filter, timing it with the hardware counters. */
    uint32_t c0 = io->cycles(), i0 = io->instret();
    uint16_t *res = (uint16_t *)filter_core(buf_a, buf_b, w, h, rec, passes, maxval);
    uint32_t c1 = io->cycles(), i1 = io->instret();

    /* 6. Optional window/level to 8 bits, then reply. */
    if (window_on) {
        uint8_t *bytes = (uint8_t *)(res == buf_a ? buf_b : buf_a);
        window_level(res, bytes, n, center, width);
        if (send_resp(io, PS_OK, w, h, 1, 255, c1 - c0, i1 - i0, n, 0) < 0) return -1;
        return io->write(io->ctx, bytes, n) < 0 ? -1 : 0;
    }
    if (send_resp(io, PS_OK, w, h, 2, maxval, c1 - c0, i1 - i0, n * 2, 0) < 0) return -1;
    return io->write(io->ctx, (const uint8_t *)res, n * 2) < 0 ? -1 : 0;
}
