/* gauss_linux.c - runs the separable Gaussian prototype (src/gauss.s) under qemu-riscv32 for testing.
 *
 * stdin : records, each little-endian u32: mode, W, H, r, then (r+1) u32 weights, then the payload; the run ends at
 *         end of input.
 *           mode 1  horizontal pass over ONE row:      payload W u16 samples; output W u16
 *           mode 2  vertical pass producing ONE row:   payload (2r+1) rows of W u16 samples (H is ignored); output W u16
 *           mode 3  the whole 2-D pass:                payload W*H u16 samples; output W*H u16
 *                   (every row through gauss_h, then every output row through gauss_v with the rows above and below
 *                    clamped to the first / last row: the composition a filter would use)
 * stdout: the output samples, little-endian, followed by GUARD extra u16 values that were pre-filled with 0xAAAA and must
 *         come back unchanged (a store past the end of the output row would show), and for mode 2 also the 4 u32 words
 *         after the accumulator, pre-filled with 0x5A5A5A5A.
 */
#define GUARD 4u
#include <stdint.h>

extern void gauss_h(const uint16_t *src, uint16_t *dst, uint32_t W, uint32_t r, const int32_t *w);
extern void gauss_v(const uint16_t *const *rows, uint16_t *dst, uint32_t W, uint32_t r, const int32_t *w, uint32_t *acc);

static long sys3(long n, long a, long b, long c)
{
    register long a7 __asm__("a7") = n;
    register long a0 __asm__("a0") = a;
    register long a1 __asm__("a1") = b;
    register long a2 __asm__("a2") = c;
    __asm__ volatile("ecall" : "+r"(a0) : "r"(a7), "r"(a1), "r"(a2) : "memory");
    return a0;
}

__attribute__((optimize("no-tree-loop-distribute-patterns"))) void *memset(void *d, int c, unsigned long n)
{ unsigned char *p = d; while (n--) *p++ = (unsigned char)c; return d; }
__attribute__((optimize("no-tree-loop-distribute-patterns"))) void *memcpy(void *d, const void *s, unsigned long n)
{ unsigned char *p = d; const unsigned char *q = s; while (n--) *p++ = *q++; return d; }

#define MAXW 1100u
#define MAXH 80u
static uint16_t img[MAXW * MAXH], tmp[MAXW * MAXH], out[MAXW * MAXH + 8];
static uint16_t rowbuf[(2 * 15 + 1) * MAXW];
static uint32_t acc[MAXW + 8];
static int32_t w[16];

static int read_full(void *dst, uint32_t n)
{
    uint8_t *p = dst;
    uint32_t got = 0;
    while (got < n) {
        long r = sys3(63, 0, (long)(p + got), (long)(n - got));
        if (r <= 0) return -1;
        got += (uint32_t)r;
    }
    return 0;
}
static int write_full(const void *src, uint32_t n)
{
    const uint8_t *p = src;
    while (n) {
        long r = sys3(64, 1, (long)p, (long)n);
        if (r <= 0) return -1;
        p += r; n -= (uint32_t)r;
    }
    return 0;
}

void run(void)
{
    uint32_t hdr[4];
    while (!read_full(hdr, sizeof hdr)) {
        uint32_t mode = hdr[0], W = hdr[1], H = hdr[2], r = hdr[3], y, k;
        if (r > 15 || W == 0 || W > MAXW || H > MAXH || read_full(w, (r + 1) * 4)) break;
        if (mode == 1) {
            if (read_full(img, W * 2)) break;
            for (k = 0; k < W + GUARD; k++) out[k] = 0xAAAA;
            gauss_h(img, out, W, r, w);
            write_full(out, (W + GUARD) * 2);
        } else if (mode == 2) {
            const uint16_t *rows[2 * 15 + 1];
            if (read_full(rowbuf, (2 * r + 1) * W * 2)) break;
            for (k = 0; k < 2 * r + 1; k++) rows[k] = rowbuf + k * W;
            for (k = 0; k < W + GUARD; k++) out[k] = 0xAAAA;
            for (k = 0; k < W + GUARD; k++) acc[k] = 0x5A5A5A5Au;
            gauss_v(rows, out, W, r, w, acc);
            write_full(out, (W + GUARD) * 2);
            write_full(acc + W, GUARD * 4);
        } else if (mode == 3) {
            if (H == 0 || read_full(img, W * H * 2)) break;
            for (k = 0; k < W * H + GUARD; k++) out[k] = 0xAAAA;
            for (y = 0; y < H; y++) gauss_h(img + y * W, tmp + y * W, W, r, w);
            for (y = 0; y < H; y++) {
                const uint16_t *rows[2 * 15 + 1];
                for (k = 0; k < 2 * r + 1; k++) {
                    int32_t yy = (int32_t)y + (int32_t)k - (int32_t)r;
                    if (yy < 0) yy = 0;
                    if (yy > (int32_t)H - 1) yy = (int32_t)H - 1;
                    rows[k] = tmp + (uint32_t)yy * W;
                }
                gauss_v(rows, out + y * W, W, r, w, acc);
            }
            write_full(out, (W * H + GUARD) * 2);
        } else break;
    }
    sys3(93, 0, 0, 0);
    for (;;) {}
}

__attribute__((naked)) void _start(void) { __asm__ volatile("call run"); }
