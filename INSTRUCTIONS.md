# Instructions

```sh
cd ~/projects/riscv-image-blur

make run                        # build + run blur (8 passes) on images/input.pgm
make run FILTER=sharpen         # choose a filter
make run FILTER=blur PASSES=3   # override pass count (0..255)
make run FILTER=identity WINDOW="1064 400"   # window/level -> 8-bit output
make phantom                    # make a 512x512 16-bit CT-like test image
make demo                       # run every filter -> images/output_<filter>.pgm
make test                       # regression tests (SKIP_BIG=1 skips the 16M-pixel cases)
make pico                       # firmware for the Pico 2's RISC-V cores -> build-pico/image_filters.uf2
make test-proto                 # the device protocol, tested under qemu-riscv32 (see pico/README.md)
make test-stream                # streaming mode (row by row, any height), byte-identical to whole-image
make test-nifti                 # the NIfTI slice tool (scripts/nifti_slice.py)
make test-dicom                 # the DICOM slice tool (scripts/dicom_slice.py, docs/dicom.md)
make test-dicom-c test-geom-c test-nifti-c test-sidecar-c   # the anonymiser's C modules against their Python twins
make test-anon-c                # the anonymiser protocol under qemu-riscv32 (two builds)
make test-anonymize test-batch  # the host client and its batch mode
make test-fetch                 # the TCIA download script's archive checks (no network)
make test-bench                 # the USB throughput benchmark logic (pico/bench/), no hardware
make rv32 && make test32        # RV32 build (the Pico 2's architecture) under qemu-riscv32
make rv32zbb && make test32zbb # RV32 + Zbb min/max (what the RP2350's Hazard3 supports)
make testall                    # RV64, RV32 and RV32+Zbb
python3 scripts/estimate_cycles.py build32/filter blur   # estimated Hazard3 cycles/pixel
make debug                      # step through in gdb (breaks at _start)
make clean

xdg-open images/input.pgm
xdg-open images/output.pgm
```

Filters: identity, blur, gaussian, sharpen, edge, emboss, softemboss, sobel, sobelx, sobely
(see docs/filters.md for what each one does, docs/window_level.md for windowing).

`images/input.pgm` is generated (256x256 checkerboard) only if missing.
To use your own image (8-bit or 16-bit PGM, up to 16,777,216 pixels):

```sh
convert downloaded.jpg -resize 256x256 -colorspace Gray -depth 8 images/input.pgm
make run FILTER=edge
```

The DICOM to NIfTI anonymiser (on the Pico; flash `build-pico/image_filters.uf2` first, see pico/README.md and docs/dicom.md):

```sh
sudo python3 pico/tools/pico_anonymize.py data/dicom/SERIES_DIR --subject P001 --sequence T1c
#   -> output/anonymized/P001_T1c/P001_T1c.nii and .json (never overwritten without --overwrite)
sudo python3 pico/tools/pico_anonymize.py --batch data/dicom --out output/batch --report output/batch.csv
#   every sub-folder named <subject>_<sequence>; finished cases are skipped, so an interrupted batch just restarts
python3 pico/tools/pico_anonymize.py SERIES_DIR --subject P001 --sequence T1c --exec "qemu-riscv32 build32/anon_test"
#   the same, with the device side run under the emulator (no board needed; make test-anon-c builds it)
python3 -I scripts/fetch_tcia.py plan --border > plan.csv && python3 -I scripts/fetch_tcia.py get plan.csv
#   download public TCIA series into data/fetched/ (each archive is checked first)
```

Manual steps (no Makefile; two source files, `-I src` finds `records.inc`):

```sh
python3 scripts/make_test_image.py images/input.pgm
riscv64-linux-gnu-as -I src -o build/filter.o src/filter.s
riscv64-linux-gnu-as -I src -o build/kernel.o src/kernel.s
riscv64-linux-gnu-ld -o build/filter build/filter.o build/kernel.o
qemu-riscv64 build/filter sharpen
qemu-riscv64 build/filter identity window 1064 400
```
