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

$(PROTO32): pico/src/proto.c pico/src/proto.h pico/test/proto_linux.c $(BIN32)
	$(CC32) -Ipico/src -o $@ pico/src/proto.c pico/test/proto_linux.c $(BUILD32)/kernel.o

test-proto: $(PROTO32)
	$(PYTHON) tests/test_proto.py

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

.PHONY: all image phantom run debug demo test rv32 rv32zbb test32 test32zbb testall pico test-proto clean
