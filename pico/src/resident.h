/* resident.h - the resident-image protocol ("RES1"): upload an image ONCE, then change the filter or the window and get the
 * result without sending the image again. Independent of any hardware (like proto.h): the same code runs on the Pico and
 * under qemu-riscv32.
 *
 * The device keeps three things in the shared arena, in this order (every part rounded up to 16 bytes):
 *     S    the source image, 16-bit, exactly as uploaded; never modified, so every APPLY starts from the original
 *     D0   two 8-bit display frames of w*h bytes (window/level applied); one is the FRONT frame (the last
 *     D1   finished one), the other the BACK frame (the next one is written there, then they swap)
 *     the filter pipeline's row buffers (stream.h) in whatever is left
 * The front/back pair and the frame counter are the hand-off to a future second core that scans the front frame out to a
 * display; for now the front frame can be sent to the host.
 * Any other request (IMG1, STR1, DCM1) uses the same arena, so it DROPS the resident image: the next APPLY or GET answers
 * PS_NO_IMAGE.
 *
 * Request: a fixed 64-byte header, sixteen little-endian u32 words:
 *     0   "RES1"
 *     1   command: 1 LOAD, 2 APPLY, 3 GET (anything else: PS_BAD_CMD)
 *     2   width      LOAD only (0 for the other commands, else PS_BAD_CMD)
 *     3   height     LOAD only (same)
 *     4   maxval     LOAD only, 1..65535 (same)
 *     5   flags      APPLY only: bit 0 SEND (send the frame back), bit 1 SEP (a separable kernel follows the header, as in
 *                    STR1: u32 r, then r+1 signed 32-bit weights), bit 2 RAW16 (send / compute the 16-bit filtered rows
 *                    instead of a display frame; the display frames and the counter are left alone). Other bits and
 *                    LOAD/GET flags must be 0 (PS_BAD_CMD)
 *     6   passes     APPLY: how many times the filter is applied (0xFFFFFFFF = the filter's default; 1 for a kernel)
 *     7   center     APPLY: window centre   (not used with RAW16)
 *     8   width      APPLY: window width 1..65535 (not used with RAW16)
 *     9   reserved, 0
 *     10-13  the filter name, up to 15 characters and a NUL (APPLY without SEP)
 *     14-15  reserved, 0
 * Reply: the 40-byte "OUT1" header of proto.h (status, w, h, bytes per sample, maxval, cycles, instructions, payload,
 * reserved), always, as an ACKNOWLEDGEMENT first; a status other than PS_OK means nothing follows.
 *   LOAD   ack (OK); the host then sends w*h u16 samples; then a final OUT1 (OK, or PS_TIMEOUT / PS_BAD_SAMPLE; on any error
 *          there is no resident image)
 *   APPLY  ack (OK; bytes per sample 1 and maxval 255, or 2 and the image's maxval with RAW16; payload = bytes per row;
 *          reserved = bytes left for the pipeline); with SEND the rows follow, each "ROW1" + u32 row index + the row;
 *          then a final OUT1 with cycles and instructions spent in the filter, payload = bytes sent, reserved = the frame
 *          counter (the number of display frames finished since LOAD)
 *   GET    ack, the rows of the front frame (ROW1 as above), a final OUT1 (reserved = the frame counter);
 *          PS_NO_IMAGE until a frame has been finished
 * Statuses used besides proto.h's: PS_NO_IMAGE (11), PS_BAD_CMD (12).
 */
#ifndef RESIDENT_H
#define RESIDENT_H
#include <stdint.h>
#include "proto.h"
#include "stream.h"

#define RES_MAGIC 0x31534552u            /* "RES1" as a little-endian word */
#define RES_HDR_WORDS 16

enum { RES_LOAD = 1, RES_APPLY = 2, RES_GET = 3 };
enum { RES_F_SEND = 1, RES_F_SEP = 2, RES_F_RAW16 = 4 };

/* Forget the resident image (another request is about to use the arena). */
void resident_invalidate(void);

/* The "RES1" magic has just been read. Serves one RES1 request using the arena, the stream object and the kernel object
 * that proto.c owns. Returns 0, or < 0 when the link closed (like proto_serve_one). */
int resident_serve_after_magic(const proto_io *io, void *arena, uint32_t arena_bytes, stream_t *st, sep_kernel *kernel);

#endif
