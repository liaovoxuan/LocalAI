cmake_minimum_required(VERSION 3.16)

project(VirtualWorld VERSION 1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)

option(VIRTUALWORLD_BUILD_BUNDLED_QEMU "Build qemu-master into VirtualWorld runtime/qemu" OFF)
set(VIRTUALWORLD_QEMU_SOURCE_DIR "${CMAKE_CURRENT_SOURCE_DIR}/qemu-master" CACHE PATH "QEMU source directory")
set(VIRTUALWORLD_QEMU_TARGET_LIST "x86_64-softmmu,aarch64-softmmu,arm-softmmu,i386-softmmu,ppc-softmmu,ppc64-softmmu,riscv64-softmmu" CACHE STRING "Bundled QEMU target list")
set(VIRTUALWORLD_QEMU_BUILD_JOBS "4" CACHE STRING "Parallel jobs for bundled QEMU runtime builds")

find_package(QT NAMES Qt6 Qt5 REQUIRED COMPONENTS Core Widgets Network)
find_package(Qt${QT_VERSION_MAJOR} REQUIRED COMPONENTS Core Widgets Network)

add_executable(VirtualWorld
    src/main.cpp
    src/ConfigManager.cpp
    src/ConfigManager.hpp
    src/ImageManager.cpp
    src/ImageManager.hpp
    src/ImageDownloadManager.cpp
    src/ImageDownloadManager.hpp
    src/Platform.cpp
    src/Platform.hpp
    src/QemuEngine.cpp
    src/QemuEngine.hpp
    src/QemuParser.cpp
    src/QemuParser.hpp
    src/RuntimeManager.cpp
    src/RuntimeManager.hpp
    src/Translator.cpp
    src/Translator.hpp
    src/UtmParser.cpp
    src/UtmParser.hpp
    src/VirtualMachineWindow.cpp
    src/VirtualMachineWindow.hpp
    src/VirtualWorldWindow.cpp
    src/VirtualWorldWindow.hpp
    src/VMManager.cpp
    src/VMManager.hpp
    src/VmConfig.cpp
    src/VmConfig.hpp
)

target_include_directories(VirtualWorld PRIVATE src)
target_link_libraries(VirtualWorld PRIVATE Qt${QT_VERSION_MAJOR}::Core Qt${QT_VERSION_MAJOR}::Widgets Qt${QT_VERSION_MAJOR}::Network)

if(APPLE)
    set(VIRTUALWORLD_RUNTIME_PLATFORM "macos")
    if(CMAKE_SYSTEM_PROCESSOR MATCHES "arm64|aarch64")
        set(VIRTUALWORLD_RUNTIME_ARCH "apple-silicon")
    else()
        set(VIRTUALWORLD_RUNTIME_ARCH "x86_64")
    endif()
elseif(WIN32)
    set(VIRTUALWORLD_RUNTIME_PLATFORM "windows")
    if(CMAKE_SYSTEM_PROCESSOR MATCHES "ARM64|aarch64")
        set(VIRTUALWORLD_RUNTIME_ARCH "aarch64")
    else()
        set(VIRTUALWORLD_RUNTIME_ARCH "x86_64")
    endif()
else()
    set(VIRTUALWORLD_RUNTIME_PLATFORM "linux")
    if(CMAKE_SYSTEM_PROCESSOR MATCHES "riscv64")
        set(VIRTUALWORLD_RUNTIME_ARCH "riscv64")
    elseif(CMAKE_SYSTEM_PROCESSOR MATCHES "arm64|aarch64")
        set(VIRTUALWORLD_RUNTIME_ARCH "aarch64")
    else()
        set(VIRTUALWORLD_RUNTIME_ARCH "x86_64")
    endif()
endif()

set(VIRTUALWORLD_RUNTIME_OUTPUT
    "${CMAKE_CURRENT_SOURCE_DIR}/runtime/qemu/${VIRTUALWORLD_RUNTIME_PLATFORM}/${VIRTUALWORLD_RUNTIME_ARCH}")
set(VIRTUALWORLD_QEMU_BUILD_DIR
    "${CMAKE_BINARY_DIR}/qemu-build-${VIRTUALWORLD_RUNTIME_PLATFORM}-${VIRTUALWORLD_RUNTIME_ARCH}")

add_custom_command(TARGET VirtualWorld POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_directory
            "${CMAKE_CURRENT_SOURCE_DIR}/runtime"
            "$<TARGET_FILE_DIR:VirtualWorld>/runtime"
    COMMENT "Copying bundled runtime directory next to VirtualWorld")

install(TARGETS VirtualWorld RUNTIME DESTINATION . BUNDLE DESTINATION .)
install(DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/runtime" DESTINATION .)

if(VIRTUALWORLD_BUILD_BUNDLED_QEMU)
    find_program(VIRTUALWORLD_MAKE_PROGRAM NAMES gmake make)
    if(NOT VIRTUALWORLD_MAKE_PROGRAM)
        message(FATAL_ERROR "Building bundled QEMU requires make or gmake.")
    endif()
    if(NOT EXISTS "${VIRTUALWORLD_QEMU_SOURCE_DIR}/configure")
        message(FATAL_ERROR "QEMU source not found: ${VIRTUALWORLD_QEMU_SOURCE_DIR}")
    endif()

    set(VIRTUALWORLD_QEMU_RUNTIME_STAMP "${VIRTUALWORLD_QEMU_BUILD_DIR}/.virtualworld-qemu-runtime.stamp")
    add_custom_command(
        OUTPUT "${VIRTUALWORLD_QEMU_RUNTIME_STAMP}"
        COMMAND ${CMAKE_COMMAND} -E make_directory "${VIRTUALWORLD_QEMU_BUILD_DIR}"
        COMMAND "${VIRTUALWORLD_QEMU_SOURCE_DIR}/configure"
                --target-list=${VIRTUALWORLD_QEMU_TARGET_LIST}
                --prefix=${VIRTUALWORLD_RUNTIME_OUTPUT}
                --disable-docs
        COMMAND ${VIRTUALWORLD_MAKE_PROGRAM} -j${VIRTUALWORLD_QEMU_BUILD_JOBS}
        COMMAND ${VIRTUALWORLD_MAKE_PROGRAM} install
        COMMAND ${CMAKE_COMMAND} -E touch "${VIRTUALWORLD_QEMU_RUNTIME_STAMP}"
        WORKING_DIRECTORY "${VIRTUALWORLD_QEMU_BUILD_DIR}"
        COMMENT "Building bundled QEMU runtime for ${VIRTUALWORLD_RUNTIME_PLATFORM}/${VIRTUALWORLD_RUNTIME_ARCH}"
        VERBATIM)
    add_custom_target(qemu-runtime DEPENDS "${VIRTUALWORLD_QEMU_RUNTIME_STAMP}")
endif()
