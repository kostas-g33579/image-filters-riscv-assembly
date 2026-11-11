# Resident-image mode (`RES1`)

Upload an image to the board once, then change the filter or the window and get each result without sending the image again.
This is the first step towards the interactive viewer: a viewer changes a parameter many times on the same slice, and with
the image resident each change costs the filtering plus the display frame, not an upload as well.

    sudo python3 pico/tools/pico_resident.py data/slices/BRATS_001_t1c_z77.pgm --window 761 1350 gauss:1.0 gauss:2.0:6 blur sobel

Each STEP (`NAME`, `NAME:PASSES`, `gauss:SIGMA`, `gauss:SIGMA:RADIUS`) starts from the original image on the board and gives
an 8-bit display frame. `--out-prefix P` writes the frames, `--repeat N` repeats the list for timing. In Python,
`ResidentSession` (`pico/tools/pico_resident.py`) is what a viewer would use. The wire format is in the header comment of
`pico/src/resident.h`.

## What the device keeps

One arena (420,000 bytes, shared with every other request), carved in this order, each part rounded up to 16 bytes:

| part | size | |
|---|---|---|
| `S` | 2 bytes per pixel | the source image, 16-bit, exactly as uploaded. Never modified, so every step starts from the original |
| `D0`, `D1` | 1 byte per pixel each | two 8-bit display frames (window/level applied): one is the **front** frame (the last finished one), the other the **back** frame (the next one is written there, then they swap) |
| pipeline | what is left | the row buffers of the streaming filter (`stream.h`) |

A 240 x 240 slice uses 115 + 58 + 58 = 230 KB and leaves 190 KB; a 90,000-pixel image (the whole-image limit) leaves 60,000
bytes. Filtering reads `S` row by row through the existing streaming pipeline, so the 3x3 filters and the separable Gaussian
are exactly the code and the results already tested elsewhere; a row sink applies the window and writes the back frame
(and sends it to the host when asked). When the last row is done the frames swap and a frame counter goes up.

Any other request (`IMG1`, `STR1`, `DCM1`) uses the same arena, so it **drops** the resident image: the next APPLY or GET
answers `PS_NO_IMAGE` and the client loads again.

## Commands

- **LOAD** uploads the image (width, height, maxval); any earlier resident image is gone, also when the load fails.
- **APPLY** filters `S` into the back frame and swaps. Flags: `SEND` sends the frame back, `SEP` follows with a separable
  kernel (as in `STR1`), `RAW16` produces and sends the 16-bit filtered rows instead of a display frame (the real values, for
  data and measurements; the display frames and the counter are left alone). The reply carries the cycles and instructions
  the core spent filtering.
- **GET** sends the front frame again without computing.

## Why two frames, and the second core

The front/back pair and the counter are the hand-off to a future second core that scans the front frame out to a display
(HDMI) while the first core computes the next frame into the back one. Nothing is shared except "which is the front frame" and
the counter, so no frame is ever half written when it is shown. USB stays on core 0 for good (it is the host link: uploading a
series, the anonymiser, commands), and core 1 is kept for the scanout, which is the one job that needs a core of its own. Today the
"display" is the host: the PC window gets the frame from the same USB reply.

What this does and does not buy over USB: a parameter change no longer sends the image (115 KB for a 240 x 240 slice), but the
display frame still has to cross USB (58 KB, about 105 ms at the measured 546 KB/s). The large gain comes with a display on the
board, where no frame crosses USB at all and the cost of a change is the filtering alone (19 to 80 ms at 240 x 240).

## Cost (240 x 240, predicted before the first board run)

| step | filter on the core | round trip |
|---|--:|--:|
| LOAD (once) | - | about 0.21 s (115 KB up) |
| `gauss:0.5` (r = 1) | about 14 ms | about 105-115 ms (the frame download) |
| `gauss:1.0` (r = 3) | about 19 ms + overhead | about 110-125 ms |
| `gauss:2.0:6` | about 64 ms | about 115-135 ms |
| `blur` (8 passes) | about 145 ms | about 150-200 ms |

The rows go out as they are finished, so most of the filtering overlaps with the download; a step is bounded by the larger of
the two plus a little. Against 0.22 s per change today (upload and 16-bit download), about 2x for the cheap filters. Measured
values go here after the board run.

## Tests (`make test-resident`, about 2 s)

- `tests/test_resident.py`, against the emulator: display frames bit-exact against the verified Linux harness and an independent
  Gaussian model (7 images x 11 filters in random order, random windows); every step starts from the original; GET, SEND, RAW16
  and the frame counter; the largest pass count that fits fills the pipeline region and leaves the source and both frames intact;
  other requests drop the image; every bad request (command, reserved fields, flags, dimensions, maxval, sample, filter, passes,
  window, kernel, memory, nothing loaded, a host that stops mid-upload) answers its status and leaves the device in step.
- `pico/test/resident_layout_host.c`, on the host: source, two frames and pipeline region lie in the arena in order, do not
  overlap, start on 16-byte boundaries and leave exactly the reported bytes, for 1,607 sizes. The emulator cannot see this: two
  frames that overlap still give the right bytes, because the front frame is never read while the back one is written, which is
  what a scanning core would do.
- A mutation sweep of 48 changes to `resident.c` and the hook in `proto.c`, plus a control that must survive: 43 caught. The five
  survivors behave identically to the real code: resetting `front` and the counter on LOAD (`resident_invalidate()` already
  clears the counter, and which buffer is called "front" cannot be seen); the explicit memory check, and the same check off by 16
  bytes, and giving the pipeline the whole arena size, each alone (`stream_begin` refuses the same sizes itself, and the explicit
  check refuses them before it; removing both together is caught by the memory-boundary and layout tests); and the default pass
  count of a kernel, which reads the identity entry of the filter table (1 pass).
