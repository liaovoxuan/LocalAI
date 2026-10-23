cmake_minimum_required(VERSION 3.16)

project(VirtualWorld VERSION 1.0 LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)

option(VIRTUALWORLD_BUILD_BUNDLED_QEMU "Build QEMU into VirtualWorld runtime/qemu" OFF)
# Backend sources owned by other workstreams. They are picked up automatically
# once they appear; when they are missing (or fail to provide a usable backend)
# the registry falls back to QEMU hardware acceleration and TCG.
option(VIRTUALWORLD_INCLUDE_CONTRIB_BACKENDS
       "Compile LinuxKvmEngine/HarmonyVirtualizationEngine when their sources exist" ON)

# ---------------------------------------------------------------------------
# HarmonyOS in-process QEMU integration gates (fail-closed by default).
#
# The Harmony backend drives QEMU strictly in-process through QemuInProcessRuntime.
# The REAL core path is only compiled when BOTH of these are ON:
#   * HARMONY_INPROCESS_REAL_CORE        — the static QEMU core is linked in
#   * DEEPSEEK_HOST_SAFETY_GATE_PASSED   — DeepSeek's qemu_init fatal-exit safety
#                                           gate is verified
# Until DeepSeek's fix lands, qemu_init() can still call exit() on a malformed
# config and kill the host process, so the public build MUST stay fail-closed.
# These are intentionally OFF; enabling DEEPSEEK_HOST_SAFETY_GATE_PASSED before
# the fix is unsafe. The Harmony translation unit gates its real-core code on these
# macros, and falls back to a fail-closed stub otherwise (no external process).
# ---------------------------------------------------------------------------
option(HARMONY_INPROCESS_REAL_CORE
       "Link the real in-process QEMU static core into the Harmony backend" OFF)
option(DEEPSEEK_HOST_SAFETY_GATE_PASSED
       "DeepSeek's qemu_init fatal-exit safety gate is verified (DO NOT enable before the fix)" OFF)

set(VIRTUALWORLD_QEMU_SOURCE_DIR "${CMAKE_CURRENT_SOURCE_DIR}/qemu-source" CACHE PATH "QEMU source directory")
set(VIRTUALWORLD_QEMU_TARGET_LIST "x86_64-softmmu,i386-softmmu,aarch64-softmmu,arm-softmmu,ppc-softmmu,ppc64-softmmu,m68k-softmmu,riscv64-softmmu,loongarch64-softmmu" CACHE STRING "Bundled QEMU target list")
set(VIRTUALWORLD_QEMU_BUILD_JOBS "4" CACHE STRING "Parallel jobs for bundled QEMU runtime builds")

find_package(QT NAMES Qt6 Qt5 REQUIRED COMPONENTS Core Widgets Network)
find_package(Qt${QT_VERSION_MAJOR} REQUIRED COMPONENTS Core Widgets Network)
find_package(OpenSSL REQUIRED COMPONENTS Crypto)
find_package(Threads REQUIRED)

# Python3 drives the Mini vMac in-process spawn-API scan (CTest + packaging gate).
find_package(Python3 COMPONENTS Interpreter QUIET)
if(Python3_Interpreter_FOUND)
    set(VW_PYTHON3_EXECUTABLE "${Python3_EXECUTABLE}")
else()
    set(VW_PYTHON3_EXECUTABLE "python3")
endif()

set(VIRTUALWORLD_PLATFORM_SOURCES src/MacMouseDeltaTracker.cpp)
if(APPLE)
    enable_language(OBJCXX)
    set(VIRTUALWORLD_PLATFORM_SOURCES src/MacMouseDeltaTracker.mm)
endif()

# The Mini vMac in-process C++ API is compiled into the product and links the
# in-process engine static archive (engine target + link added further below).
list(APPEND VIRTUALWORLD_PLATFORM_SOURCES
    src/MiniVmacInProcessCore.cpp
    src/MiniVmacInProcessCore.hpp)

# ---------------------------------------------------------------------------
# Virtualization backends
# ---------------------------------------------------------------------------
# Owned by this workstream: the shared contract, the registry and the Windows
# WHPX backend (plain C++ outside _WIN32).
set(VIRTUALWORLD_BACKEND_SOURCES
    src/VirtualizationCapabilities.cpp
    src/VirtualizationCapabilities.hpp
    src/VirtualizationEngine.cpp
    src/VirtualizationEngine.hpp
    src/VirtualizationEngineRegistry.cpp
    src/VirtualizationEngineRegistry.hpp
    src/WindowsHypervisorEngine.cpp
    src/WindowsHypervisorEngine.hpp
)

