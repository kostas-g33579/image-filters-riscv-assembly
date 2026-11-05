# Streaming: filtering an image one row at a time

The whole-image mode keeps two copies of the image in the chip's 520 KB of RAM, which
caps it at 90,000 pixels (about 300 x 300). A 3x3 filter does not need the whole image,
though: output row `j` depends only on input rows `j-1`, `j` and `j+1`. Streaming uses
that. The image goes through the chip row by row and the chip never holds more than a
few rows of each pass, so the height is unlimited and memory grows only with the
**width** and the **number of passes**.

## The pipeline

Each filter pass is a *stage* with a small ring of 3 row buffers. A chain of passes is a
chain of stages: a stage's output row is written straight into the next stage's ring.

```
host  -->  [ring 0: 3 rows] --filter_row--> [ring 1: 3 rows] --filter_row--> ... --> output row --> host
 rows         pass 1                           pass 2                                  (windowed if asked)
```

* A stage produces output row `j` as soon as it holds rows `j-1`, `j`, `j+1`, so each
  stage runs one row behind the one before it. A chain of 4 passes is 4 rows behind.
* **Top edge:** row 0 has no row above, so it uses itself, as in the whole-image
  filter. **Bottom edge:** when the last input row has arrived, the last two output rows
  (`H-2`, `H-1`) are produced together, the last using itself as the row below
  ("flush"). Every pass replicates its own edges, so this equals running the passes
  one after another on whole images.
* **Three buffers are enough:** when row `j+2` arrives it overwrites the slot of row
  `j-1`, which output row `j` has just finished with.
* **Order matters.** If a stage could push two rows into the next stage's ring before
  that stage had consumed one, it would overwrite a row still needed. The pipeline is
  therefore processed *depth first*: after every row it produces, it moves to the next
  stage and works downstream until nothing more can be produced, and only then goes back
  up. It is a loop, not recursion, so the chip's small stack is not a concern.

## Memory

Each stage needs `3 * width * 2` bytes, plus one output row:

    bytes = (passes * 3 + 1) * width * 2        (+ width bytes with a window)

The device has a 420,000-byte working area (shared with the whole-image buffers, which
are never needed at the same time), so for example 2048 wide allows 34 passes and 4096
wide 17. The device answers with status 9 if a request does not fit, before any pixel
is sent. Height has no effect on memory.

## The kernel's part

The assembly gained a row-level entry point, `filter_row(dst, above, cur, below, W,
record, maxval)`, that does one pass on one row. It reuses the very code of the
whole-image functions (assembler macros expanded in both places): the whole-image
machine code is byte-for-byte unchanged, which was checked by assembling the old and the
new kernel and comparing. The price per row is the setup of the weights and the
reciprocal, a few dozen instructions against ~11,000 cycles of work for a 240-sample row
(about 1%).

## Why the host sends and receives at the same time

The device sends each finished row back while it is still receiving later rows. If the
host first sent the whole image and only then read, both sides would end up blocked
writing into a full buffer that the other is not emptying (a pipe in the tests, the USB
buffer on the board). The client therefore runs a sender thread. When the device reports
an error part-way, it reads and discards the rows still in flight so the next request
starts on a message boundary, so the client always sends all `H` rows.

## Verified how

`tests/test_stream.py` compares the streamed result byte for byte with the whole-image
harness (itself checked against an independent reference) for all filters and bit
depths, every width and height from 1 to 9, up to 255 passes, windows, 4096-wide, 16384
wide, 20000 rows tall, 1024 x 1024 and the memory limit to the byte. Twenty-two
deliberate bugs in the pipeline, the protocol and the row assembly were injected to
check that the tests notice (19 were caught at once; two real gaps at the exact memory
and height boundaries were then closed with new tests; one, an unclamped top row, is
harmless because row -1 wraps to the slot that holds row 0).

## Measured on the chip

All results byte-identical to the Linux harness (`--verify`), Pico 2 W, counters read
around `filter_row` only.

| run | cycles | instructions | IPC | round trip |
|---|---:|---:|---:|---:|
| 240x240 sharpen, whole image (for comparison) | 2,718,135 | 2,538,090 | 0.93 | 0.67 s |
| 240x240 sharpen, streamed | 3,188,407 | 2,565,724 | 0.80 | **0.38 s** |
| 512x512 gaussian x4, streamed (over the whole-image limit) | 50,549,382 | 48,028,966 | 0.95 | 2.64 s |

Two things to read correctly:

* **While streaming, the cycle counter includes USB interrupt time.** The core is
  receiving and sending rows while it filters, so interrupt handlers (and the flash-cache
  misses they cause) run inside the measured window. It costs about **1,900 cycles and
  ~56 instructions per input row**, the same in both runs above (1,880 and 1,859), so it
  shows up as +17% on a one-pass 240-wide image and only +2% on a four-pass 512-wide one.
  It is a real cost of doing both jobs on one core, not the filter getting slower: the
  filter's own cost is the whole-image number (47 cycles/pixel). For pure filter cost,
  use whole-image mode.
* **Streaming is faster in wall-clock time even though it uses more core cycles,**
  because the upload and the download overlap (USB is full duplex): 0.67 s -> 0.38 s for
  the same slice, 261 -> 460 KB/s effective. The 512x512 run moved 790 KB in 2.64 s
  (299 KB/s); the core was busy for only 13% of that, so USB is the bottleneck.
