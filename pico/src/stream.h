/* stream.h - row-by-row filtering, so an image never has to fit in memory whole.
 *
 * A chain of N filter passes becomes a pipeline of N stages. Each stage keeps only
 * the last 3 rows of its input (a ring of 3 row buffers). When a stage has seen rows
 * j-1, j and j+1 it can produce output row j, which it writes straight into the NEXT
 * stage's ring (or, for the last stage, into the output row). At the top and bottom
 * of the image the missing neighbour row is the row itself, exactly as the
 * whole-image filter replicates its edges, so the result is bit-identical.
 *
 * Memory is  passes * 3 * width * 2 bytes  (+ 3 row buffers), independent of height.
 *
 * A stage can instead apply a SEPARABLE symmetric kernel of radius r (src/gauss.s, docs/separable_gaussian.md): then a
 * stage keeps the last 2r+1 rows, and takes the horizontal pass over each row as it arrives and the vertical pass over
 * rows j-r .. j+r (clamped at the image edges) to produce row j. Memory is passes * (2r+1) * width * 2 bytes plus a few
 * row buffers and a row of 32-bit accumulators, again independent of height.
 * No SDK, no libc: it runs on the Pico and under qemu-riscv32 (see proto.h).
 */
#ifndef STREAM_H
#define STREAM_H
#include <stdint.h>

#define STREAM_MAX_PASSES 255u

/* One record of the assembly filter table (src/records.inc): 64 bytes.
 * On RV32 the name pointer is 4 bytes followed by 4 bytes of padding. */
typedef struct {
    const char *name;
    uint32_t name_pad;
    int32_t kernel[9];
    int32_t div, bias, abs_flag, passes, kernel2_off;
} filter_rec;

/* A separable symmetric kernel for gauss.s: w[0] weighs the centre sample, w[k] each of the two at distance k. The weights
 * must be non-negative and sum to exactly 2^14 (the caller checks: proto.c does, sep_kernel_valid() below). */
#define SEP_MAX_RADIUS 15u
#define SEP_WEIGHT_TOTAL 16384
typedef struct {
    uint32_t r;
    int32_t w[SEP_MAX_RADIUS + 1];
} sep_kernel;

/* 1 if radius <= SEP_MAX_RADIUS, all weights w[0..r] >= 0 and w[0] + 2 * sum(w[1..r]) == 2^14. */
int sep_kernel_valid(const sep_kernel *k);

typedef struct stream stream_t;

/* Called for every finished output row, in order. `row` is the filtered samples
 * (16-bit), valid only during the call. */
typedef void (*stream_sink_fn)(void *ctx, uint32_t row_index, const uint16_t *row);

/* Bytes of working memory a stream needs, or 0 if the arguments are invalid. */
uint32_t stream_memory_needed(uint32_t width, uint32_t passes);

/* Set up a stream in `arena` (arena_bytes long, 4-byte aligned). Returns 0, or -1 if
 * the arguments are invalid or the arena is too small. `cycles` / `instret` (may be
 * NULL) are read around every filter_row call to account for filter time only. */
int stream_begin(stream_t *s, void *arena, uint32_t arena_bytes, uint32_t width, uint32_t height,
                 uint32_t maxval, const filter_rec *rec, uint32_t passes, stream_sink_fn sink, void *sink_ctx,
                 uint32_t (*cycles)(void), uint32_t (*instret)(void));

/* The same pipeline with `passes` stages that each apply the separable kernel k (which must stay valid for the whole
 * stream). Bytes needed, or 0 if the arguments are invalid. */
uint32_t stream_sep_memory_needed(uint32_t width, uint32_t r, uint32_t passes);
int stream_begin_sep(stream_t *s, void *arena, uint32_t arena_bytes, uint32_t width, uint32_t height,
                     uint32_t maxval, const sep_kernel *k, uint32_t passes, stream_sink_fn sink, void *sink_ctx,
                     uint32_t (*cycles)(void), uint32_t (*instret)(void));

/* Where the next input row must be written (width samples). Valid until stream_commit. */
uint16_t *stream_input_slot(stream_t *s);

/* The row written to stream_input_slot is complete: run the pipeline, which may call
 * the sink zero or more times (after the last row it flushes the bottom edge). */
void stream_commit(stream_t *s);

/* Rows delivered to the sink so far; and cycles / instructions spent in filter_row. */
uint32_t stream_rows_out(const stream_t *s);
uint32_t stream_cycles(const stream_t *s);
uint32_t stream_instret(const stream_t *s);

/* The stream object itself (a caller allocates it; its size is fixed). */
struct stream {
    uint32_t w, h, maxval, n;          /* n = number of stages (passes) */
    const filter_rec *rec;             /* a 3x3 filter, or NULL when sep is used */
    const sep_kernel *sep;             /* a separable kernel, or NULL */
    uint32_t rad, ring_rows;           /* rows needed on each side (1 for 3x3), and rows kept per stage (2*rad+1) */
    uint16_t *ring;                    /* n * ring_rows rows */
    uint16_t *stage_row;               /* separable only: where a row lands before its horizontal pass */
    uint32_t *acc;                     /* separable only: W accumulators for the vertical pass */
    uint16_t *sink_row;                /* one row buffer for the final stage's output */
    uint32_t in_count[STREAM_MAX_PASSES + 1];   /* stage k: rows received (slot n = the sink side) */
    uint32_t out_count[STREAM_MAX_PASSES + 1];  /* stage k: rows produced */
    stream_sink_fn sink;
    void *sink_ctx;
    uint32_t sink_rows;
    uint32_t (*cycles_fn)(void), (*instret_fn)(void);
    uint32_t cyc, ins;
};

#endif
