/* resident_layout_host.c - checks the memory layout of the resident image (resident.c) on the host, with stubs for the assembly:
 * after a LOAD the source image, the two display frames and the pipeline region lie inside the arena in that order, do not
 * overlap, start on 16-byte boundaries, and together leave exactly the bytes the device reports for the pipeline. The
 * emulator cannot see this: two frames that overlap still give the right bytes through the protocol, because the front frame
 * is never read while the back one is written - which is exactly what a second core scanning the front frame would do.
 * resident.c is included so the test can look at its state. Built with the host compiler by `make test-resident`.
 * Prints "RESIDENT LAYOUT OK <n> cases" or the first failure. */
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "resident.c"

const filter_rec filter_table[] = { { 0 } };
void window_level(const uint16_t *s, uint8_t *d, uint32_t n, uint32_t c, uint32_t w) { (void)s; (void)d; (void)n; (void)c; (void)w; }
void filter_row(uint16_t *dst, const uint16_t *a, const uint16_t *c, const uint16_t *b, uint32_t w, const filter_rec *rec, uint32_t maxval)
{ (void)dst; (void)a; (void)c; (void)b; (void)w; (void)rec; (void)maxval; }
void gauss_h(const uint16_t *s, uint16_t *d, uint32_t W, uint32_t r, const int32_t *w) { (void)s; (void)d; (void)W; (void)r; (void)w; }
void gauss_v(const uint16_t *const *rows, uint16_t *d, uint32_t W, uint32_t r, const int32_t *w, uint32_t *acc)
{ (void)rows; (void)d; (void)W; (void)r; (void)w; (void)acc; }

typedef struct { const uint8_t *p; uint32_t left; } feed;
static int fread_(void *ctx, uint8_t *buf, uint32_t n, int32_t t)
{
    feed *f = ctx; (void)t;
    if (!f->left) return 0;
    if (n > f->left) n = f->left;
    memcpy(buf, f->p, n); f->p += n; f->left -= n;
    return (int)n;
}
static int fwrite_(void *ctx, const uint8_t *buf, uint32_t n) { (void)ctx; (void)buf; (void)n; return 0; }
static uint32_t zero(void) { return 0; }

#define ARENA_BYTES 420000u
static uint8_t arena[ARENA_BYTES + 64] __attribute__((aligned(16)));
static uint8_t input[64 + 2 * 90000];
static stream_t st;
static sep_kernel kern;

#define FAIL(...) do { printf("RESIDENT LAYOUT FAIL: "); printf(__VA_ARGS__); printf("\n"); return 1; } while (0)

static int one(uint32_t w, uint32_t h)
{
    uint32_t hdr[RES_HDR_WORDS] = { RES_MAGIC, RES_LOAD, w, h, 255 };
    memcpy(input, hdr, sizeof hdr);
    memset(input + sizeof hdr, 0, (size_t)w * h * 2);
    feed f = { input + 4, 64 - 4 + w * h * 2 };            /* the magic has been read already */
    proto_io io = { fread_, fwrite_, zero, zero, &f };
    memset(arena, 0xEE, sizeof arena);
    if (resident_serve_after_magic(&io, arena, ARENA_BYTES, &st, &kern) < 0) FAIL("%ux%u: serve failed", w, h);
    if (!g.loaded) FAIL("%ux%u: not loaded", w, h);
    uintptr_t base = (uintptr_t)arena, end = base + ARENA_BYTES, px = (uintptr_t)w * h;
    uintptr_t s = (uintptr_t)g.src, f0 = (uintptr_t)g.frame[0], f1 = (uintptr_t)g.frame[1], rest = (uintptr_t)g.rest;
    if (s != base) FAIL("%ux%u: the source does not start at the arena", w, h);
    if (s + px * 2 > f0) FAIL("%ux%u: the source overlaps display frame 0 (%lu bytes)", w, h, (unsigned long)(s + px * 2 - f0));
    if (f0 + px > f1) FAIL("%ux%u: display frame 0 overlaps frame 1", w, h);
    if (f1 + px > rest) FAIL("%ux%u: display frame 1 overlaps the pipeline region", w, h);
    if (rest + g.rest_bytes != end) FAIL("%ux%u: the pipeline region does not end at the arena end (reported %u bytes, real %lu)", w, h,
                                           g.rest_bytes, (unsigned long)(end - rest));
    if ((s | f0 | f1 | rest) & 15u) FAIL("%ux%u: a part is not 16-byte aligned", w, h);
    if (g.rest_bytes > ARENA_BYTES) FAIL("%ux%u: more pipeline bytes than the arena", w, h);
    return 0;
}

int main(void)
{
    unsigned cases = 0;
    for (uint32_t w = 1; w <= 40; w++)
        for (uint32_t h = 1; h <= 40; h++) { if (one(w, h)) return 1; cases++; }
    static const uint32_t big[][2] = { { 300, 300 }, { 90000, 1 }, { 1, 90000 }, { 256, 256 }, { 240, 240 }, { 301, 299 }, { 17, 5293 } };
    for (unsigned i = 0; i < sizeof big / sizeof big[0]; i++) { if (one(big[i][0], big[i][1])) return 1; cases++; }
    printf("RESIDENT LAYOUT OK %u cases\n", cases);
    return 0;
}
