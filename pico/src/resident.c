/* resident.c - the resident-image protocol (see resident.h). */
#include "resident.h"

extern const filter_rec filter_table[];   /* kernel.s; ends with a null name */
extern void window_level(const uint16_t *src, uint8_t *dst, uint32_t n, uint32_t center, uint32_t width);

#define RESP_MAGIC 0x3154554Fu   /* "OUT1" */
#define ROW_MAGIC  0x31574F52u   /* "ROW1" */
#define RESP_WORDS 10
#define PASSES_DEFAULT 0xFFFFFFFFu
#define MAX_PASSES 255u
#define IO_TIMEOUT_MS 3000

/* The resident image. `loaded` is the only thing that says the arena still holds it. */
static struct {
    int loaded;
    uint32_t w, h, maxval;
    uint16_t *src;
    uint8_t *frame[2];
    int front;                 /* index of the front frame */
    uint32_t frames;           /* display frames finished since LOAD */
    uint8_t *rest;             /* what is left of the arena, for the pipeline */
    uint32_t rest_bytes;
} g;

void resident_invalidate(void) { g.loaded = 0; g.frames = 0; }

static uint32_t up16(uint32_t n) { return (n + 15u) & ~15u; }

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

static int send_resp(const proto_io *io, uint32_t status, uint32_t w, uint32_t h, uint32_t bps, uint32_t maxval,
                     uint32_t cycles, uint32_t instret, uint32_t payload, uint32_t reserved)
{
    uint32_t r[RESP_WORDS] = { RESP_MAGIC, status, w, h, bps, maxval, cycles, instret, payload, reserved };
    return io->write(io->ctx, (const uint8_t *)r, sizeof r);
}

static int name_eq(const char *a, const char *b)
{
    while (*a && *a == *b) { a++; b++; }
    return *a == *b;
}

/* Carve the arena for an image of w*h samples. */
static void layout(void *arena, uint32_t arena_bytes, uint32_t w, uint32_t h)
{
    uint32_t px = w * h, off = 0;
    g.src = (uint16_t *)arena;
    off += up16(px * 2u);
    g.frame[0] = (uint8_t *)arena + off;
    off += up16(px);
    g.frame[1] = (uint8_t *)arena + off;
    off += up16(px);
    g.rest = (uint8_t *)arena + off;
    g.rest_bytes = arena_bytes - off;
}

static int send_row(const proto_io *io, uint32_t index, const uint8_t *row, uint32_t bytes)
{
    uint32_t hdr[2] = { ROW_MAGIC, index };
    if (io->write(io->ctx, (const uint8_t *)hdr, sizeof hdr) < 0) return -1;
    return io->write(io->ctx, row, bytes);
}

/* ------------------------------------------------------------------ LOAD */

static int do_load(const proto_io *io, void *arena, uint32_t arena_bytes, uint32_t w, uint32_t h, uint32_t maxval)
{
    resident_invalidate();
    if (w == 0 || h == 0 || w > PROTO_MAX_PIXELS || h > PROTO_MAX_PIXELS || (uint64_t)w * h > PROTO_MAX_PIXELS)
        return send_resp(io, PS_BAD_DIMS, w, h, 0, maxval, 0, 0, 0, 0) < 0 ? -1 : 0;
    if (maxval == 0 || maxval > 65535u)
        return send_resp(io, PS_BAD_MAXVAL, w, h, 0, maxval, 0, 0, 0, 0) < 0 ? -1 : 0;
    layout(arena, arena_bytes, w, h);
    if (send_resp(io, PS_OK, w, h, 0, maxval, 0, 0, 0, g.rest_bytes) < 0) return -1;

    uint32_t n = w * h;
    int rc = read_exact(io, (uint8_t *)g.src, n * 2u, IO_TIMEOUT_MS);
    if (rc < 0) return -1;
    if (rc == 0) return send_resp(io, PS_TIMEOUT, w, h, 0, maxval, 0, 0, 0, 0) < 0 ? -1 : 0;
    for (uint32_t i = 0; i < n; i++)
        if (g.src[i] > maxval) return send_resp(io, PS_BAD_SAMPLE, w, h, 0, maxval, 0, 0, 0, 0) < 0 ? -1 : 0;
    g.w = w; g.h = h; g.maxval = maxval; g.front = 0; g.frames = 0; g.loaded = 1;
    return send_resp(io, PS_OK, w, h, 0, maxval, 0, 0, 0, g.rest_bytes) < 0 ? -1 : 0;
}

