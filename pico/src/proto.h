/* proto.h - the image-filter wire protocol, independent of any hardware.
 *
 * The host sends a request header, the device acknowledges (or rejects) it,
 * the host sends the samples, and the device replies with a result header and
 * the filtered samples. All integers are little-endian 32-bit; samples are
 * always unsigned 16-bit little-endian on the wire, whatever the image's
 * maxval. See pico/README.md for the byte layout.
 *
 * This file and proto.c use no SDK and no libc, so the same code runs on the
 * Pico (USB serial, main.c) and under qemu-riscv32 (pipes, test/proto_linux.c).
 */
#ifndef PROTO_H
#define PROTO_H
#include <stdint.h>

/* Largest image the device accepts. Two 16-bit buffers of this many pixels
 * must fit in the RP2350's 520 KB of SRAM with room for the SDK and stacks. */
#define PROTO_MAX_PIXELS 90000u

enum proto_status {
    PS_OK = 0,
    PS_BAD_MAGIC = 1,      /* (not sent: the device resynchronises instead) */
    PS_BAD_FILTER = 2,
    PS_BAD_DIMS = 3,
    PS_BAD_MAXVAL = 4,
    PS_BAD_PASSES = 5,
    PS_BAD_WINDOW = 6,
    PS_BAD_SAMPLE = 7,     /* a sample exceeds maxval */
    PS_TIMEOUT = 8         /* the host stopped sending */
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