# src/MacVirtualizationEngine.hpp/.mm is owned by the macOS workstream. It is
# compiled when present; VirtualizationEngine.cpp picks it up through
# __has_include, so a missing or unavailable backend simply leaves QEMU in place.
if(APPLE AND EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/src/MacVirtualizationEngine.hpp"
         AND EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/src/MacVirtualizationEngine.mm")
    enable_language(OBJCXX)
    list(APPEND VIRTUALWORLD_BACKEND_SOURCES
        src/MacVirtualizationEngine.hpp
        src/MacVirtualizationEngine.mm)
    # A compile definition, not just a CMake variable: the shared translation
    # units must compile the adapter and link the Apple frameworks together, or
    # neither.
    set(VIRTUALWORLD_MACOS_BACKEND 1)
endif()

# src/LinuxKvmEngine.hpp/.cpp and src/HarmonyVirtualizationEngine.hpp/.cpp are
# owned by other workstreams. They join the build when they appear; the registry
# keeps working (and falling back to QEMU) when they do not.
set(VIRTUALWORLD_CONTRIB_BACKEND_SOURCES "")
if(VIRTUALWORLD_INCLUDE_CONTRIB_BACKENDS)
    if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/src/LinuxKvmEngine.hpp"
       AND EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/src/LinuxKvmEngine.cpp")
        list(APPEND VIRTUALWORLD_CONTRIB_BACKEND_SOURCES
            src/LinuxKvmEngine.hpp
            src/LinuxKvmEngine.cpp)
        set(VIRTUALWORLD_HAVE_LINUX_KVM_BACKEND 1)
    endif()
    if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/src/HarmonyVirtualizationEngine.hpp"
       AND EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/src/HarmonyVirtualizationEngine.cpp")
        list(APPEND VIRTUALWORLD_CONTRIB_BACKEND_SOURCES
            src/HarmonyVirtualizationEngine.hpp
            src/HarmonyVirtualizationEngine.cpp)
        set(VIRTUALWORLD_HAVE_HARMONY_BACKEND 1)
    endif()
endif()
list(APPEND VIRTUALWORLD_BACKEND_SOURCES ${VIRTUALWORLD_CONTRIB_BACKEND_SOURCES})

# ---------------------------------------------------------------------------
# Backend presence -> compile definitions
# ---------------------------------------------------------------------------
# One source of truth for "which platform backend is in this build". Both the
# application and every backend test target get the same definitions, so the
# shared translation units can never disagree with what is linked.
set(VIRTUALWORLD_BACKEND_DEFINITIONS "")
if(VIRTUALWORLD_MACOS_BACKEND)
    list(APPEND VIRTUALWORLD_BACKEND_DEFINITIONS VIRTUALWORLD_MACOS_BACKEND=1)
endif()
# The WHPX backend is plain C++ on every host and always compiled in; it reports
# `host-unsupported` outside Windows.
list(APPEND VIRTUALWORLD_BACKEND_DEFINITIONS VIRTUALWORLD_HAVE_WINDOWS_BACKEND=1)
if(VIRTUALWORLD_HAVE_LINUX_KVM_BACKEND)
    list(APPEND VIRTUALWORLD_BACKEND_DEFINITIONS VIRTUALWORLD_HAVE_LINUX_KVM_BACKEND=1)
endif()
if(VIRTUALWORLD_HAVE_HARMONY_BACKEND)
    list(APPEND VIRTUALWORLD_BACKEND_DEFINITIONS VIRTUALWORLD_HAVE_HARMONY_BACKEND=1)
endif()

message(STATUS "VirtualWorld backends: macos=${VIRTUALWORLD_MACOS_BACKEND}"
               " windows=1"
               " linux-kvm=${VIRTUALWORLD_HAVE_LINUX_KVM_BACKEND}"
               " harmony=${VIRTUALWORLD_HAVE_HARMONY_BACKEND}")

add_executable(VirtualWorld
    MACOSX_BUNDLE
    assets/VirtualWorld.icns
    src/AppLanguage.cpp
    src/AppLanguage.hpp
    src/main.cpp
    src/ConfigManager.cpp
    src/ConfigManager.hpp
    src/ImageManager.cpp
    src/ImageManager.hpp
    src/ImageDownloadManager.cpp
    src/ImageDownloadManager.hpp
    ${VIRTUALWORLD_PLATFORM_SOURCES}
    src/MacMouseDeltaTracker.hpp
    src/Platform.cpp
    src/Platform.hpp
    src/QemuEngine.cpp
    src/QemuEngine.hpp
    src/QemuParser.cpp
    src/QemuParser.hpp
    src/RecoveryDiagnostics.cpp
    src/RecoveryDiagnostics.hpp
    src/RecoveryGuard.cpp
    src/RecoveryGuard.hpp
    src/SupportTrust.cpp
    src/SupportTrust.hpp
    src/RuntimeManager.cpp
    src/RuntimeManager.hpp
    src/SystemSoftwareEngine.cpp
    src/SystemSoftwareEngine.hpp
    src/SystemSoftwareRomGenerator.cpp
    src/SystemSoftwareRomGenerator.hpp
    src/Translator.cpp
    src/Translator.hpp
    src/UtmParser.cpp
    src/UtmParser.hpp
    ${VIRTUALWORLD_BACKEND_SOURCES}
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
target_compile_definitions(VirtualWorld PRIVATE ${VIRTUALWORLD_BACKEND_DEFINITIONS})
target_link_libraries(VirtualWorld PRIVATE
    Qt${QT_VERSION_MAJOR}::Core
    Qt${QT_VERSION_MAJOR}::Widgets
    Qt${QT_VERSION_MAJOR}::Network
    OpenSSL::Crypto)

if(VIRTUALWORLD_MACOS_BACKEND)
    # The macOS backend talks to Virtualization.framework directly. The sources
    # guard every call with @available(macOS 11.0, *), so linking the framework
    # does not raise the deployment target by itself.
    target_link_libraries(VirtualWorld PRIVATE
        "-framework Virtualization"
        "-framework Foundation"
        "-framework Security")
endif()

# ---------------------------------------------------------------------------
# Mini vMac in-process engine (system-software; NOT a VirtualizationEngine backend)
# ---------------------------------------------------------------------------
# The engine is a portable 68K emulator core + a headless platform layer, all
# running in-process: no QProcess/fork/exec/system/popen/startDetached is ever
# used to launch a child (required on HarmonyOS/OpenHarmony). The C core is built
# as a static archive; the C++ API (src/MiniVmacInProcessCore.*) is compiled into
# the product and links it.
#
# The upstream emulator remains unmodified (GPLv2, COPYING.txt kept).
# The inprocess/ integration supplies compiler configuration, portable host
# services and the scanner; src/MiniVmacInProcessCore.* supplies the C++ API.
# The set of translation units matches third_party/minivmac/inprocess/build_engine.sh
# exactly: 15 upstream core .c, two C glue units and portable C++ host services.
enable_language(C)

set(VW_MINIVMAC_UPSTREAM_SRC "${CMAKE_CURRENT_SOURCE_DIR}/third_party/minivmac/upstream/src")
set(VW_MINIVMAC_INPROCESS_DIR "${CMAKE_CURRENT_SOURCE_DIR}/third_party/minivmac/inprocess")
set(VW_MINIVMAC_INCLUDE
    "${VW_MINIVMAC_INPROCESS_DIR}/cfg"
    "${VW_MINIVMAC_UPSTREAM_SRC}"
    "${VW_MINIVMAC_INPROCESS_DIR}")

# 15 upstream core translation units + the headless platform layer + the
# independent screen-probe TU (reproduces SCRNEMDV's display-page selection).
set(VW_MINIVMAC_CORE_SOURCES
    GLOBGLUE.c M68KITAB.c MINEM68K.c VIAEMDEV.c IWMEMDEV.c SCCEMDEV.c
    RTCEMDEV.c ROMEMDEV.c SCSIEMDV.c SONYEMDV.c SCRNEMDV.c MOUSEMDV.c
    KBRDEMDV.c SNDEMDEV.c PROGMAIN.c)
set(VW_MINIVMAC_SOURCES "")
foreach(_mv_f ${VW_MINIVMAC_CORE_SOURCES})
    list(APPEND VW_MINIVMAC_SOURCES "${VW_MINIVMAC_UPSTREAM_SRC}/${_mv_f}")
endforeach()
list(APPEND VW_MINIVMAC_SOURCES "${VW_MINIVMAC_INPROCESS_DIR}/vw_minivmac_core.c")
list(APPEND VW_MINIVMAC_SOURCES "${VW_MINIVMAC_INPROCESS_DIR}/vw_screen_probe.c")
list(APPEND VW_MINIVMAC_SOURCES "${VW_MINIVMAC_INPROCESS_DIR}/vw_host_platform.cpp")

# Production archive: no test hooks or GUI dependencies. C11 atomics protect
# engine state; standard C++ host services supply timing and synchronization.
add_library(vw_minivmac_inprocess STATIC ${VW_MINIVMAC_SOURCES})
set_target_properties(vw_minivmac_inprocess PROPERTIES C_STANDARD 11 C_STANDARD_REQUIRED ON)
target_include_directories(vw_minivmac_inprocess PRIVATE ${VW_MINIVMAC_INCLUDE})
if(MSVC)
    target_compile_options(vw_minivmac_inprocess PRIVATE /w /utf-8)
    if(CMAKE_C_COMPILER_ID STREQUAL "MSVC")
        target_compile_options(vw_minivmac_inprocess PRIVATE
            $<$<COMPILE_LANGUAGE:C>:/experimental:c11atomics>)
    endif()
else()
    target_compile_options(vw_minivmac_inprocess PRIVATE -w)
endif()
target_compile_definitions(vw_minivmac_inprocess PRIVATE VW_INPROCESS_ENGINE=1)
target_link_libraries(vw_minivmac_inprocess PUBLIC Threads::Threads)

# Test archive: identical engine plus the test-only pre-ProgramMain hold gate.
add_library(vw_minivmac_inprocess_test STATIC ${VW_MINIVMAC_SOURCES})
set_target_properties(vw_minivmac_inprocess_test PROPERTIES C_STANDARD 11 C_STANDARD_REQUIRED ON)
target_include_directories(vw_minivmac_inprocess_test PRIVATE ${VW_MINIVMAC_INCLUDE})
if(MSVC)
    target_compile_options(vw_minivmac_inprocess_test PRIVATE /w /utf-8)
    if(CMAKE_C_COMPILER_ID STREQUAL "MSVC")
        target_compile_options(vw_minivmac_inprocess_test PRIVATE
            $<$<COMPILE_LANGUAGE:C>:/experimental:c11atomics>)
    endif()
else()
    target_compile_options(vw_minivmac_inprocess_test PRIVATE -w)
endif()
target_compile_definitions(vw_minivmac_inprocess_test PRIVATE
    VW_INPROCESS_ENGINE=1 VW_INPROCESS_TEST_HOOKS)
target_link_libraries(vw_minivmac_inprocess_test PUBLIC Threads::Threads)

# The C++ API links the production archive into the product.
target_link_libraries(VirtualWorld PRIVATE vw_minivmac_inprocess Threads::Threads)
target_include_directories(VirtualWorld PRIVATE "${VW_MINIVMAC_INPROCESS_DIR}")

# SINGLE-VARIANT CONSTRAINT (do not relax):
#   The engine exposes ~192 globals per machine variant; plus vs 128k alone
#   conflict on 176 symbols. Static archives do NOT error on duplicate symbols
#   — the linker silently keeps the first definition it sees, so the machine
#   type is decided by link order, not by intent. Therefore exactly ONE variant
#   may be linked into any single executable. Here only the default (plus)
#   variant is compiled in (VW_MINIVMAC_INCLUDE -> cfg/ = Mac Plus). The
#   per-variant archives (build-minivmac-inprocess/libvwminivmac-<variant>.a)
#   are produced only by the `minivmac_engine_standalone` custom target for
#   verification and must NEVER be added to target_link_libraries(VirtualWorld).
#   To ship a different machine, build a dedicated engine archive + a dedicated
#   CMake target; do not co-link multiple variants. See docs/virtualization-release.md.

if(APPLE)
    set_source_files_properties(assets/VirtualWorld.icns PROPERTIES
        MACOSX_PACKAGE_LOCATION "Resources")
    set_target_properties(VirtualWorld PROPERTIES
        MACOSX_BUNDLE TRUE
        MACOSX_BUNDLE_BUNDLE_NAME "VirtualWorld"
        MACOSX_BUNDLE_GUI_IDENTIFIER "com.liaoboxuan.virtualworld"
        MACOSX_BUNDLE_BUNDLE_VERSION "${PROJECT_VERSION}"
        MACOSX_BUNDLE_SHORT_VERSION_STRING "${PROJECT_VERSION}"
        MACOSX_BUNDLE_ICON_FILE "VirtualWorld.icns"
        MACOSX_BUNDLE_COPYRIGHT "Copyright © Liao Boxuan"
    )
endif()

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
set(VIRTUALWORLD_QEMU_BUILD_ENV
    "PATH=/opt/homebrew/bin:/usr/local/bin:$ENV{PATH}"
    "PKG_CONFIG_PATH=/opt/homebrew/lib/pkgconfig:/opt/homebrew/share/pkgconfig:/usr/local/lib/pkgconfig:/usr/local/share/pkgconfig:$ENV{PKG_CONFIG_PATH}"
    "CPPFLAGS=-I/opt/homebrew/include -I/usr/local/include $ENV{CPPFLAGS}"
    "LDFLAGS=-L/opt/homebrew/lib -L/usr/local/lib $ENV{LDFLAGS}"
    "VIRTUALWORLD_RUNTIME_BUILD=1")

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
        COMMAND ${CMAKE_COMMAND} -E env ${VIRTUALWORLD_QEMU_BUILD_ENV}
                ${CMAKE_COMMAND} -E chdir "${VIRTUALWORLD_QEMU_BUILD_DIR}"
                "${VIRTUALWORLD_QEMU_SOURCE_DIR}/configure"
                --target-list=${VIRTUALWORLD_QEMU_TARGET_LIST}
                --prefix=${VIRTUALWORLD_RUNTIME_OUTPUT}
                --disable-docs
                --disable-pvg
                --enable-fdt=system
                --extra-cflags=-I/opt/homebrew/include
                --extra-cflags=-I/usr/local/include
                --extra-ldflags=-L/opt/homebrew/lib
                --extra-ldflags=-L/usr/local/lib
        COMMAND ${CMAKE_COMMAND} -E env ${VIRTUALWORLD_QEMU_BUILD_ENV}
                ${CMAKE_COMMAND} -E chdir "${VIRTUALWORLD_QEMU_BUILD_DIR}"
                ${VIRTUALWORLD_MAKE_PROGRAM} -j${VIRTUALWORLD_QEMU_BUILD_JOBS}
        COMMAND ${CMAKE_COMMAND} -E env ${VIRTUALWORLD_QEMU_BUILD_ENV}
                ${CMAKE_COMMAND} -E chdir "${VIRTUALWORLD_QEMU_BUILD_DIR}"
                ${VIRTUALWORLD_MAKE_PROGRAM} install
        COMMAND ${CMAKE_COMMAND}
                -DRUNTIME_ROOT=${VIRTUALWORLD_RUNTIME_OUTPUT}
                -DQEMU_SOURCE_DIR=${VIRTUALWORLD_QEMU_SOURCE_DIR}
                -P "${CMAKE_CURRENT_SOURCE_DIR}/cmake/BundleQemuRuntime.cmake"
        COMMAND ${CMAKE_COMMAND} -E touch "${VIRTUALWORLD_QEMU_RUNTIME_STAMP}"
        COMMENT "Building bundled QEMU runtime for ${VIRTUALWORLD_RUNTIME_PLATFORM}/${VIRTUALWORLD_RUNTIME_ARCH}"
        VERBATIM)
    add_custom_target(qemu-runtime DEPENDS "${VIRTUALWORLD_QEMU_RUNTIME_STAMP}")
    add_dependencies(VirtualWorld qemu-runtime)
endif()

if(VIRTUALWORLD_BUILD_BUNDLED_QEMU)
    set(VIRTUALWORLD_COPY_RUNTIME_DEPENDS VirtualWorld qemu-runtime)
else()
    set(VIRTUALWORLD_COPY_RUNTIME_DEPENDS VirtualWorld)
endif()

if(APPLE)
    # QEMU includes firmware and data files that must remain resources, not
    # code-signing subcomponents under Contents/MacOS.
    set(VIRTUALWORLD_RUNTIME_BUNDLE_DESTINATION
        "$<TARGET_FILE_DIR:VirtualWorld>/../Resources/runtime")
else()
    set(VIRTUALWORLD_RUNTIME_BUNDLE_DESTINATION
        "$<TARGET_FILE_DIR:VirtualWorld>/runtime")
endif()

add_custom_command(TARGET VirtualWorld POST_BUILD
    COMMAND ${CMAKE_COMMAND}
            -DSOURCE_DIR=${CMAKE_CURRENT_SOURCE_DIR}/runtime
            -DDESTINATION_DIR=${VIRTUALWORLD_RUNTIME_BUNDLE_DESTINATION}
            -P "${CMAKE_CURRENT_SOURCE_DIR}/cmake/CopyRuntime.cmake"
    COMMENT "Copying bundled runtime directory into VirtualWorld")

if(APPLE)
    set(VIRTUALWORLD_SUPPORT_BUNDLE_DESTINATION
        "$<TARGET_FILE_DIR:VirtualWorld>/../Resources/support")
else()
    set(VIRTUALWORLD_SUPPORT_BUNDLE_DESTINATION
        "$<TARGET_FILE_DIR:VirtualWorld>/support")
endif()

add_custom_command(TARGET VirtualWorld POST_BUILD
    COMMAND ${CMAKE_COMMAND}
            -DSOURCE_DIR=${CMAKE_CURRENT_SOURCE_DIR}/assets/support
            -DDESTINATION_DIR=${VIRTUALWORLD_SUPPORT_BUNDLE_DESTINATION}
            -P "${CMAKE_CURRENT_SOURCE_DIR}/cmake/CopyBundleSupport.cmake"
    COMMENT "Copying signed Support Disk resources into VirtualWorld")

if(APPLE)
    add_custom_command(TARGET VirtualWorld POST_BUILD
        COMMAND /usr/bin/xattr -cr "$<TARGET_FILE_DIR:VirtualWorld>/../.."
        COMMAND /usr/bin/codesign --force --deep --sign - "$<TARGET_FILE_DIR:VirtualWorld>/../.."
        COMMENT "Clearing quarantine attributes and ad-hoc signing VirtualWorld.app")
endif()

install(TARGETS VirtualWorld RUNTIME DESTINATION . BUNDLE DESTINATION .)
if(APPLE)
    install(DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/runtime"
            DESTINATION "VirtualWorld.app/Contents/Resources"
            PATTERN ".DS_Store" EXCLUDE
            PATTERN ".*" EXCLUDE
            REGEX "/[^/]+ [23](\\.[^/]*)?$" EXCLUDE)
    install(DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/assets/support/"
            DESTINATION "VirtualWorld.app/Contents/Resources/support"
            PATTERN ".DS_Store" EXCLUDE
            PATTERN ".*" EXCLUDE)
else()
    install(DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/runtime"
            DESTINATION .
            PATTERN ".DS_Store" EXCLUDE
            PATTERN ".*" EXCLUDE
            REGEX "/[^/]+ [23](\\.[^/]*)?$" EXCLUDE)
    install(DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/assets/support/"
            DESTINATION "support"
            PATTERN ".DS_Store" EXCLUDE
            PATTERN ".*" EXCLUDE)
endif()

if(APPLE)
    install(CODE [[
        file(GLOB_RECURSE _vw_runtime_files
             "${CMAKE_INSTALL_PREFIX}/VirtualWorld.app/Contents/Resources/runtime/*")
        foreach(_vw_runtime_file IN LISTS _vw_runtime_files)
            if(EXISTS "${_vw_runtime_file}" AND NOT IS_DIRECTORY "${_vw_runtime_file}")
                if("${_vw_runtime_file}" MATCHES "/bin/[^/]+$")
                    execute_process(COMMAND /bin/chmod 755 "${_vw_runtime_file}")
                endif()
            endif()
        endforeach()
    ]])
    install(CODE [[
        execute_process(
            COMMAND /usr/bin/xattr -cr "${CMAKE_INSTALL_PREFIX}/VirtualWorld.app")
        execute_process(
            COMMAND /usr/bin/codesign --force --deep --sign - "${CMAKE_INSTALL_PREFIX}/VirtualWorld.app"
            RESULT_VARIABLE _vw_codesign_result
            OUTPUT_VARIABLE _vw_codesign_output
            ERROR_VARIABLE _vw_codesign_output)
        if(NOT _vw_codesign_result EQUAL 0)
            message(FATAL_ERROR "Failed to ad-hoc sign VirtualWorld.app after install: ${_vw_codesign_output}")
        endif()
    ]])
endif()

include(CTest)
if(BUILD_TESTING)
    add_executable(VirtualWorldTranslatorTests
        tests/TranslatorTests.cpp
        src/Platform.cpp
        src/Platform.hpp
        src/QemuParser.cpp
        src/QemuParser.hpp
        src/RecoveryGuard.cpp
        src/RecoveryGuard.hpp
        src/SupportTrust.cpp
        src/SupportTrust.hpp
        src/RuntimeManager.cpp
        src/RuntimeManager.hpp
        src/SystemSoftwareEngine.cpp
        src/SystemSoftwareEngine.hpp
        src/SystemSoftwareRomGenerator.cpp
        src/SystemSoftwareRomGenerator.hpp
        src/Translator.cpp
        src/Translator.hpp
        src/MiniVmacInProcessCore.cpp
        src/MiniVmacInProcessCore.hpp
        src/VmConfig.cpp
        src/VmConfig.hpp
    )
    target_include_directories(VirtualWorldTranslatorTests PRIVATE src "${VW_MINIVMAC_INPROCESS_DIR}")
    target_link_libraries(VirtualWorldTranslatorTests PRIVATE
        Qt${QT_VERSION_MAJOR}::Core OpenSSL::Crypto
        vw_minivmac_inprocess Threads::Threads)
    add_test(NAME VirtualWorldTranslatorTests COMMAND VirtualWorldTranslatorTests)

    # -----------------------------------------------------------------------
    # Backend test targets
    # -----------------------------------------------------------------------
    # Every backend test links the same shared plumbing (VmConfig, QEMU
    # detection, translator, the registry and whatever platform backends this
    # build compiled in). Platform suites are added only when their sources are
    # actually in the build:
    #   * the macOS suite needs src/MacVirtualizationEngine.mm and the Apple
    #     frameworks, so it is Apple-only;
    #   * the Harmony suite is pure Qt/C++ logic driven by an injected
    #     environment, so it compiles and runs on any host;
    #   * the Linux KVM and Windows WHPX suites are plain C++ too, and are added
    #     whenever their backend sources are present.
    # No suite is edited here; only the wiring is owned by this file.
    set(VIRTUALWORLD_BACKEND_TEST_SOURCES
        src/Platform.cpp
        src/Platform.hpp
        src/QemuParser.cpp
        src/QemuParser.hpp
        src/RecoveryGuard.cpp
        src/RecoveryGuard.hpp
        src/SupportTrust.cpp
        src/SupportTrust.hpp
        src/RuntimeManager.cpp
        src/RuntimeManager.hpp
        src/SystemSoftwareEngine.cpp
        src/SystemSoftwareEngine.hpp
        src/SystemSoftwareRomGenerator.cpp
        src/SystemSoftwareRomGenerator.hpp
        src/Translator.cpp
        src/Translator.hpp
        src/VmConfig.cpp
        src/VmConfig.hpp
        src/QemuEngine.cpp
        src/QemuEngine.hpp
        src/MiniVmacInProcessCore.cpp
        src/MiniVmacInProcessCore.hpp
        ${VIRTUALWORLD_BACKEND_SOURCES}
    )

    macro(vw_add_backend_test target_name test_source)
        add_executable(${target_name} ${test_source} ${VIRTUALWORLD_BACKEND_TEST_SOURCES})
        target_include_directories(${target_name} PRIVATE src "${VW_MINIVMAC_INPROCESS_DIR}")
        target_compile_definitions(${target_name} PRIVATE ${VIRTUALWORLD_BACKEND_DEFINITIONS})
        target_link_libraries(${target_name} PRIVATE
            Qt${QT_VERSION_MAJOR}::Core OpenSSL::Crypto
            vw_minivmac_inprocess Threads::Threads)
        if(VIRTUALWORLD_MACOS_BACKEND)
            target_link_libraries(${target_name} PRIVATE
                "-framework Virtualization"
                "-framework Foundation"
                "-framework Security")
        endif()
        add_test(NAME ${target_name} COMMAND ${target_name})
        # Backend tests run straight out of the build directory, where there is
        # no bundled runtime/. Prepending the shipped QEMU runtime to PATH
        # reproduces the lookup a packaged app performs (bundle Resources, then
        # PATH) so "QEMU is missing" never becomes a false negative.
        if(EXISTS "${VIRTUALWORLD_RUNTIME_OUTPUT}/bin")
            set_property(TEST ${target_name} PROPERTY
                ENVIRONMENT_MODIFICATION
                "PATH=path_list_prepend:${VIRTUALWORLD_RUNTIME_OUTPUT}/bin")
        endif()
    endmacro()

    # Shared contract + registry + auto-selection. Runs on every host, with or
    # without the platform backends compiled in.
    vw_add_backend_test(VirtualWorldVirtualizationEngineTests
                        tests/VirtualizationEngineTests.cpp)

    # Cross-backend integration: duplicate registration, missing backends,
    # unavailable factories, cross-arch honesty, JSON field stability.
    vw_add_backend_test(VirtualWorldVirtualizationIntegrationTests
                        tests/VirtualizationIntegrationTests.cpp)

    if(VIRTUALWORLD_MACOS_BACKEND)
        vw_add_backend_test(VirtualWorldMacVirtualizationEngineTests
                            tests/MacVirtualizationEngineTests.cpp)
    else()
        message(STATUS "Skipping VirtualWorldMacVirtualizationEngineTests: "
                       "src/MacVirtualizationEngine.hpp/.mm not built here")
    endif()

    if(VIRTUALWORLD_HAVE_HARMONY_BACKEND)
        vw_add_backend_test(VirtualWorldHarmonyVirtualizationEngineTests
                            tests/HarmonyVirtualizationEngineTests.cpp)
    else()
        message(STATUS "Skipping VirtualWorldHarmonyVirtualizationEngineTests: "
                       "Harmony backend not compiled in")
    endif()

    if(VIRTUALWORLD_HAVE_LINUX_KVM_BACKEND)
        vw_add_backend_test(VirtualWorldLinuxKvmEngineTests
                            tests/LinuxKvmEngineTests.cpp)
    else()
        message(STATUS "Skipping VirtualWorldLinuxKvmEngineTests: "
                       "Linux KVM backend not compiled in")
    endif()

    if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/tests/WindowsHypervisorEngineTests.cpp")
        vw_add_backend_test(VirtualWorldWindowsHypervisorEngineTests
                            tests/WindowsHypervisorEngineTests.cpp)
    endif()

    # Host × Guest architecture selection matrix contract tests.
    # Validates cross-ISA rejection, tier honesty, schema V3 fields,
    # selection determinism, and default support-matrix invariants.
    vw_add_backend_test(VirtualWorldVirtualizationArchitectureMatrixTests
                        tests/VirtualizationArchitectureMatrixTests.cpp)

    # --- Mini vMac in-process engine release verifications -------------------
    # Engine self-prove: the engine's own lifecycle/concurrency/containment
    # suite. Links the test archive (with the pre-ProgramMain hold gate). The
    # test reports its REAL checks/failures; it is never forced green.
    add_executable(VirtualWorldMiniVmacInProcessTests
        tests/MiniVmacInProcessCoreTests.cpp
        src/MiniVmacInProcessCore.cpp
        src/MiniVmacInProcessCore.hpp)
    target_include_directories(VirtualWorldMiniVmacInProcessTests PRIVATE
        src "${VW_MINIVMAC_INPROCESS_DIR}")
    target_compile_definitions(VirtualWorldMiniVmacInProcessTests PRIVATE
        VW_INPROCESS_TEST_HOOKS)
    target_link_libraries(VirtualWorldMiniVmacInProcessTests PRIVATE
        vw_minivmac_inprocess_test Threads::Threads)
    add_test(NAME VirtualWorldMiniVmacInProcessTests
             COMMAND VirtualWorldMiniVmacInProcessTests)

    # Spawn-API scan: proves the engine + C++ API never launch a child process.
    # The scan script (read-only) reads the production archive produced by
    # build_engine.sh; minivmac_engine_standalone keeps that artifact fresh.
    if(WIN32)
        # The shell script uses a POSIX compiler. Scan the actual MSVC-built
        # production archive on Windows instead of invoking Git Bash's clang.
        add_custom_target(minivmac_engine_standalone
            DEPENDS vw_minivmac_inprocess vw_minivmac_inprocess_test)
    else()
        add_custom_target(minivmac_engine_standalone ALL
            COMMAND bash "${VW_MINIVMAC_INPROCESS_DIR}/build_engine.sh"
            WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}"
            COMMENT "Building standalone Mini vMac in-process engine artifacts (spawn-API scan)")
    endif()
    find_program(VW_MINIVMAC_NM NAMES llvm-nm nm
                 HINTS "$ENV{ProgramFiles}/LLVM/bin")
    if(NOT VW_MINIVMAC_NM)
        message(FATAL_ERROR "Mini vMac object verification requires llvm-nm or nm")
    endif()
    add_test(NAME VirtualWorldMiniVmacInProcessScan
        COMMAND ${VW_PYTHON3_EXECUTABLE}
                "${VW_MINIVMAC_INPROCESS_DIR}/scan_spawn_apis.py"
                --archive "$<TARGET_FILE:vw_minivmac_inprocess>"
                --nm "${VW_MINIVMAC_NM}"
        WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}")

    # --- Packaging / install release gate -----------------------------------
    # Opt-in (default OFF) so normal development builds, contrib-OFF builds, and
    # "missing platform backend" configurations are completely unaffected. When
    # enabled it adds the VirtualWorldPackagingGate CTest, which inspects the
    # built bundle (app or install tree) and writes a release manifest. See
    # docs/virtualization-release.md for the switch and the skip semantics.
    option(VIRTUALWORLD_ENABLE_PACKAGING_GATE
           "Run the bundle/manifest release gate as a CTest test" OFF)
    if(VIRTUALWORLD_ENABLE_PACKAGING_GATE)
        if(APPLE)
            set(_vw_bundle_root "${CMAKE_BINARY_DIR}/VirtualWorld.app")
        else()
            set(_vw_bundle_root "${CMAKE_BINARY_DIR}")
        endif()

        # Normalise the compiled-in backend booleans for the gate script.
        # Windows (WHPX) is plain C++ compiled into every build, so it is always
        # reported as present; the other three follow their real flags.
        if(VIRTUALWORLD_MACOS_BACKEND)
            set(_vw_b_mac ON)
        else()
            set(_vw_b_mac OFF)
        endif()
        set(_vw_b_windows ON)
        if(VIRTUALWORLD_HAVE_LINUX_KVM_BACKEND)
            set(_vw_b_linux ON)
        else()
            set(_vw_b_linux OFF)
        endif()
        if(VIRTUALWORLD_HAVE_HARMONY_BACKEND)
            set(_vw_b_harmony ON)
        else()
            set(_vw_b_harmony OFF)
        endif()

        add_test(NAME VirtualWorldPackagingGate
            COMMAND ${CMAKE_COMMAND}
                -DPACKAGING_GATE_MODE=verify
                -DBUNDLE_ROOT=${_vw_bundle_root}
                -DPRODUCT_VERSION=${PROJECT_VERSION}
                -DHOST_OS=${CMAKE_HOST_SYSTEM_NAME}
                -DHOST_ARCH=${CMAKE_HOST_SYSTEM_PROCESSOR}
                -DCONTRIB_BACKENDS=${VIRTUALWORLD_INCLUDE_CONTRIB_BACKENDS}
                -DBACKEND_MAC=${_vw_b_mac}
                -DBACKEND_WINDOWS=${_vw_b_windows}
                -DBACKEND_LINUX_KVM=${_vw_b_linux}
                -DBACKEND_HARMONY=${_vw_b_harmony}
                -DMANIFEST_FILE=${CMAKE_BINARY_DIR}/virtualization-release-manifest.json
                -DPYTHON3_EXECUTABLE=${VW_PYTHON3_EXECUTABLE}
                -DSELFPROVE_EXE=${CMAKE_BINARY_DIR}/VirtualWorldMiniVmacInProcessTests
                -DVW_GATE_CMAKE_BINARY_DIR=${CMAKE_BINARY_DIR}
                -DVW_GATE_CMAKE_COMMAND=${CMAKE_COMMAND}
                -P "${CMAKE_CURRENT_SOURCE_DIR}/tests/VirtualizationPackagingTests.cmake")
    endif()
endif()