/* ------------------------------------------------------------------ APPLY */

typedef struct {
    const proto_io *io;
    uint8_t *back;             /* the frame being written (display mode) */
    uint32_t center, width;
    int send, raw16, failed;
} sink_ctx;

static void row_sink(void *vctx, uint32_t index, const uint16_t *row)
{
    sink_ctx *c = (sink_ctx *)vctx;
    if (c->failed) return;
    if (c->raw16) {
        if (c->send && send_row(c->io, index, (const uint8_t *)row, g.w * 2u) < 0) c->failed = 1;
        return;
    }
    uint8_t *dst = c->back + (uint64_t)index * g.w;
    window_level(row, dst, g.w, c->center, c->width);
    if (c->send && send_row(c->io, index, dst, g.w) < 0) c->failed = 1;
}

/* sep_state: 0 = no kernel, 1 = a valid kernel is in `kernel`, 2 = the host sent an invalid one */
static int do_apply(const proto_io *io, stream_t *st, const sep_kernel *kernel, int sep_state, uint32_t flags,
                    uint32_t passes, uint32_t center, uint32_t width, const char *name)
{
    const int send = (flags & RES_F_SEND) != 0, sep = (flags & RES_F_SEP) != 0, raw16 = (flags & RES_F_RAW16) != 0;
    const uint32_t w = g.w, h = g.h, maxval = g.maxval;
    const uint32_t out_bps = raw16 ? 2u : 1u, out_maxval = raw16 ? maxval : 255u;
#define REJECT(status) return send_resp(io, (status), w, h, 0, maxval, 0, 0, 0, g.rest_bytes) < 0 ? -1 : 0

    if (!g.loaded) return send_resp(io, PS_NO_IMAGE, 0, 0, 0, 0, 0, 0, 0, 0) < 0 ? -1 : 0;
    const filter_rec *rec = filter_table;
    if (!sep)
        while (rec->name && !name_eq(rec->name, name)) rec++;
    if (!sep && !rec->name) REJECT(PS_BAD_FILTER);
    if (sep && sep_state != 1) REJECT(PS_BAD_KERNEL);
    if (passes != PASSES_DEFAULT && passes > MAX_PASSES) REJECT(PS_BAD_PASSES);
    if (!raw16 && (width == 0 || width > 65535u || center > 65535u)) REJECT(PS_BAD_WINDOW);
    if (passes == PASSES_DEFAULT) passes = sep ? 1u : (uint32_t)rec->passes;

    uint32_t need = sep ? stream_sep_memory_needed(w, kernel->r, passes) : stream_memory_needed(w, passes);
    if (!need || need > g.rest_bytes) REJECT(PS_NO_MEMORY);

    sink_ctx sc = { io, g.frame[1 - g.front], center, width, send, raw16, 0 };
    if ((sep ? stream_begin_sep(st, g.rest, g.rest_bytes, w, h, maxval, kernel, passes, row_sink, &sc, io->cycles, io->instret)
             : stream_begin(st, g.rest, g.rest_bytes, w, h, maxval, rec, passes, row_sink, &sc, io->cycles, io->instret)) < 0)
        REJECT(PS_NO_MEMORY);
    if (send_resp(io, PS_OK, w, h, out_bps, out_maxval, 0, 0, w * out_bps, g.rest_bytes) < 0) return -1;

    for (uint32_t y = 0; y < h; y++) {
        uint16_t *slot = stream_input_slot(st);
        const uint16_t *src = g.src + (uint64_t)y * w;
        for (uint32_t x = 0; x < w; x++) slot[x] = src[x];
        stream_commit(st);
        if (sc.failed) return -1;
    }
    if (!raw16) { g.front = 1 - g.front; g.frames++; }
    return send_resp(io, PS_OK, w, h, out_bps, out_maxval, stream_cycles(st), stream_instret(st),
                     send ? h * w * out_bps : 0, g.frames) < 0 ? -1 : 0;
#undef REJECT
}

