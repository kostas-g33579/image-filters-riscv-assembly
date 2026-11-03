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
    ../src/kernel.s          # the assembly filter core, unchanged
)

# Assemble the kernel as RV32 with Zbb min/max (the RP2350's Hazard3 has Zbb).
set_source_files_properties(../src/kernel.s PROPERTIES COMPILE_OPTIONS
    "-Wa,--defsym,XLEN=32;-Wa,--defsym,ZBB=1;-Wa,-I,${CMAKE_CURRENT_SOURCE_DIR}/../src")

target_include_directories(image_filters PRIVATE src)
# A reader that is slow for a moment must not make the device drop output.
target_compile_definitions(image_filters PRIVATE PICO_STDIO_USB_STDOUT_TIMEOUT_US=5000000)
target_link_libraries(image_filters pico_stdlib)

pico_enable_stdio_usb(image_filters 1)
pico_enable_stdio_uart(image_filters 0)
pico_add_extra_outputs(image_filters)
