/* proto.h - the image-filter wire protocol, independent of any hardware.
 *
 * Two request types share one header layout (see pico/README.md):
 *
 *   IMG1  whole image: the host sends all the samples, then the device filters the
 *         image in memory and returns it. Limited to PROTO_MAX_PIXELS pixels.
 *   STR1  streaming: the host sends the image ROW BY ROW and the device returns each
 *         filtered row as soon as it exists (a few rows behind), holding only a few
 *         rows of every filter pass in memory. Any height; the width is limited by
 *         the memory budget: passes * 3 * width * 2 bytes must fit PROTO_ARENA_BYTES.
 *
 * STR1 can also apply a separable symmetric kernel (a Gaussian, a box ...; src/gauss.s) in place of a named 3x3 filter:
 * set bit 1 of the request's `flags` and send, right after the 48-byte header, u32 r (0..15) and (r+1) signed 32-bit
 * weights w[0..r] (w[0] the centre, w[k] each of the two samples at distance k). The weights must be non-negative and sum
 * to exactly 2^14 (status 10 otherwise); `passes` then counts how many times the kernel is applied (default 1), and the
 * name field is ignored. The host computes the weights, so the device needs no exp() and the width can follow a size in
 * millimetres. Streaming only (IMG1 answers status 10). Memory: stream_sep_memory_needed() in stream.h.
 *
 * All integers are little-endian 32-bit; samples are unsigned 16-bit little-endian
 * on the wire whatever the image's maxval.
 *
 * This file and proto.c use no SDK and no libc, so the same code runs on the Pico
 * (USB serial, main.c) and under qemu-riscv32 (pipes, test/proto_linux.c).
 */
#ifndef PROTO_H
#define PROTO_H
#include <stdint.h>

/* Largest whole image (IMG1) the device accepts: two 16-bit buffers of this many
 * pixels live in the shared arena below. */
#define PROTO_MAX_PIXELS 90000u

/* Working memory shared by IMG1 (two whole-image buffers) and STR1 (the pipeline).
 * The RP2350 has 520 KB of SRAM; this leaves room for the SDK, USB and the stacks. */
#define PROTO_ARENA_BYTES 420000u

/* Sanity limits for STR1 (the real limit on width is the memory budget). */
#define PROTO_STREAM_MAX_WIDTH  16384u
#define PROTO_STREAM_MAX_HEIGHT (1u << 20)

enum proto_status {
    PS_OK = 0,
    PS_BAD_MAGIC = 1,      /* (not sent: the device resynchronises instead) */
    PS_BAD_FILTER = 2,
    PS_BAD_DIMS = 3,
    PS_BAD_MAXVAL = 4,
    PS_BAD_PASSES = 5,
    PS_BAD_WINDOW = 6,
    PS_BAD_SAMPLE = 7,     /* a sample exceeds maxval */
    PS_TIMEOUT = 8,        /* the host stopped sending */
    PS_NO_MEMORY = 9,      /* STR1: passes * 3 * width * 2 bytes do not fit the arena */
    PS_BAD_KERNEL = 10,    /* a separable kernel: radius above 15, a negative weight, a sum other than 2^14, or IMG1 */
    PS_NO_IMAGE = 11,      /* RES1: no resident image (never loaded, or another request used the arena), or no frame yet */
    PS_BAD_CMD = 12        /* RES1: unknown command, or a field that must be 0 is not */
};

typedef struct proto_io {
    /* Read up to n bytes into buf, waiting at most timeout_ms (-1 = forever).
     * Return the byte count (>= 1), 0 on timeout, or < 0 if the link closed. */
    int (*read)(void *ctx, uint8_t *buf, uint32_t n, int32_t timeout_ms);
    /* Write all n bytes. Return 0 on success, < 0 on failure. */
    int (*write)(void *ctx, const uint8_t *buf, uint32_t n);
    uint32_t (*cycles)(void);    /* free-running cycle counter (or 0) */
    uint32_t (*instret)(void);   /* instructions-retired counter (or 0) */
    void *ctx;
} proto_io;

/* Wait for one request and serve it. Returns 0 when a request was handled
 * (including ones answered with an error status), < 0 when the link closed. */
int proto_serve_one(const proto_io *io);

#endif
