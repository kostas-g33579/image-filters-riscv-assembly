/* proto.c - request handling around the assembly filter core (src/kernel.s). */
#include "proto.h"

/* One record of the assembly filter table (src/records.inc): 64 bytes.
 * On RV32 the name pointer is 4 bytes followed by 4 bytes of padding. */
typedef struct {
    const char *name;
    uint32_t name_pad;
    int32_t kernel[9];
    int32_t div, bias, abs_flag, passes, kernel2_off;
} filter_rec;
_Static_assert(sizeof(filter_rec) == 64, "filter record layout out of sync with records.inc");
_Static_assert(sizeof(void *) == 4, "this protocol layer targets RV32");

extern const filter_rec filter_table[];   /* kernel.s; ends with a null name */
extern void *filter_core(const uint16_t *src, uint16_t *scratch, uint32_t w,
                         uint32_t h, const filter_rec *rec, uint32_t passes,
                         uint32_t maxval);
extern void window_level(const uint16_t *src, uint8_t *dst, uint32_t n,
                         uint32_t center, uint32_t width);

#define REQ_MAGIC  0x31474D49u   /* "IMG1" as a little-endian word */
#define RESP_MAGIC 0x3154554Fu   /* "OUT1" */
#define REQ_WORDS  12            /* 48-byte request header */
#define RESP_WORDS 10            /* 40-byte response header */
#define PASSES_DEFAULT 0xFFFFFFFFu
#define MAX_PASSES 255u

static uint16_t buf_a[PROTO_MAX_PIXELS] __attribute__((aligned(4)));
static uint16_t buf_b[PROTO_MAX_PIXELS] __attribute__((aligned(4)));

static int read_exact(const proto_io *io, uint8_t *dst, uint32_t n, int32_t timeout_ms)
{
    uint32_t got = 0;
    while (got < n) {
        int r = io->read(io->ctx, dst + got, n - got, timeout_ms);
        if (r < 0) return -1;      /* link closed */
        if (r == 0) return 0;      /* timed out */
        got += (uint32_t)r;
    }
    return 1;
}

static int send_resp(const proto_io *io, uint32_t status, uint32_t w, uint32_t h,
                     uint32_t bps, uint32_t maxval, uint32_t cycles,
                     uint32_t instret, uint32_t payload)
{
    uint32_t r[RESP_WORDS] = { RESP_MAGIC, status, w, h, bps, maxval, cycles,
                               instret, payload, 0 };
    return io->write(io->ctx, (const uint8_t *)r, sizeof r);
}

static int name_eq(const char *a, const char *b)
{
    while (*a && *a == *b) { a++; b++; }
    return *a == *b;
}

int proto_serve_one(const proto_io *io)
{
    uint32_t hdr[REQ_WORDS];

    /* 1. Find the magic number, skipping any noise, however long. */
    uint32_t win = 0;
    for (;;) {
        uint8_t b;
        int r = io->read(io->ctx, &b, 1, -1);
        if (r < 0) return -1;
        if (r == 0) continue;
        win = (win >> 8) | ((uint32_t)b << 24);
        if (win == REQ_MAGIC) break;
    }
    hdr[0] = REQ_MAGIC;

    /* 2. The rest of the header. */
    int rc = read_exact(io, (uint8_t *)&hdr[1], sizeof hdr - 4, 1000);
    if (rc < 0) return -1;
    if (rc == 0) { send_resp(io, PS_TIMEOUT, 0, 0, 0, 0, 0, 0, 0); return 0; }

    /* 3. Validate everything the header says before accepting any samples. */
    ((char *)&hdr[1])[15] = 0;                       /* force NUL termination */
    const char *name = (const char *)&hdr[1];
    uint32_t w = hdr[5], h = hdr[6], maxval = hdr[7], passes = hdr[8];
    uint32_t window_on = hdr[9] & 1u, center = hdr[10], width = hdr[11];

    const filter_rec *rec = filter_table;
    while (rec->name && !name_eq(rec->name, name)) rec++;

    uint32_t status = PS_OK;
    if (!rec->name) status = PS_BAD_FILTER;
    else if (w == 0 || h == 0 || w > PROTO_MAX_PIXELS || h > PROTO_MAX_PIXELS ||
             (uint64_t)w * h > PROTO_MAX_PIXELS) status = PS_BAD_DIMS;
    else if (maxval == 0 || maxval > 65535u) status = PS_BAD_MAXVAL;
    else if (passes != PASSES_DEFAULT && passes > MAX_PASSES) status = PS_BAD_PASSES;
    else if (window_on && (width == 0 || width > 65535u || center > 65535u)) status = PS_BAD_WINDOW;

    if (send_resp(io, status, w, h, 0, maxval, 0, 0, 0) < 0) return -1;
    if (status != PS_OK) return 0;                    /* rejected: nothing follows */

    /* 4. The samples, straight into buf_a (wire format == native RV32 layout). */
    uint32_t n = w * h;
    rc = read_exact(io, (uint8_t *)buf_a, n * 2, 3000);
    if (rc < 0) return -1;
    if (rc == 0) { send_resp(io, PS_TIMEOUT, w, h, 0, maxval, 0, 0, 0); return 0; }

    for (uint32_t i = 0; i < n; i++)
        if (buf_a[i] > maxval) {
            send_resp(io, PS_BAD_SAMPLE, w, h, 0, maxval, 0, 0, 0);
            return 0;
        }

    /* 5. Filter, timing it with the hardware counters. */
    if (passes == PASSES_DEFAULT) passes = (uint32_t)rec->passes;
    uint32_t c0 = io->cycles(), i0 = io->instret();
    uint16_t *res = (uint16_t *)filter_core(buf_a, buf_b, w, h, rec, passes, maxval);
    uint32_t c1 = io->cycles(), i1 = io->instret();

    /* 6. Optional window/level to 8 bits, then reply. */
    if (window_on) {
        uint8_t *bytes = (uint8_t *)(res == buf_a ? buf_b : buf_a);
        window_level(res, bytes, n, center, width);
        if (send_resp(io, PS_OK, w, h, 1, 255, c1 - c0, i1 - i0, n) < 0) return -1;
        return io->write(io->ctx, bytes, n) < 0 ? -1 : 0;
    }
    if (send_resp(io, PS_OK, w, h, 2, maxval, c1 - c0, i1 - i0, n * 2) < 0) return -1;
    return io->write(io->ctx, (const uint8_t *)res, n * 2) < 0 ? -1 : 0;
}
