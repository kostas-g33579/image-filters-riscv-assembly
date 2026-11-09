/* anon.c - the DICOM -> NIfTI anonymiser protocol. See anon.h for the wire format and the guarantees. */
#include "anon.h"

#define NO_FILE 0xFFFFFFFFu

typedef struct {
    const proto_io *io;
    anon_work *w;
    uint32_t unread;            /* bytes of the file being served that are still on the wire */
} ctx_t;

static void put32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24);
}
static uint32_t get32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

/* 1 = all n bytes read, 0 = timed out, -1 = link closed */
static int read_exact(const proto_io *io, uint8_t *dst, uint32_t n, int32_t timeout_ms)
{
    uint32_t got = 0;
    while (got < n) {
        int r = io->read(io->ctx, dst + got, n - got, timeout_ms);
        if (r < 0) return -1;
        if (r == 0) return 0;
        got += (uint32_t)r;
    }
    return 1;
}

/* read and throw away n bytes; same results as read_exact */
static int discard(const proto_io *io, anon_work *w, uint32_t n)
{
    while (n) {
        uint32_t k = n < ANON_CHUNK ? n : ANON_CHUNK;
        int r = read_exact(io, w->in, k, ANON_IO_TIMEOUT_MS);
        if (r <= 0) return r;
        n -= k;
    }
    return 1;
}

static int send_frame(const proto_io *io, uint8_t type, const uint8_t *payload, uint32_t n)
{
    uint8_t h[5];
    h[0] = type;
    put32(h + 1, n);
    if (io->write(io->ctx, h, 5) < 0) return -1;
    if (n && io->write(io->ctx, payload, n) < 0) return -1;
    return 0;
}

/* The error frame: the file index and "STAGE:NAME", both from fixed tables (nothing from the input). */
static int send_error(const proto_io *io, uint32_t file, const char *stage, const char *name)
{
    uint8_t p[4 + 48];
    uint32_t n = 4;
    put32(p, file);
    while (*stage && n < sizeof p - 1) p[n++] = (uint8_t)*stage++;
    p[n++] = ':';
    while (*name && n < sizeof p) p[n++] = (uint8_t)*name++;
    return send_frame(io, 'E', p, n);
}

/* an error was found in file idx: report it. 1 = reported (the caller drains the rest), -1 = link closed */
static int fail(ctx_t *c, uint32_t idx, const char *stage, const char *name)
{
    return send_error(c->io, idx, stage, name) < 0 ? -1 : 1;
}

/* convert `bytes` of input (a whole number of samples, at most ANON_CHUNK) and send them as one 'V' frame */
static int emit(ctx_t *c, const uint8_t *pix, uint32_t bytes)
{
    anon_work *w = c->w;
    uint32_t sb = nii_sample_bytes(&w->ns);
    w->frame[0] = 'V';
    put32(w->frame + 1, bytes);
    nii_convert_samples(&w->ns, pix, bytes / sb, w->frame + 5);
    return c->io->write(c->io->ctx, w->frame, 5 + bytes) < 0 ? -1 : 0;
}

/* One file. 0 = done, 1 = error reported (c->unread bytes of this file remain on the wire), 2 = error reported,
 * the host went quiet (nothing more to read), -1 = link closed. */
static int do_file(ctx_t *c, uint32_t idx)
{
    anon_work *w = c->w;
    const proto_io *io = c->io;
    uint8_t lenb[4];
    uint32_t total, avail, need, sb, px_avail, whole, done, rem, k;
    int r;

    r = read_exact(io, lenb, 4, ANON_IO_TIMEOUT_MS);
    if (r < 0) return -1;
    if (r == 0) return send_error(io, idx, "FILE", "TIMEOUT") < 0 ? -1 : 2;
    total = get32(lenb);
    c->unread = total;
    if (total > ANON_MAX_FILE) return fail(c, idx, "FILE", "TOO_LARGE");

    avail = total < ANON_HDR_MAX ? total : ANON_HDR_MAX;
    r = read_exact(io, w->head, avail, ANON_IO_TIMEOUT_MS);
    if (r < 0) return -1;
    if (r == 0) return send_error(io, idx, "FILE", "TIMEOUT") < 0 ? -1 : 2;
    c->unread -= avail;

    r = dcm_parse_head(w->head, avail, total, &w->info);
    if (r) return fail(c, idx, "PARSE", dcm_err_name(r));
    r = dcm_check(&w->info);
    if (r) return fail(c, idx, "CHECK", dcm_err_name(r));
    r = geom_add(&w->gs, &w->info);
    if (r) return fail(c, idx, "GEOM", geom_err_name(r));
    r = nii_add(&w->ns, &w->info);
    if (r) return fail(c, idx, "NII", nii_err_name(r));
    sc_add(&w->ss, &w->info);

    sb = nii_sample_bytes(&w->ns);
    need = (uint32_t)w->info.rows * w->info.cols * sb;

    /* the pixel bytes that are already in memory (pix_off <= avail: the header ended inside it) */
    px_avail = avail - w->info.pix_off;
    if (px_avail > need) px_avail = need;
    whole = px_avail - px_avail % sb;
    for (done = 0; done < whole; done += k) {
        k = whole - done < ANON_CHUNK ? whole - done : ANON_CHUNK;
        if (emit(c, w->head + w->info.pix_off + done, k) < 0) return -1;
    }
    if (px_avail != whole) {                        /* the cut fell inside a 16-bit sample: complete it from the wire */
        uint8_t two[2];
        r = read_exact(io, &two[1], 1, ANON_IO_TIMEOUT_MS);
        if (r < 0) return -1;
        if (r == 0) return send_error(io, idx, "FILE", "TIMEOUT") < 0 ? -1 : 2;
        c->unread -= 1;
        two[0] = w->head[w->info.pix_off + whole];
        if (emit(c, two, 2) < 0) return -1;
        done += 2;
    }
    for (rem = need - done; rem; rem -= k) {        /* the rest of the pixel data, as it arrives */
        k = rem < ANON_CHUNK ? rem : ANON_CHUNK;
        r = read_exact(io, w->in, k, ANON_IO_TIMEOUT_MS);
        if (r < 0) return -1;
        if (r == 0) return send_error(io, idx, "FILE", "TIMEOUT") < 0 ? -1 : 2;
        c->unread -= k;
        if (emit(c, w->in, k) < 0) return -1;
    }
    if (c->unread) {                                /* padding or anything after the samples: read and dropped */
        r = discard(io, w, c->unread);
        if (r < 0) return -1;
        if (r == 0) return send_error(io, idx, "FILE", "TIMEOUT") < 0 ? -1 : 2;
        c->unread = 0;
    }
    return 0;
}

