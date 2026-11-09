AS     = riscv64-linux-gnu-as
LD     = riscv64-linux-gnu-ld
QEMU   = qemu-riscv64
GDB    = riscv64-linux-gnu-gdb
PYTHON = python3

BUILD_DIR  = build
IMAGES_DIR = images
BIN        = $(BUILD_DIR)/filter
FILTER    ?= blur
PASSES    ?=
WINDOW    ?=

all: $(BIN)

SRCS = src/filter.s src/kernel.s src/records.inc src/xlen.inc
OBJS = $(BUILD_DIR)/filter.o $(BUILD_DIR)/kernel.o

$(BIN): $(SRCS) | $(BUILD_DIR)
	$(AS) -I src -o $(BUILD_DIR)/filter.o src/filter.s
	$(AS) -I src -o $(BUILD_DIR)/kernel.o src/kernel.s
	$(LD) -o $(BIN) $(OBJS)

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

# Only generates the checkerboard if input.pgm doesn't exist yet, so a
# user-supplied image is never overwritten by `make run`.
$(IMAGES_DIR)/input.pgm: | $(IMAGES_DIR)
	$(PYTHON) scripts/make_test_image.py $@

# 512x512 16-bit CT-like phantom -> images/phantom16.pgm (copy it to
# input.pgm to try window/level: make run FILTER=identity WINDOW="1064 400").
phantom: | $(IMAGES_DIR)
	$(PYTHON) scripts/make_phantom16.py $(IMAGES_DIR)/phantom16.pgm

# Force-regenerate the checkerboard (overwrites input.pgm).
image: | $(IMAGES_DIR)
	$(PYTHON) scripts/make_test_image.py $(IMAGES_DIR)/input.pgm

$(IMAGES_DIR):
	mkdir -p $(IMAGES_DIR)

run: $(BIN) $(IMAGES_DIR)/input.pgm
	$(QEMU) $(BIN) $(FILTER) $(PASSES) $(if $(WINDOW),window $(WINDOW))
	@echo "wrote $(IMAGES_DIR)/output.pgm"

debug: $(BIN) $(IMAGES_DIR)/input.pgm
	@$(QEMU) -g 1234 $(BIN) & \
	QEMU_PID=$$!; \
	sleep 0.3; \
	$(GDB) -q -ex "target remote localhost:1234" -ex "break _start" $(BIN); \
	kill $$QEMU_PID 2>/dev/null; \
	true

# ---- RV32 build: the Pico 2's Hazard3 cores are RV32IMAC. Same sources,
# assembled with XLEN=32; runs under qemu-riscv32 for testing.
BUILD32 = build32
BIN32   = $(BUILD32)/filter
AS32    = $(AS) -march=rv32im -mabi=ilp32 --defsym XLEN=32 -I src
LD32    = $(LD) -m elf32lriscv

rv32: $(BIN32)

$(BIN32): $(SRCS) | $(BUILD32)
	$(AS32) -o $(BUILD32)/filter.o src/filter.s
	$(AS32) -o $(BUILD32)/kernel.o src/kernel.s
	$(LD32) -o $(BIN32) $(BUILD32)/filter.o $(BUILD32)/kernel.o

$(BUILD32):
	mkdir -p $(BUILD32)

# RV32 + Zbb (min/max for clamping), as on the RP2350's Hazard3. Needs a
# QEMU CPU with Zbb enabled.
BUILD32Z = build32zbb
BIN32Z   = $(BUILD32Z)/filter
AS32Z    = $(AS) -march=rv32im_zbb -mabi=ilp32 --defsym XLEN=32 --defsym ZBB=1 -I src

rv32zbb: $(BIN32Z)

$(BIN32Z): $(SRCS) | $(BUILD32Z)
	$(AS32Z) -o $(BUILD32Z)/filter.o src/filter.s
	$(AS32Z) -o $(BUILD32Z)/kernel.o src/kernel.s
	$(LD32) -o $(BIN32Z) $(BUILD32Z)/filter.o $(BUILD32Z)/kernel.o

$(BUILD32Z):
	mkdir -p $(BUILD32Z)

test32zbb: $(BIN32Z)
	FILTER_BIN=$(BIN32Z) QEMU="qemu-riscv32 -cpu rv32,zbb=true" $(PYTHON) tests/run_tests.py