/* ------------------------------------------------------------------ GET */

static int do_get(const proto_io *io)
{
    if (!g.loaded || g.frames == 0) return send_resp(io, PS_NO_IMAGE, 0, 0, 0, 0, 0, 0, 0, 0) < 0 ? -1 : 0;
    const uint8_t *f = g.frame[g.front];
    if (send_resp(io, PS_OK, g.w, g.h, 1, 255, 0, 0, g.w, g.rest_bytes) < 0) return -1;
    for (uint32_t y = 0; y < g.h; y++)
        if (send_row(io, y, f + (uint64_t)y * g.w, g.w) < 0) return -1;
    return send_resp(io, PS_OK, g.w, g.h, 1, 255, 0, 0, g.h * g.w, g.frames) < 0 ? -1 : 0;
}

/* ------------------------------------------------------------------ request */

int resident_serve_after_magic(const proto_io *io, void *arena, uint32_t arena_bytes, stream_t *st, sep_kernel *kernel)
{
    uint32_t hdr[RES_HDR_WORDS];
    hdr[0] = RES_MAGIC;
    int rc = read_exact(io, (uint8_t *)&hdr[1], sizeof hdr - 4, 1000);
    if (rc < 0) return -1;
    if (rc == 0) return send_resp(io, PS_TIMEOUT, 0, 0, 0, 0, 0, 0, 0, 0) < 0 ? -1 : 0;

    const uint32_t cmd = hdr[1], w = hdr[2], h = hdr[3], maxval = hdr[4], flags = hdr[5], passes = hdr[6], center = hdr[7],
                   width = hdr[8];
    ((char *)&hdr[10])[15] = 0;                          /* force NUL termination of the name */
    const char *name = (const char *)&hdr[10];

    /* A separable kernel follows the header of an APPLY: always read it, whether or not the request is accepted. */
    int sep_state = 0;
    if (cmd == RES_APPLY && (flags & RES_F_SEP)) {
        uint32_t kr = 0;
        rc = read_exact(io, (uint8_t *)&kr, 4, 1000);
        if (rc < 0) return -1;
        if (rc == 0) return send_resp(io, PS_TIMEOUT, 0, 0, 0, 0, 0, 0, 0, 0) < 0 ? -1 : 0;
        if (kr > SEP_MAX_RADIUS) {
            sep_state = 2;
        } else {
            rc = read_exact(io, (uint8_t *)kernel->w, (kr + 1) * 4, 1000);
            if (rc < 0) return -1;
            if (rc == 0) return send_resp(io, PS_TIMEOUT, 0, 0, 0, 0, 0, 0, 0, 0) < 0 ? -1 : 0;
            kernel->r = kr;
            sep_state = sep_kernel_valid(kernel) ? 1 : 2;
        }
    }

    int bad = hdr[9] != 0 || hdr[14] != 0 || hdr[15] != 0;
    if (cmd == RES_LOAD) bad |= flags != 0;
    else if (cmd == RES_APPLY || cmd == RES_GET) bad |= w != 0 || h != 0 || maxval != 0;
    if (cmd == RES_GET) bad |= flags != 0;
    if (cmd == RES_APPLY) bad |= (flags & ~(uint32_t)(RES_F_SEND | RES_F_SEP | RES_F_RAW16)) != 0;
    if (bad || cmd < RES_LOAD || cmd > RES_GET) return send_resp(io, PS_BAD_CMD, 0, 0, 0, 0, 0, 0, 0, 0) < 0 ? -1 : 0;

    if (cmd == RES_LOAD) return do_load(io, arena, arena_bytes, w, h, maxval);
    if (cmd == RES_GET) return do_get(io);
    return do_apply(io, st, kernel, sep_state, flags, passes, center, width, name);
}
