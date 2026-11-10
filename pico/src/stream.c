/* stream.c - the row pipeline described in stream.h. */
#include "stream.h"

extern void filter_row(uint16_t *dst, const uint16_t *above, const uint16_t *cur, const uint16_t *below,
                       uint32_t w, const filter_rec *rec, uint32_t maxval);
extern void gauss_h(const uint16_t *src, uint16_t *dst, uint32_t W, uint32_t r, const int32_t *w);
extern void gauss_v(const uint16_t *const *rows, uint16_t *dst, uint32_t W, uint32_t r, const int32_t *w, uint32_t *acc);

/* Slot i (0 .. ring_rows-1) of stage k's ring. */
static uint16_t *slot(const stream_t *s, uint32_t k, uint32_t i)
{
    return s->ring + (s->ring_rows * k + i) * s->w;
}

uint32_t stream_memory_needed(uint32_t width, uint32_t passes)
{
    if (width == 0 || passes > STREAM_MAX_PASSES) return 0;
    return (passes * 3u + 1u) * width * 2u;       /* the rings plus one output row */
}

int sep_kernel_valid(const sep_kernel *k)
{
    int64_t sum;
    uint32_t i;
    if (k->r > SEP_MAX_RADIUS) return 0;
    sum = k->w[0];
    for (i = 0; i <= k->r; i++)
        if (k->w[i] < 0) return 0;
    for (i = 1; i <= k->r; i++) sum += 2 * (int64_t)k->w[i];
    return sum == SEP_WEIGHT_TOTAL;
}

/* rings, then the staging row and the sink row, then (4-byte aligned) the accumulators */
uint32_t stream_sep_memory_needed(uint32_t width, uint32_t r, uint32_t passes)
{
    if (width == 0 || r > SEP_MAX_RADIUS || passes > STREAM_MAX_PASSES) return 0;
    return (((passes * (2u * r + 1u) + 2u) * width * 2u + 3u) & ~3u) + width * 4u;
}

static int begin_common(stream_t *s, uint32_t width, uint32_t height, uint32_t maxval, uint32_t passes,
                        stream_sink_fn sink, void *sink_ctx, uint32_t (*cycles)(void), uint32_t (*instret)(void))
{
    s->w = width; s->h = height; s->maxval = maxval; s->n = passes;
    for (uint32_t k = 0; k <= passes; k++) s->in_count[k] = s->out_count[k] = 0;
    s->sink = sink; s->sink_ctx = sink_ctx; s->sink_rows = 0;
    s->cycles_fn = cycles; s->instret_fn = instret; s->cyc = s->ins = 0;
    return 0;
}

int stream_begin(stream_t *s, void *arena, uint32_t arena_bytes, uint32_t width, uint32_t height,
                 uint32_t maxval, const filter_rec *rec, uint32_t passes, stream_sink_fn sink, void *sink_ctx,
                 uint32_t (*cycles)(void), uint32_t (*instret)(void))
{
    uint32_t need = stream_memory_needed(width, passes);
    if (!need || !height || !sink || !rec || need > arena_bytes || ((uintptr_t)arena & 3u)) return -1;
    begin_common(s, width, height, maxval, passes, sink, sink_ctx, cycles, instret);
    s->rec = rec; s->sep = 0; s->rad = 1; s->ring_rows = 3;
    s->ring = (uint16_t *)arena;
    s->sink_row = s->ring + (uint32_t)3 * passes * width;
    s->stage_row = 0; s->acc = 0;
    return 0;
}

int stream_begin_sep(stream_t *s, void *arena, uint32_t arena_bytes, uint32_t width, uint32_t height,
                     uint32_t maxval, const sep_kernel *k, uint32_t passes, stream_sink_fn sink, void *sink_ctx,
                     uint32_t (*cycles)(void), uint32_t (*instret)(void))
{
    uint32_t need;
    if (!k || !sep_kernel_valid(k)) return -1;
    need = stream_sep_memory_needed(width, k->r, passes);
    if (!need || !height || !sink || need > arena_bytes || ((uintptr_t)arena & 3u)) return -1;
    begin_common(s, width, height, maxval, passes, sink, sink_ctx, cycles, instret);
    s->rec = 0; s->sep = k; s->rad = k->r; s->ring_rows = 2u * k->r + 1u;
    s->ring = (uint16_t *)arena;
    s->stage_row = s->ring + s->ring_rows * passes * width;
    s->sink_row = s->stage_row + width;
    s->acc = (uint32_t *)(((uintptr_t)(s->sink_row + width) + 3u) & ~(uintptr_t)3u);
    return 0;
}

uint16_t *stream_input_slot(stream_t *s)
{
    /* With no passes the input row is delivered straight to the sink from this slot. */
    if (!s->n) return s->sink_row;
    return s->sep ? s->stage_row : slot(s, 0, s->in_count[0] % s->ring_rows);
}

