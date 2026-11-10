/* gauss_probe.c - times the separable Gaussian prototype (src/gauss.s) on the real chip.
 *
 * Send it any byte over the USB serial port. For each radius in gauss_probe_weights.inc it builds the same deterministic
 * 256 x 96 16-bit test image, runs the horizontal pass over every row and then the vertical pass for every output row
 * (the rows above and below the image repeat the first / last row), and prints for each pass the cycles and instructions
 * retired between the start and the end of the whole pass (read from the core's own mcycle / minstret counters, with
 * interrupts off so USB work does not land in the window) and an FNV-1a checksum of the result.
 * pico/tools/gauss_probe_check.py recomputes the checksums with the Python model and prints cycles per pixel.
 */
#include <stdint.h>
#include <stdio.h>
#include "pico/stdlib.h"
#include "hardware/sync.h"
#include "gauss_probe_weights.inc"

extern void gauss_h(const uint16_t *src, uint16_t *dst, uint32_t W, uint32_t r, const int32_t *w);
extern void gauss_v(const uint16_t *const *rows, uint16_t *dst, uint32_t W, uint32_t r, const int32_t *w, uint32_t *acc);

#define W 256u
#define H 96u
static uint16_t img[W * H], mid[W * H], out[W * H];
static uint32_t acc[W];

static uint32_t cycles(void) { uint32_t v; __asm__ volatile("csrr %0, mcycle" : "=r"(v)); return v; }
static uint32_t instret(void) { uint32_t v; __asm__ volatile("csrr %0, minstret" : "=r"(v)); return v; }

static uint32_t fnv(const uint16_t *p, uint32_t n)
{
    uint32_t h = 2166136261u, i;
    for (i = 0; i < n; i++) {
        h ^= p[i] & 0xFF; h *= 16777619u;
        h ^= p[i] >> 8; h *= 16777619u;
    }
    return h;
}

static void run(void)
{
    uint32_t x = 12345, i, n, y, k;
    for (i = 0; i < W * H; i++) { x = x * 1664525u + 1013904223u; img[i] = (uint16_t)(x >> 12); }
    printf("GPROBE-BEGIN\nCFG W=%u H=%u image=%08x\n", (unsigned)W, (unsigned)H, (unsigned)fnv(img, W * H));
    for (n = 0; n < PROBE_NR; n++) {
        uint32_t r = probe_radii[n], c0, i0, c1, i1, c2, i2, irq;
        const int32_t *w = probe_w[n];
        irq = save_and_disable_interrupts();
        c0 = cycles(); i0 = instret();
        for (y = 0; y < H; y++) gauss_h(img + y * W, mid + y * W, W, r, w);
        c1 = cycles(); i1 = instret();
        for (y = 0; y < H; y++) {
            const uint16_t *rows[2 * 15 + 1];
            for (k = 0; k < 2 * r + 1; k++) {
                int32_t yy = (int32_t)y + (int32_t)k - (int32_t)r;
                if (yy < 0) yy = 0;
                if (yy > (int32_t)H - 1) yy = (int32_t)H - 1;
                rows[k] = mid + (uint32_t)yy * W;
            }
            gauss_v(rows, out + y * W, W, r, w, acc);
        }
        c2 = cycles(); i2 = instret();
        restore_interrupts(irq);
        printf("H r=%u cycles=%u instret=%u fnv=%08x\n", (unsigned)r, (unsigned)(c1 - c0), (unsigned)(i1 - i0), (unsigned)fnv(mid, W * H));
        printf("V r=%u cycles=%u instret=%u fnv=%08x\n", (unsigned)r, (unsigned)(c2 - c1), (unsigned)(i2 - i1), (unsigned)fnv(out, W * H));
    }
    printf("GPROBE-END\n");
}

int main(void)
{
    __asm__ volatile("csrw 0x320, zero");           /* Hazard3 holds mcycle / minstret stopped after reset; start them */
    stdio_init_all();
    for (;;) {
        (void)getchar();
        run();
    }
}
