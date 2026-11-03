AS     = riscv64-linux-gnu-as
LD     = riscv64-linux-gnu-ld
QEMU   = qemu-riscv64
GDB    = riscv64-linux-gnu-gdb
PYTHON = python3

BUILD_DIR  = build
IMAGES_DIR = images
BIN        = $(BUILD_DIR)/blur

all: $(BIN)

$(BIN): src/blur.s | $(BUILD_DIR)
	$(AS) -o $(BUILD_DIR)/blur.o src/blur.s
	$(LD) -o $(BIN) $(BUILD_DIR)/blur.o

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

# Only generates the checkerboard if input.pgm doesn't exist yet, so a
# user-supplied image is never overwritten by `make run`.
$(IMAGES_DIR)/input.pgm: | $(IMAGES_DIR)
	$(PYTHON) scripts/make_test_image.py $@

# Force-regenerate the checkerboard (overwrites input.pgm).
image: | $(IMAGES_DIR)
	$(PYTHON) scripts/make_test_image.py $(IMAGES_DIR)/input.pgm

$(IMAGES_DIR):
	mkdir -p $(IMAGES_DIR)

run: $(BIN) $(IMAGES_DIR)/input.pgm
	$(QEMU) $(BIN)
	@echo "wrote $(IMAGES_DIR)/output.pgm"

debug: $(BIN) $(IMAGES_DIR)/input.pgm
	@$(QEMU) -g 1234 $(BIN) & \
	QEMU_PID=$$!; \
	sleep 0.3; \
	$(GDB) -q -ex "target remote localhost:1234" -ex "break _start" $(BIN); \
	kill $$QEMU_PID 2>/dev/null; \
	true

test: $(BIN)
	$(PYTHON) tests/run_tests.py

clean:
	rm -rf $(BUILD_DIR) $(IMAGES_DIR)/output.pgm

.PHONY: all image run debug test clean
