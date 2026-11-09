/* anon_probe.c - a test image for the on-device anonymiser modules (dicom.c, geom.c, nifti.c, sidecar.c).
 *
 * There is no USB protocol for the anonymiser yet, so this firmware proves the modules on the real chip: send it
 * any byte over the USB serial port and it runs the whole pipeline on the built-in synthetic series
 * (probe_series.inc; no real data) and prints, in the same format as the emulator harnesses, a status line and a
 * checksum of the converted voxels for every slice, the 352-byte NIfTI header as hex and the JSON sidecar, with
 * the cycle count of every stage. pico/tools/anon_probe_check.py compares that with what the emulator produces
 * for the same files, which shows whether the chip's compiler flags, software floating point and word size change
 * anything.
 */
#include <stdint.h>
#include <stdio.h>
#include "pico/stdlib.h"
#include "dicom.h"
#include "geom.h"
#include "nifti.h"
#include "sidecar.h"
#include "probe_series.inc"

static uint32_t cycles(void)
{
    uint32_t v;
    __asm__ volatile("csrr %0, mcycle" : "=r"(v));
    return v;
}

static dcm_info info;
static geom_state gs;
static geom_result res;
static nii_state ns;
static sc_state ss;
static sc_labels lab;
static uint8_t conv[4096], hdr[NII_HEADER_BYTES];
static char json[SC_MAX_JSON];

static void set(char *dst, const char *src)
{
    unsigned i = 0;
    while (src[i] && i < SC_LABEL_MAX) { dst[i] = src[i]; i++; }
    dst[i] = 0;
}

static void run(void)
{
    const unsigned char *p = probe_series;
    uint32_t i, k, t[8];
    int r;

    set(lab.subject, "P017"); set(lab.sequence, "T1c"); set(lab.site, "S02"); set(lab.scanner, "C01");
    geom_init(&gs);
    nii_init(&ns);
    sc_init(&ss);
    printf("PROBE-BEGIN\n");
    for (i = 0; i < PROBE_N; i++) {
        uint32_t len = probe_lens[i], h = 2166136261u, n;
        t[0] = cycles();
        r = dcm_parse(p, len, &info);
        t[1] = cycles();
        if (!r) r = dcm_check(&info);
        t[2] = cycles();
        if (r) { printf("F%u=ERR:%s\n", (unsigned)i, dcm_err_name(r)); goto done; }
        r = geom_add(&gs, &info);
        t[3] = cycles();
        if (r) { printf("F%u=ERR:%s\n", (unsigned)i, geom_err_name(r)); goto done; }
        r = nii_add(&ns, &info);
        if (r) { printf("F%u=ERR:%s\n", (unsigned)i, nii_err_name(r)); goto done; }
        n = (uint32_t)info.rows * info.cols * nii_sample_bytes(&ns);
        nii_convert(&ns, p + info.pix_off, conv);
        for (k = 0; k < n; k++) { h ^= conv[k]; h *= 16777619u; }
        t[4] = cycles();
        sc_add(&ss, &info);
        t[5] = cycles();
        printf("F%u=OK\nS%u=%08x\nC%u=parse %u check %u geom %u nii+convert %u sidecar %u\n", (unsigned)i, (unsigned)i,
               (unsigned)h, (unsigned)i, (unsigned)(t[1] - t[0]), (unsigned)(t[2] - t[1]), (unsigned)(t[3] - t[2]),
               (unsigned)(t[4] - t[3]), (unsigned)(t[5] - t[4]));
        p += len;
    }
    t[0] = cycles();
    r = geom_finish(&gs, &res);
    t[1] = cycles();
    if (!r) r = nii_header(&ns, &res, hdr);
    t[2] = cycles();
    if (r) { printf("RESULT=ERR:%d\n", r); goto done; }
    {
        int n = sc_build(&ss, &lab, &ns, &res, json, sizeof json);
        t[3] = cycles();
        printf("HEADER=");
        for (k = 0; k < NII_HEADER_BYTES; k++) printf("%02x", hdr[k]);
        printf("\n");
        if (n < 0) printf("RESULT=ERR:JSON_TOO_LONG\n");
        else printf("JSON_BEGIN\n%sJSON_END\n", json);
        printf("FINISH-CYCLES=geom_finish %u nii_header %u sidecar_build %u\n", (unsigned)(t[1] - t[0]),
               (unsigned)(t[2] - t[1]), (unsigned)(t[3] - t[2]));
    }
done:
    printf("PROBE-END\n");
}

int main(void)
{
    __asm__ volatile("csrw 0x320, zero");           /* Hazard3 holds mcycle stopped after reset; start it */
    stdio_init_all();
    for (;;) {
        (void)getchar();                            /* any byte starts a run */
        run();
    }
}
