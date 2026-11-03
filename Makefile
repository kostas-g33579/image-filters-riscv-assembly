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

all: $(BIN)

$(BIN): src/filter.s | $(BUILD_DIR)
	$(AS) -o $(BUILD_DIR)/filter.o src/filter.s
	$(LD) -o $(BIN) $(BUILD_DIR)/filter.o

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
	$(QEMU) $(BIN) $(FILTER) $(PASSES)
	@echo "wrote $(IMAGES_DIR)/output.pgm"

debug: $(BIN) $(IMAGES_DIR)/input.pgm
	@$(QEMU) -g 1234 $(BIN) & \
	QEMU_PID=$$!; \
	sleep 0.3; \
	$(GDB) -q -ex "target remote localhost:1234" -ex "break _start" $(BIN); \
	kill $$QEMU_PID 2>/dev/null; \
	true

# Run every filter on images/input.pgm -> images/output_<filter>.pgm
FILTERS = identity blur gaussian sharpen edge emboss sobelx sobely
demo: $(BIN) $(IMAGES_DIR)/input.pgm
	@for f in $(FILTERS); do \
		$(QEMU) $(BIN) $$f && mv $(IMAGES_DIR)/output.pgm $(IMAGES_DIR)/output_$$f.pgm \
		&& echo "wrote $(IMAGES_DIR)/output_$$f.pgm"; \
	done

test: $(BIN)
	$(PYTHON) tests/run_tests.py

clean:
	rm -rf $(BUILD_DIR) $(IMAGES_DIR)/output.pgm $(IMAGES_DIR)/output_*.pgm

.PHONY: all image run debug demo test clean
