/* gauss_linux.c - runs the separable Gaussian prototype (src/gauss.s) under qemu-riscv32 for testing.
 *
 * stdin : records, each little-endian u32: mode, W, H, r, then (r+1) u32 weights, then the payload; the run ends at
 *         end of input. The weights past w[r] are filled with 0x01010101 before every record, so reading one shows.
 *           mode 1  horizontal pass over ONE row:      payload W u16 samples; output W u16
 *           mode 2  vertical pass producing ONE row:   payload (2r+1) rows of W u16 samples (H is ignored); output W u16
 *           mode 3  the whole 2-D pass:                payload W*H u16 samples; output W*H u16
 *                   (every row through gauss_h, then every output row through gauss_v with the rows above and below
 *                    clamped to the first / last row: the composition a filter would use)
 *           mode 4  as mode 1 (payload W u16 samples), but through a shim that fills s0..s11 with distinct patterns first and
 *                   afterwards checks them; output: one u32, bit i set when s_i was changed (0 = all preserved)
 *           mode 5  the same for the vertical pass (payload (2r+1) rows of W u16 samples)
 * stdout: the output samples, little-endian, followed by GUARD extra u16 values that were pre-filled with 0xAAAA and must
 *         come back unchanged (a store past the end of the output row would show), and for mode 2 also the 4 u32 words
 *         after the accumulator, pre-filled with 0x5A5A5A5A.
 */
#define GUARD 4u
#include <stdint.h>

extern void gauss_h(const uint16_t *src, uint16_t *dst, uint32_t W, uint32_t r, const int32_t *w);
extern void gauss_v(const uint16_t *const *rows, uint16_t *dst, uint32_t W, uint32_t r, const int32_t *w, uint32_t *acc);

/* check_h / check_v: call gauss_h / gauss_v with s0..s11 holding known values; return a mask of the ones that changed. The
 * arguments are the routines' own (a0..a4, and a5 for the vertical pass). */
__asm__(
    ".text\n"
    ".macro FILL\n"
    ".set i, 0\n"
    ".endm\n"
    ".globl check_h\ncheck_h:\n    la t0, gauss_h\n    j check_common\n"
    ".globl check_v\ncheck_v:\n    la t0, gauss_v\n"
    "check_common:\n"
    "    addi sp, sp, -64\n    sw ra, 60(sp)\n    sw t0, 56(sp)\n"
    "    sw s0, 0(sp)\n    sw s1, 4(sp)\n    sw s2, 8(sp)\n    sw s3, 12(sp)\n    sw s4, 16(sp)\n    sw s5, 20(sp)\n"
    "    sw s6, 24(sp)\n    sw s7, 28(sp)\n    sw s8, 32(sp)\n    sw s9, 36(sp)\n    sw s10, 40(sp)\n    sw s11, 44(sp)\n"
    "    li s0, 0x10000001\n    li s1, 0x20000002\n    li s2, 0x30000003\n    li s3, 0x40000004\n    li s4, 0x50000005\n"
    "    li s5, 0x60000006\n    li s6, 0x70000007\n    li s7, 0x08000008\n    li s8, 0x09000009\n    li s9, 0x0A00000A\n"
    "    li s10, 0x0B00000B\n    li s11, 0x0C00000C\n"
    "    lw t0, 56(sp)\n    jalr ra, 0(t0)\n"
    "    li a0, 0\n"
    "    li t0, 0x10000001\n    xor t0, t0, s0\n    snez t0, t0\n    slli t0, t0, 0\n    or a0, a0, t0\n"
    "    li t0, 0x20000002\n    xor t0, t0, s1\n    snez t0, t0\n    slli t0, t0, 1\n    or a0, a0, t0\n"
    "    li t0, 0x30000003\n    xor t0, t0, s2\n    snez t0, t0\n    slli t0, t0, 2\n    or a0, a0, t0\n"
    "    li t0, 0x40000004\n    xor t0, t0, s3\n    snez t0, t0\n    slli t0, t0, 3\n    or a0, a0, t0\n"
    "    li t0, 0x50000005\n    xor t0, t0, s4\n    snez t0, t0\n    slli t0, t0, 4\n    or a0, a0, t0\n"
    "    li t0, 0x60000006\n    xor t0, t0, s5\n    snez t0, t0\n    slli t0, t0, 5\n    or a0, a0, t0\n"
    "    li t0, 0x70000007\n    xor t0, t0, s6\n    snez t0, t0\n    slli t0, t0, 6\n    or a0, a0, t0\n"
    "    li t0, 0x08000008\n    xor t0, t0, s7\n    snez t0, t0\n    slli t0, t0, 7\n    or a0, a0, t0\n"
    "    li t0, 0x09000009\n    xor t0, t0, s8\n    snez t0, t0\n    slli t0, t0, 8\n    or a0, a0, t0\n"
    "    li t0, 0x0A00000A\n    xor t0, t0, s9\n    snez t0, t0\n    slli t0, t0, 9\n    or a0, a0, t0\n"
    "    li t0, 0x0B00000B\n    xor t0, t0, s10\n    snez t0, t0\n    slli t0, t0, 10\n    or a0, a0, t0\n"
    "    li t0, 0x0C00000C\n    xor t0, t0, s11\n    snez t0, t0\n    slli t0, t0, 11\n    or a0, a0, t0\n"
    "    lw s0, 0(sp)\n    lw s1, 4(sp)\n    lw s2, 8(sp)\n    lw s3, 12(sp)\n    lw s4, 16(sp)\n    lw s5, 20(sp)\n"
    "    lw s6, 24(sp)\n    lw s7, 28(sp)\n    lw s8, 32(sp)\n    lw s9, 36(sp)\n    lw s10, 40(sp)\n    lw s11, 44(sp)\n"
    "    lw ra, 60(sp)\n    addi sp, sp, 64\n    ret\n");
extern uint32_t check_h(const uint16_t *src, uint16_t *dst, uint32_t W, uint32_t r, const int32_t *w);
extern uint32_t check_v(const uint16_t *const *rows, uint16_t *dst, uint32_t W, uint32_t r, const int32_t *w, uint32_t *acc);

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
        if (r > 15 || W == 0 || W > MAXW || H > MAXH) break;
        for (k = 0; k < 16; k++) w[k] = 0x01010101;                       /* poison: a routine that reads a weight past w[r] gets a wrong answer */
        if (read_full(w, (r + 1) * 4)) break;
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
        } else if (mode == 4) {
            uint32_t mask;
            if (read_full(img, W * 2)) break;
            mask = check_h(img, out, W, r, w);
            write_full(&mask, 4);
        } else if (mode == 5) {
            const uint16_t *rows[2 * 15 + 1];
            uint32_t mask;
            if (read_full(rowbuf, (2 * r + 1) * W * 2)) break;
            for (k = 0; k < 2 * r + 1; k++) rows[k] = rowbuf + k * W;
            mask = check_v(rows, out, W, r, w, acc);
            write_full(&mask, 4);
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
