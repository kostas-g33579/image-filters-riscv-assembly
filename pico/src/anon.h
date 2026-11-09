/* anon.h - the DICOM -> NIfTI anonymiser protocol, independent of any hardware (like proto.h).
 *
 * The host sends a series of DICOM files (already sorted along the slice normal; the device only verifies the
 * order) and gets back ONLY: the voxel values, a 352-byte NIfTI-1 header built from constants and validated
 * numbers, a JSON sidecar built the same way, and short fixed error names. Nothing else a file contains can reach
 * the reply: see dicom.h, nifti.h and sidecar.h for what each part may contain.
 *
 * Request  (all integers little-endian):
 *     "DCM1"
 *     u32 flags          0 (reserved; anything else is refused)
 *     u32 nfiles         1 .. ANON_MAX_FILES
 *     4 labels           subject, sequence, site, scanner: each u8 length + that many bytes (sc_valid_label)
 *     nfiles times:      u32 length of the file, then the file
 *   The host waits for the 'A' frame before it sends the files.
 *
 * Reply:
 *     "NII1"
 *     frames, each:      u8 type, u32 payload length, payload
 *       'A'  accepted: the request header is valid; send the files          (no payload)
 *       'V'  voxel bytes, in slice order, already converted to the output type (1..4096 bytes)
 *       'H'  the 352-byte NIfTI header                                      (after the last 'V')
 *       'J'  the JSON sidecar                                               (after 'H')
 *       'D'  done: everything is complete and valid; the host keeps its output only now
 *       'E'  error: u32 file index (0xFFFFFFFF = not about one file) + ASCII "STAGE:NAME" (a fixed table name).
 *            Nothing follows; whatever the host has received is to be thrown away.
 *
 * Only the first ANON_HDR_MAX bytes of a file are held in memory (all the tags the reader keeps are in a header of
 * about 3 KB in the data seen); the pixel data is converted and sent in pieces as it arrives, so the size of a file
 * does not matter. After an error the rest of the request is read and discarded so the next request starts on a
 * message boundary (giving up if the host goes quiet).
 */
#ifndef ANON_H
#define ANON_H
#include <stdint.h>
#include "proto.h"
#include "dicom.h"
#include "geom.h"
#include "nifti.h"
#include "sidecar.h"

#define ANON_MAGIC        0x314D4344u      /* "DCM1" as a little-endian word */
#define ANON_REPLY_MAGIC  0x3149494Eu      /* "NII1" */
#define ANON_MAX_FILES    4096u
#define ANON_MAX_FILE     (64u << 20)      /* a file longer than this is refused (a sanity bound, not a memory bound) */
#ifndef ANON_HDR_MAX
#define ANON_HDR_MAX      131072u          /* bytes of a file held in memory; the header must end within them */
#endif
#ifndef ANON_CHUNK
#define ANON_CHUNK        4096u            /* pixel bytes converted and sent at a time */
#endif
#define ANON_IO_TIMEOUT_MS 5000

/* Working memory; the caller provides it (about 150 KB), e.g. inside the filter arena. */
typedef struct {
    uint8_t head[ANON_HDR_MAX];
    uint8_t in[ANON_CHUNK];
    uint8_t label[256];                 /* a label as received, before it is checked */
    uint8_t frame[5 + ANON_CHUNK];
    uint8_t hdr[NII_HEADER_BYTES];
    dcm_info info;
    geom_state gs;
    geom_result res;
    nii_state ns;
    sc_state ss;
    sc_labels lab;
    char json[SC_MAX_JSON];
} anon_work;

/* Serve one request whose magic word has already been read. Returns 0 when the request was handled (also when
 * it was answered with an error frame), < 0 if the link closed. */
int anon_serve_after_magic(const proto_io *io, anon_work *w);

/* Wait for a "DCM1" (skipping any noise) and serve it. For tests and for a build that serves only this protocol. */
int anon_serve_one(const proto_io *io, anon_work *w);

#endif