# ---- Pico firmware (RP2350 RISC-V cores) and its protocol layer ----
PICO_BUILD = build-pico
PICO_SDK  ?= /usr/share/pico-sdk

# Needs the Pico SDK (pacman/AUR: pico-sdk) and cmake + ninja.
pico:
	PICO_SDK_PATH=$(PICO_SDK) cmake -S pico -B $(PICO_BUILD) -G Ninja
	PICO_SDK_PATH=$(PICO_SDK) cmake --build $(PICO_BUILD)
	@echo "firmware: $(PICO_BUILD)/image_filters.uf2"

# The device protocol + assembly kernel as a qemu-riscv32 Linux program.
CC32 = riscv64-elf-gcc -march=rv32im -mabi=ilp32 -O2 -ffreestanding -static -nostartfiles -Wl,--no-relax
PROTO32 = $(BUILD32)/proto_test

$(PROTO32): pico/src/proto.c pico/src/proto.h pico/src/stream.c pico/src/stream.h pico/test/proto_linux.c $(BIN32)
	$(CC32) -Ipico/src -o $@ pico/src/proto.c pico/src/stream.c pico/test/proto_linux.c $(BUILD32)/kernel.o

# The allow-list DICOM reader in C (pico/src/dicom.c) as a qemu-riscv32 program, and its differential
# test: the C and Python readers get the same files and must give identical results.
DICOM32 = $(BUILD32)/dicom_test

$(DICOM32): pico/src/dicom.c pico/src/dicom.h pico/test/dicom_linux.c
	@mkdir -p $(BUILD32)
	$(CC32) -Wall -Wextra -Ipico/src -o $@ pico/src/dicom.c pico/test/dicom_linux.c

test-dicom-c: $(DICOM32)
	$(PYTHON) -I tests/test_dicom_c.py

# Stack geometry in C (pico/src/geom.c): slice order, spacing and the voxel-to-world matrix, checked against
# the Python Stack to the last bit and against the DICOM definition.
GEOM32 = $(BUILD32)/geom_test

$(GEOM32): pico/src/geom.c pico/src/geom.h pico/src/dicom.c pico/src/dicom.h pico/test/geom_linux.c
	@mkdir -p $(BUILD32)
	$(CC32) -Wall -Wextra -Ipico/src -o $@ pico/src/dicom.c pico/src/geom.c pico/test/geom_linux.c

test-geom-c: $(GEOM32)
	$(PYTHON) -I tests/test_geom_c.py

# The NIfTI side of the on-device anonymiser (pico/src/nifti.c): the 352-byte header, the voxel re-packing, the refusals.
NIFTIC32 = $(BUILD32)/nifti_test

$(NIFTIC32): pico/src/nifti.c pico/src/nifti.h pico/src/geom.c pico/src/geom.h pico/src/dicom.c pico/src/dicom.h pico/test/nifti_linux.c
	@mkdir -p $(BUILD32)
	$(CC32) -Wall -Wextra -Ipico/src -o $@ pico/src/dicom.c pico/src/geom.c pico/src/nifti.c pico/test/nifti_linux.c

test-nifti-c: $(NIFTIC32)
	$(PYTHON) -I tests/test_nifti_c.py

# The JSON sidecar of the anonymiser (pico/src/sidecar.c): values, units, range checks, discrepancies, audit.
SIDECAR32 = $(BUILD32)/sidecar_test

$(SIDECAR32): pico/src/sidecar.c pico/src/sidecar.h pico/src/nifti.c pico/src/nifti.h pico/src/geom.c pico/src/geom.h pico/src/dicom.c pico/src/dicom.h pico/test/sidecar_linux.c
	@mkdir -p $(BUILD32)
	$(CC32) -Wall -Wextra -Ipico/src -o $@ pico/src/dicom.c pico/src/geom.c pico/src/nifti.c pico/src/sidecar.c pico/test/sidecar_linux.c

test-sidecar-c: $(SIDECAR32)
	$(PYTHON) -I tests/test_sidecar_c.py

