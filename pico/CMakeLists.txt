cmake_minimum_required(VERSION 3.13)

# RISC-V cores of the RP2350 (Hazard3), not the Cortex-M33 pair. Works on both
# the Pico 2 and the Pico 2 W (nothing here touches the wireless chip).
set(PICO_PLATFORM rp2350-riscv CACHE STRING "")
set(PICO_BOARD pico2_w CACHE STRING "")
# The SDK looks for riscv32-unknown-elf-gcc etc. by default; Arch's package is
# called riscv64-elf-gcc (it has the rv32 multilibs).
set(PICO_GCC_TRIPLE riscv64-elf CACHE STRING "")

include(pico_sdk_import.cmake)
project(image_filters C CXX ASM)
pico_sdk_init()

add_executable(image_filters
    src/main.c
    src/proto.c
    src/stream.c
    ../src/kernel.s          # the assembly filter core, unchanged
)

# Assemble the kernel as RV32 with Zbb min/max (the RP2350's Hazard3 has Zbb).
set_source_files_properties(../src/kernel.s PROPERTIES COMPILE_OPTIONS
    "-Wa,--defsym,XLEN=32;-Wa,--defsym,ZBB=1;-Wa,-I,${CMAKE_CURRENT_SOURCE_DIR}/../src")

target_include_directories(image_filters PRIVATE src)
# A reader that is slow for a moment must not make the device drop output.
# TinyUSB's CDC FIFOs are 4096 bytes instead of 64: while the core filters a row the host can
# already send the next one, and the core does not wait while a result row drains. Measured on
# the chip (512x512, 4 passes): 1.56-1.66 s -> 0.83 s round trip; 240x240: 0.26 s -> 0.20 s.
# Costs 8 KB of RAM. (docs/streaming.md)
target_compile_definitions(image_filters PRIVATE PICO_STDIO_USB_STDOUT_TIMEOUT_US=5000000
    CFG_TUD_CDC_RX_BUFSIZE=4096 CFG_TUD_CDC_TX_BUFSIZE=4096)
target_link_libraries(image_filters pico_stdlib)

pico_enable_stdio_usb(image_filters 1)
pico_enable_stdio_uart(image_filters 0)
pico_add_extra_outputs(image_filters)

# ---- USB throughput benchmark (make bench-pico); not part of the filter firmware ----
# Each variant changes one thing so its effect can be measured alone.
function(bench_variant name)
    add_executable(${name} bench/bench_main.c bench/bench.c)
    target_include_directories(${name} PRIVATE bench)
    target_link_libraries(${name} pico_stdlib)
    pico_enable_stdio_usb(${name} 1)
    pico_enable_stdio_uart(${name} 0)
    pico_add_extra_outputs(${name})
endfunction()

bench_variant(usb_bench_stdio)
bench_variant(usb_bench_fifo)
target_compile_definitions(usb_bench_fifo PRIVATE BENCH_FIFO=4096
    CFG_TUD_CDC_RX_BUFSIZE=4096 CFG_TUD_CDC_TX_BUFSIZE=4096)
bench_variant(usb_bench_raw)
target_compile_definitions(usb_bench_raw PRIVATE BENCH_RAW=1 BENCH_FIFO=4096
    CFG_TUD_CDC_RX_BUFSIZE=4096 CFG_TUD_CDC_TX_BUFSIZE=4096)

# ---- On-chip probe for the anonymiser modules (dicom.c, geom.c, nifti.c, sidecar.c): runs the whole pipeline on a built-in
# synthetic series and prints the results with cycle counts; pico/tools/anon_probe_check.py compares them with the emulator.
add_executable(anon_probe
    probe/anon_probe.c
    src/dicom.c
    src/geom.c
    src/nifti.c
    src/sidecar.c
)
target_include_directories(anon_probe PRIVATE src probe)
target_link_libraries(anon_probe pico_stdlib)
pico_enable_stdio_usb(anon_probe 1)
pico_enable_stdio_uart(anon_probe 0)
pico_add_extra_outputs(anon_probe)
