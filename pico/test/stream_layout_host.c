/* stream_layout_host.c - checks the memory layout of the separable stream (stream_begin_sep) on the host, with stubs for the
 * assembly: every region lies inside the bytes stream_sep_memory_needed() reports, the regions do not overlap, the 16-bit rows
 * are 2-byte aligned and the 32-bit accumulators 4-byte aligned for every width, radius and pass count, and one byte less
 * memory is refused. The emulator cannot test alignment (qemu lets a misaligned access through; the Hazard3 cores may trap).
 * Built with the host compiler by `make test-gauss-stream`. Prints "LAYOUT OK <n> cases" or the first failure. */
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "stream.h"

void filter_row(uint16_t *dst, const uint16_t *a, const uint16_t *c, const uint16_t *b, uint32_t w, const filter_rec *rec, uint32_t maxval)
{ (void)dst; (void)a; (void)c; (void)b; (void)w; (void)rec; (void)maxval; }
void gauss_h(const uint16_t *s, uint16_t *d, uint32_t W, uint32_t r, const int32_t *w) { (void)s; (void)d; (void)W; (void)r; (void)w; }
void gauss_v(const uint16_t *const *rows, uint16_t *d, uint32_t W, uint32_t r, const int32_t *w, uint32_t *acc)
{ (void)rows; (void)d; (void)W; (void)r; (void)w; (void)acc; }
static void sink(void *c, uint32_t i, const uint16_t *row) { (void)c; (void)i; (void)row; }

static uint8_t buf[2 * 1024 * 1024] __attribute__((aligned(16)));
static stream_t st;

#define FAIL(...) do { printf("LAYOUT FAIL: "); printf(__VA_ARGS__); printf("\n"); return 1; } while (0)

int main(void)
{
    unsigned cases = 0;
    for (uint32_t passes = 0; passes <= 4; passes++)
        for (uint32_t r = 0; r <= SEP_MAX_RADIUS; r++)
            for (uint32_t W = 1; W <= 70; W++) {
                sep_kernel k;
                memset(&k, 0, sizeof k);
                k.r = r;
                k.w[0] = SEP_WEIGHT_TOTAL;                                    /* the identity kernel is valid for any r */
                uint32_t need = stream_sep_memory_needed(W, r, passes);
                uintptr_t base = (uintptr_t)buf, end = base + need;
                if (!need) FAIL("no memory figure for W=%u r=%u passes=%u", W, r, passes);
                if (stream_begin_sep(&st, buf, need - 1, W, 5, 255, &k, passes, sink, 0, 0, 0) >= 0) FAIL("one byte less accepted: W=%u r=%u p=%u", W, r, passes);
                if (stream_begin_sep(&st, buf, need, W, 5, 255, &k, passes, sink, 0, 0, 0) < 0) FAIL("exact memory refused: W=%u r=%u p=%u", W, r, passes);
                uintptr_t ring = (uintptr_t)st.ring, ring_end = ring + (uintptr_t)(2 * r + 1) * passes * W * 2;
                uintptr_t stage = (uintptr_t)st.stage_row, sinkr = (uintptr_t)st.sink_row, acc = (uintptr_t)st.acc;
                if (ring != base) FAIL("ring does not start at the arena");
                if (ring_end > stage || stage + W * 2 > sinkr || sinkr + W * 2 > acc) FAIL("regions overlap or are out of order: W=%u r=%u p=%u", W, r, passes);
                if (acc + W * 4 > end) FAIL("the accumulators run past the memory reported: W=%u r=%u p=%u (%lu over)", W, r, passes, (unsigned long)(acc + W * 4 - end));
                if ((ring | stage | sinkr) & 1u) FAIL("a 16-bit row is misaligned: W=%u r=%u p=%u", W, r, passes);
                if (acc & 3u) FAIL("the 32-bit accumulators are misaligned: W=%u r=%u p=%u (offset %lu)", W, r, passes, (unsigned long)(acc & 3u));
                cases++;
            }
    /* the guards: a radius above 15, too many passes or a zero width give no memory figure, and an invalid kernel no stream */
    if (stream_sep_memory_needed(10, SEP_MAX_RADIUS + 1, 1) != 0) FAIL("a radius of 16 got a memory figure");
    if (stream_sep_memory_needed(10, 255, 1) != 0) FAIL("a radius of 255 got a memory figure");
    if (stream_sep_memory_needed(10, 1, STREAM_MAX_PASSES + 1) != 0) FAIL("256 passes got a memory figure");
    if (stream_sep_memory_needed(0, 1, 1) != 0) FAIL("a width of 0 got a memory figure");
    if (stream_sep_memory_needed(10, 1, STREAM_MAX_PASSES) == 0) FAIL("255 passes got no memory figure");
    {
        sep_kernel k;
        memset(&k, 0, sizeof k);
        k.r = SEP_MAX_RADIUS + 1;                                           /* the weights past w[15] read as 0, so the sum is right */
        k.w[0] = SEP_WEIGHT_TOTAL;
        if (sep_kernel_valid(&k)) FAIL("a kernel of radius 16 was valid");
        if (stream_begin_sep(&st, buf, sizeof buf, 10, 5, 255, &k, 1, sink, 0, 0, 0) >= 0) FAIL("a stream was begun with a kernel of radius 16");
        k.r = 2; k.w[0] = 8192; k.w[1] = 8192; k.w[2] = -4096;               /* the sum is 16384; the LAST weight is negative */
        if (sep_kernel_valid(&k)) FAIL("a kernel whose last weight is negative was valid");
        if (stream_begin_sep(&st, buf, sizeof buf, 10, 5, 255, &k, 1, sink, 0, 0, 0) >= 0) FAIL("a stream was begun with a negative weight (stream.c must refuse it itself)");
        k.w[2] = 4096; k.w[1] = 4096; k.w[0] = 8192 + 2 * 1;                 /* a sum of 16386 */
        if (stream_begin_sep(&st, buf, sizeof buf, 10, 5, 255, &k, 1, sink, 0, 0, 0) >= 0) FAIL("a stream was begun with a wrong sum (stream.c must refuse it itself)");
        k.w[0] = 8192; k.w[1] = 8192; k.w[2] = -4096;
        k.w[1] = -4096; k.w[2] = 8192;
        if (sep_kernel_valid(&k)) FAIL("a kernel whose first side weight is negative was valid");
        k.w[0] = -2; k.w[1] = 8193; k.w[2] = 0;
        if (sep_kernel_valid(&k)) FAIL("a kernel whose centre weight is negative was valid");
        k.r = 1; k.w[0] = 16386; k.w[1] = 0x7FFFFFFF; k.w[2] = 0;           /* non-negative, and the sum wraps around 32 bits to 16384 */
        if (sep_kernel_valid(&k)) FAIL("a kernel whose sum wraps around 32 bits was valid");
        k.r = 3; k.w[0] = 4436; k.w[1] = 3551; k.w[2] = 1823; k.w[3] = 600;  /* a genuine one is valid */
        if (!sep_kernel_valid(&k)) FAIL("a genuine kernel was refused");
    }
    printf("LAYOUT OK %u cases\n", cases);
    return 0;
}