# The anonymiser protocol (pico/src/anon.c): DICOM files in, voxels + NIfTI header + JSON out, streamed.
# anon_test has the real sizes; anon_test_tiny has a 1023-byte head buffer, 64-byte chunks and 1..13-byte reads.
ANON_SRC = pico/src/anon.c pico/src/anon.h pico/src/proto.h pico/src/sidecar.c pico/src/sidecar.h pico/src/nifti.c pico/src/nifti.h pico/src/geom.c pico/src/geom.h pico/src/dicom.c pico/src/dicom.h pico/test/anon_linux.c
ANON32 = $(BUILD32)/anon_test
ANON32T = $(BUILD32)/anon_test_tiny
ANON_FILES = pico/src/dicom.c pico/src/geom.c pico/src/nifti.c pico/src/sidecar.c pico/src/anon.c pico/test/anon_linux.c

$(ANON32): $(ANON_SRC)
	@mkdir -p $(BUILD32)
	$(CC32) -Wall -Wextra -Ipico/src -o $@ $(ANON_FILES)

$(ANON32T): $(ANON_SRC)
	@mkdir -p $(BUILD32)
	$(CC32) -Wall -Wextra -Ipico/src -DANON_HDR_MAX=1023u -DANON_CHUNK=64u -DSLOW_READS -o $@ $(ANON_FILES)

test-anon-c: $(ANON32) $(ANON32T) $(NIFTIC32) $(SIDECAR32)
	$(PYTHON) -I tests/test_anon_c.py

# The on-chip probe of the anonymiser modules: builds the firmware (build-pico/anon_probe.uf2) and the emulator
# harnesses it is compared with. Flash it, then:  sudo python3 pico/tools/anon_probe_check.py
anon-probe: $(NIFTIC32) $(SIDECAR32)
	$(MAKE) pico
	@echo "flash build-pico/anon_probe.uf2, then: sudo python3 pico/tools/anon_probe_check.py"

# DICOM slice tool (scripts/dicom_slice.py): synthetic files from an independent writer, hostile
# files, a privacy check, and the real series in data/dicom/ when present.
test-dicom:
	$(PYTHON) -I tests/test_dicom.py

# USB benchmark logic as a qemu-riscv32 program (no hardware), and the firmware variants.
BENCH32 = $(BUILD32)/bench_test

$(BENCH32): pico/bench/bench.c pico/bench/bench.h pico/test/bench_linux.c
	@mkdir -p $(BUILD32)
	$(CC32) -Ipico/bench -o $@ pico/bench/bench.c pico/test/bench_linux.c

test-bench: $(BENCH32)
	$(PYTHON) tests/test_bench.py

# Firmware: build-pico/usb_bench_{stdio,fifo,raw}.uf2 (same build dir as the filter firmware)
bench-pico: pico
	@ls -1 $(PICO_BUILD)/usb_bench_*.uf2

# NIfTI slice tool (scripts/nifti_slice.py) against synthetic volumes with known contents.
test-nifti:
	$(PYTHON) tests/test_nifti.py

test-proto: $(PROTO32)
	$(PYTHON) tests/test_proto.py

# Streaming mode (row by row) against the whole-image harness, byte for byte.
test-stream: $(PROTO32)
	$(PYTHON) tests/test_stream.py

test32: $(BIN32)
	FILTER_BIN=$(BIN32) QEMU=qemu-riscv32 $(PYTHON) tests/run_tests.py

# Both widths.
testall: test test32 test32zbb

# Run every filter on images/input.pgm -> images/output_<filter>.pgm
FILTERS = identity blur gaussian sharpen edge emboss softemboss sobel sobelx sobely
demo: $(BIN) $(IMAGES_DIR)/input.pgm
	@for f in $(FILTERS); do \
		$(QEMU) $(BIN) $$f && mv $(IMAGES_DIR)/output.pgm $(IMAGES_DIR)/output_$$f.pgm \
		&& echo "wrote $(IMAGES_DIR)/output_$$f.pgm"; \
	done

test: $(BIN)
	$(PYTHON) tests/run_tests.py

clean:
	rm -rf $(BUILD_DIR) $(BUILD32) $(BUILD32Z) $(PICO_BUILD) $(IMAGES_DIR)/output.pgm $(IMAGES_DIR)/output_*.pgm

.PHONY: test-dicom test-dicom-c test-geom-c test-nifti-c test-sidecar-c test-anon-c anon-probe all image phantom run debug demo test rv32 rv32zbb test32 test32zbb testall pico test-proto test-stream test-bench bench-pico test-nifti clean
