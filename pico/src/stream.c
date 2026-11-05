/* stream.c - the row pipeline described in stream.h. */
#include "stream.h"

extern void filter_row(uint16_t *dst, const uint16_t *above, const uint16_t *cur, const uint16_t *below,
                       uint32_t w, const filter_rec *rec, uint32_t maxval);

/* Slot i (0..2) of stage k's ring. */
static uint16_t *slot(const stream_t *s, uint32_t k, uint32_t i)
{
    return s->ring + ((uint32_t)3 * k + i) * s->w;
}

uint32_t stream_memory_needed(uint32_t width, uint32_t passes)
{
    if (width == 0 || passes > STREAM_MAX_PASSES) return 0;
    return (passes * 3u + 1u) * width * 2u;       /* the rings plus one output row */
}

int stream_begin(stream_t *s, void *arena, uint32_t arena_bytes, uint32_t width, uint32_t height,
                 uint32_t maxval, const filter_rec *rec, uint32_t passes, stream_sink_fn sink, void *sink_ctx,
                 uint32_t (*cycles)(void), uint32_t (*instret)(void))
{
    uint32_t need = stream_memory_needed(width, passes);
    if (!need || !height || !sink || !rec || need > arena_bytes || ((uintptr_t)arena & 3u)) return -1;
    s->w = width; s->h = height; s->maxval = maxval; s->n = passes; s->rec = rec;
    s->ring = (uint16_t *)arena;
    s->sink_row = s->ring + (uint32_t)3 * passes * width;
    for (uint32_t k = 0; k <= passes; k++) s->in_count[k] = s->out_count[k] = 0;
    s->sink = sink; s->sink_ctx = sink_ctx; s->sink_rows = 0;
    s->cycles_fn = cycles; s->instret_fn = instret; s->cyc = s->ins = 0;
    return 0;
}

uint16_t *stream_input_slot(stream_t *s)
{
    /* With no passes the input row is delivered straight to the sink from this slot. */
    return s->n ? slot(s, 0, s->in_count[0] % 3u) : s->sink_row;
}

/* Stage k can produce output row j = out_count[k] once rows j-1, j, j+1 have arrived,
 * or, after the last input row has arrived, as soon as its (replicated) neighbours exist. */
static int can_emit(const stream_t *s, uint32_t k)
{
    uint32_t in = s->in_count[k], out = s->out_count[k];
    return out < s->h && (out + 1 < in || in == s->h);
}

static void emit_one(stream_t *s, uint32_t k)
{
    uint32_t j = s->out_count[k];
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