int anon_serve_after_magic(const proto_io *io, anon_work *w)
{
    static const uint8_t reply_magic[4] = { 'N', 'I', 'I', '1' };
    uint8_t fixed[8];
    char *const dst[4] = { w->lab.subject, w->lab.sequence, w->lab.site, w->lab.scanner };
    int ok_label[4], r;
    uint32_t flags, nfiles, i, j;
    ctx_t c;

    c.io = io; c.w = w; c.unread = 0;
    if (io->write(io->ctx, reply_magic, 4) < 0) return -1;

    r = read_exact(io, fixed, 8, ANON_IO_TIMEOUT_MS);
    if (r < 0) return -1;
    if (r == 0) return send_error(io, NO_FILE, "REQUEST", "TIMEOUT") < 0 ? -1 : 0;
    flags = get32(fixed);
    nfiles = get32(fixed + 4);

    for (i = 0; i < 4; i++) {                        /* each label is read in full so the stream stays aligned */
        uint8_t n;
        r = read_exact(io, &n, 1, ANON_IO_TIMEOUT_MS);
        if (r > 0 && n) r = read_exact(io, w->label, n, ANON_IO_TIMEOUT_MS);
        if (r < 0) return -1;
        if (r == 0) return send_error(io, NO_FILE, "REQUEST", "TIMEOUT") < 0 ? -1 : 0;
        w->label[n] = 0;
        ok_label[i] = n != 0;
        for (j = 0; j < n; j++)
            if (w->label[j] == 0) ok_label[i] = 0;      /* a NUL would hide the rest of the label from the check */
        if (ok_label[i]) ok_label[i] = sc_valid_label((const char *)w->label);
        if (ok_label[i])
            for (j = 0; j <= n; j++) dst[i][j] = (char)w->label[j];     /* a valid label has at most SC_LABEL_MAX characters */
    }

    if (flags != 0) return send_error(io, NO_FILE, "REQUEST", "BAD_FLAGS") < 0 ? -1 : 0;
    if (nfiles == 0 || nfiles > ANON_MAX_FILES) return send_error(io, NO_FILE, "REQUEST", "BAD_COUNT") < 0 ? -1 : 0;
    if (!ok_label[0] || !ok_label[1] || !ok_label[2] || !ok_label[3])
        return send_error(io, NO_FILE, "REQUEST", "BAD_LABELS") < 0 ? -1 : 0;
    if (send_frame(io, 'A', 0, 0) < 0) return -1;

    geom_init(&w->gs);
    nii_init(&w->ns);
    sc_init(&w->ss);

    for (i = 0; i < nfiles; i++) {
        r = do_file(&c, i);
        if (r == 0) continue;
        if (r < 0) return -1;
        if (r == 2) return 0;
        /* an error was reported: read and discard the rest of the request so the next one starts cleanly */
        r = discard(io, w, c.unread);
        for (i++; r > 0 && i < nfiles; i++) {
            uint8_t lenb[4];
            r = read_exact(io, lenb, 4, ANON_IO_TIMEOUT_MS);
            if (r > 0) r = discard(io, w, get32(lenb));
        }
        return r < 0 ? -1 : 0;
    }

    r = geom_finish(&w->gs, &w->res);
    if (r) return send_error(io, NO_FILE, "GEOM", geom_err_name(r)) < 0 ? -1 : 0;
    r = nii_header(&w->ns, &w->res, w->hdr);
    if (r) return send_error(io, NO_FILE, "NII", nii_err_name(r)) < 0 ? -1 : 0;
    r = sc_build(&w->ss, &w->lab, &w->ns, &w->res, w->json, sizeof w->json);
    if (r < 0) return send_error(io, NO_FILE, "SIDECAR", "JSON_TOO_LONG") < 0 ? -1 : 0;
    if (send_frame(io, 'H', w->hdr, NII_HEADER_BYTES) < 0 || send_frame(io, 'J', (const uint8_t *)w->json, (uint32_t)r) < 0 ||
        send_frame(io, 'D', 0, 0) < 0)
        return -1;
    return 0;
}

int anon_serve_one(const proto_io *io, anon_work *w)
{
    uint32_t win = 0;
    for (;;) {
        uint8_t b;
        int r = io->read(io->ctx, &b, 1, -1);
        if (r < 0) return -1;
        if (r == 0) continue;
        win = (win >> 8) | ((uint32_t)b << 24);
        if (win == ANON_MAGIC) break;
    }
    return anon_serve_after_magic(io, w);
}