/* Stage k can produce output row j = out_count[k] once the rows up to j + rad have arrived, or, after the last input row
 * has arrived, as soon as its (replicated) neighbours exist. */
static int can_emit(const stream_t *s, uint32_t k)
{
    uint32_t in = s->in_count[k], out = s->out_count[k];
    return out < s->h && (out + s->rad < in || in == s->h);
}

/* separable stage k receives the row in stage_row: its horizontal pass goes into the next slot of the ring */
static void arrive_sep(stream_t *s, uint32_t k)
{
    uint32_t c0 = s->cycles_fn ? s->cycles_fn() : 0, i0 = s->instret_fn ? s->instret_fn() : 0;
    gauss_h(s->stage_row, slot(s, k, s->in_count[k] % s->ring_rows), s->w, s->sep->r, s->sep->w);
    if (s->cycles_fn) s->cyc += s->cycles_fn() - c0;
    if (s->instret_fn) s->ins += s->instret_fn() - i0;
}

static void emit_sep(stream_t *s, uint32_t k)
{
    uint32_t j = s->out_count[k], r = s->sep->r, m;
    const uint16_t *rows[2 * SEP_MAX_RADIUS + 1];
    uint16_t *dst = (k + 1 < s->n) ? s->stage_row : s->sink_row;
    uint32_t c0, i0;
    for (m = 0; m < 2 * r + 1; m++) {
        int32_t idx = (int32_t)j + (int32_t)m - (int32_t)r;                  /* the neighbours, clamped to the image */
        if (idx < 0) idx = 0;
        if (idx > (int32_t)s->h - 1) idx = (int32_t)s->h - 1;
        rows[m] = slot(s, k, (uint32_t)idx % s->ring_rows);
    }
    c0 = s->cycles_fn ? s->cycles_fn() : 0; i0 = s->instret_fn ? s->instret_fn() : 0;
    gauss_v(rows, dst, s->w, r, s->sep->w, s->acc);
    if (s->cycles_fn) s->cyc += s->cycles_fn() - c0;
    if (s->instret_fn) s->ins += s->instret_fn() - i0;
    s->out_count[k] = j + 1;
    if (k + 1 < s->n) {
        arrive_sep(s, k + 1);                       /* the row now enters the next stage's ring */
        s->in_count[k + 1]++;
    } else {
        s->sink(s->sink_ctx, s->sink_rows++, s->sink_row);
    }
}

static void emit_one(stream_t *s, uint32_t k)
{
    uint32_t j = s->out_count[k];
    if (s->sep) { emit_sep(s, k); return; }
    uint32_t a = j ? j - 1 : 0, b = j + 1 < s->h ? j + 1 : s->h - 1;      /* neighbours, clamped */
    uint16_t *dst = (k + 1 < s->n) ? slot(s, k + 1, s->in_count[k + 1] % 3u) : s->sink_row;
    uint32_t c0 = s->cycles_fn ? s->cycles_fn() : 0, i0 = s->instret_fn ? s->instret_fn() : 0;
    filter_row(dst, slot(s, k, a % 3u), slot(s, k, j % 3u), slot(s, k, b % 3u), s->w, s->rec, s->maxval);
    if (s->cycles_fn) s->cyc += s->cycles_fn() - c0;
    if (s->instret_fn) s->ins += s->instret_fn() - i0;
    s->out_count[k] = j + 1;
    if (k + 1 < s->n) {
        s->in_count[k + 1]++;                       /* the row now sits in the next stage's ring */
    } else {
        s->sink(s->sink_ctx, s->sink_rows++, s->sink_row);
    }
}

void stream_commit(stream_t *s)
{
    if (!s->n) {                                    /* zero passes: the filter is the identity */
        s->sink(s->sink_ctx, s->sink_rows++, s->sink_row);
        s->in_count[0]++;
        return;
    }
    if (s->sep) arrive_sep(s, 0);                   /* the horizontal pass of the first stage, from the staging row */
    s->in_count[0]++;
    /* Depth-first: always work on the deepest stage that can produce, so a stage's ring
     * is never overwritten before the next stage has consumed it. */
    uint32_t k = 0;
    for (;;) {
        if (can_emit(s, k)) {
            emit_one(s, k);
            if (k + 1 < s->n) k++;                  /* a fresh row arrived downstream */
        } else if (k == 0) {
            break;
        } else {
            k--;                                    /* downstream is idle; maybe upstream has more (flush) */
        }
    }
}

uint32_t stream_rows_out(const stream_t *s) { return s->sink_rows; }
uint32_t stream_cycles(const stream_t *s) { return s->cyc; }
uint32_t stream_instret(const stream_t *s) { return s->ins; }
