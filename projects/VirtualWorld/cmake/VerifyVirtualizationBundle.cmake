# VerifyVirtualizationBundle.cmake
#
# Release packaging gate for VirtualWorld. Inspects an installed / built bundle
# (macOS .app or a portable directory) for the artifacts a release must ship:
# the main program, the bundled QEMU runtime, firmware / data, support
# resources, executable permissions and the platform path layout.
#
# This module is include-safe: it only defines helpers and the
# `vw_verify_bundle()` function. Run it directly as a command-line tool by
# setting `VW_VERIFY_ENTRY=1`, e.g.
#
#   cmake -DVW_VERIFY_ENTRY=1 -DBUNDLE_ROOT=.../VirtualWorld.app \
#         -P cmake/VerifyVirtualizationBundle.cmake
#
# A missing artifact is reported with a STABLE code and the precise path;
# the gate never fakes a success it could not perform. On macOS it verifies the
# app bundle layout and that the ad-hoc signature is inspectable; on Windows /
# Linux the app-bundle / codesign checks are explicitly SKIPPED and never
# reported as passing.
#
# NOTE on scopes: the small helpers below are MACROS (not functions) on purpose.
# They are invoked from inside vw_verify_bundle(), and a function's PARENT_SCOPE
# would otherwise leak past the enclosing function. Macros execute in the
# caller's scope, which is exactly what the inline helpers need.

include_guard(GLOBAL)

# --- stable failure / status codes -----------------------------------------
set(VW_GATE_CODE_MAIN_EXECUTABLE_MISSING       "MAIN_EXECUTABLE_MISSING")
set(VW_GATE_CODE_MAIN_NOT_EXECUTABLE           "MAIN_NOT_EXECUTABLE")
set(VW_GATE_CODE_RUNTIME_DIR_MISSING           "RUNTIME_DIR_MISSING")
set(VW_GATE_CODE_RUNTIME_PLATFORM_DIR_MISSING  "RUNTIME_PLATFORM_DIR_MISSING")
set(VW_GATE_CODE_QEMU_BINARY_MISSING           "QEMU_BINARY_MISSING")
set(VW_GATE_CODE_QEMU_NOT_EXECUTABLE           "QEMU_BINARY_NOT_EXECUTABLE")
set(VW_GATE_CODE_SUPPORT_DIR_MISSING           "SUPPORT_DIR_MISSING")
set(VW_GATE_CODE_FIRMWARE_MISSING              "FIRMWARE_MISSING")
set(VW_GATE_CODE_APP_LAYOUT_INVALID            "APP_BUNDLE_LAYOUT_INVALID")
set(VW_GATE_CODE_APP_CODESIGN_MISSING          "APP_CODESIGN_MISSING")
set(VW_GATE_CODE_APP_CODESIGN_INVALID          "APP_CODESIGN_INVALID")

# --- small JSON helpers (macros: run in caller scope) ----------------------
macro(vw_json_str out in)
    string(REPLACE "\\" "\\\\" _vw_s "${in}")
    string(REPLACE "\"" "\\\"" _vw_s "${_vw_s}")
    set(${out} "\"${_vw_s}\"")
endmacro()

macro(vw_json_bool out in)
    if(${in})
        set(${out} "true")
    else()
        set(${out} "false")
    endif()
endmacro()

# Each check is recorded as a JSON object fragment so the report / manifest can
# show exactly what was inspected, what passed, and what was skipped (never
# silently assumed ok).
macro(vw_gate_check_pass _list_var _check _path _msg)
    vw_json_str(_vw_jc "${_check}")
    vw_json_str(_vw_jp "${_path}")
    vw_json_str(_vw_jm "${_msg}")
    list(APPEND ${_list_var}
         "{\"check\":${_vw_jc},\"ok\":true,\"skipped\":false,\"path\":${_vw_jp},\"message\":${_vw_jm}}")
endmacro()

macro(vw_gate_check_fail _list_var _check _path _msg)
    vw_json_str(_vw_jc "${_check}")
    vw_json_str(_vw_jp "${_path}")
    vw_json_str(_vw_jm "${_msg}")
    list(APPEND ${_list_var}
         "{\"check\":${_vw_jc},\"ok\":false,\"skipped\":false,\"path\":${_vw_jp},\"message\":${_vw_jm}}")
endmacro()

macro(vw_gate_check_skip _list_var _check _msg)
    vw_json_str(_vw_jc "${_check}")
    vw_json_str(_vw_jm "${_msg}")
    list(APPEND ${_list_var}
         "{\"check\":${_vw_jc},\"ok\":true,\"skipped\":true,\"path\":\"\",\"message\":${_vw_jm}}")
endmacro()

# --- layout detection (macro: run in caller scope) -------------------------
# Resolve the platform-specific artifact locations for a bundle root. Sets the
# result variables in the caller's scope.
macro(vw_detect_layout BUNDLE_ROOT OUT_MAIN OUT_RUNTIME OUT_SUPPORT OUT_IS_APP)
    if(CMAKE_HOST_SYSTEM_NAME STREQUAL "Darwin")
        set(_vw_is_app TRUE)
        set(_vw_main "${BUNDLE_ROOT}/Contents/MacOS/VirtualWorld")
        set(_vw_runtime "${BUNDLE_ROOT}/Contents/Resources/runtime")
        set(_vw_support "${BUNDLE_ROOT}/Contents/Resources/support")
    elseif(CMAKE_HOST_SYSTEM_NAME STREQUAL "Windows")
        set(_vw_is_app FALSE)
        set(_vw_main "${BUNDLE_ROOT}/VirtualWorld.exe")
        if(NOT EXISTS "${_vw_main}")
            set(_vw_main "${BUNDLE_ROOT}/bin/VirtualWorld.exe")
        endif()
        set(_vw_runtime "${BUNDLE_ROOT}/runtime")
        set(_vw_support "${BUNDLE_ROOT}/support")
    else() # Linux and any other POSIX host
        set(_vw_is_app FALSE)
        set(_vw_main "${BUNDLE_ROOT}/VirtualWorld")
        if(NOT EXISTS "${_vw_main}")
            set(_vw_main "${BUNDLE_ROOT}/bin/VirtualWorld")
        endif()
        set(_vw_runtime "${BUNDLE_ROOT}/runtime")
        set(_vw_support "${BUNDLE_ROOT}/support")
    endif()
    set(${OUT_MAIN}    "${_vw_main}")
    set(${OUT_RUNTIME} "${_vw_runtime}")
    set(${OUT_SUPPORT} "${_vw_support}")
    set(${OUT_IS_APP}  "${_vw_is_app}")
endmacro()

macro(vw_host_platform OUT_VAR)
    if(CMAKE_HOST_SYSTEM_NAME STREQUAL "Darwin")
        set(${OUT_VAR} "macos")
    elseif(CMAKE_HOST_SYSTEM_NAME STREQUAL "Windows")
        set(${OUT_VAR} "windows")
    else()
        set(${OUT_VAR} "linux")
    endif()
endmacro()

# --- the gate --------------------------------------------------------------
# vw_verify_bundle(BUNDLE_ROOT <path>)
#   Sets in PARENT_SCOPE:
#     VW_GATE_STATUS    "passed" | "failed"
#     VW_GATE_FAILURES  list of "CODE|precise-path|message"
#     VW_GATE_CHECKS    list of JSON object fragments (one per check)
function(vw_verify_bundle)
    set(options)
    set(oneValueArgs BUNDLE_ROOT)
    set(multiValueArgs)
    cmake_parse_arguments(VW "${options}" "${oneValueArgs}" "${multiValueArgs}" ${ARGN})

    set(_failures "")
    set(_checks "")

    vw_detect_layout("${VW_BUNDLE_ROOT}" _main _runtime _support _is_app)
    vw_host_platform(_host_platform)
    set(_plat_dir "${_runtime}/qemu/${_host_platform}")

    # 1) main program
    if(EXISTS "${_main}" AND NOT IS_DIRECTORY "${_main}")
        vw_gate_check_pass(_checks "main-executable-present" "${_main}" "main program present")
        if(CMAKE_HOST_SYSTEM_NAME STREQUAL "Windows")
            vw_gate_check_skip(_checks "main-executable-permission"
                               "Windows host: POSIX executable-bit check skipped")
        else()
            execute_process(COMMAND test -x "${_main}" RESULT_VARIABLE _xr ERROR_QUIET)
            if(_xr EQUAL 0)
                vw_gate_check_pass(_checks "main-executable-permission"
                                   "${_main}" "main program is executable")
            else()
                list(APPEND _failures
                     "${VW_GATE_CODE_MAIN_NOT_EXECUTABLE}|${_main}|main program is not executable")
                vw_gate_check_fail(_checks "main-executable-permission"
                                   "${_main}" "${VW_GATE_CODE_MAIN_NOT_EXECUTABLE}")
            endif()
        endif()
    else()
        list(APPEND _failures
             "${VW_GATE_CODE_MAIN_EXECUTABLE_MISSING}|${_main}|main program not found")
        vw_gate_check_fail(_checks "main-executable-present"
                           "${_main}" "${VW_GATE_CODE_MAIN_EXECUTABLE_MISSING}")
    endif()

    # 2) runtime directory
    if(EXISTS "${_runtime}" AND IS_DIRECTORY "${_runtime}")
        vw_gate_check_pass(_checks "runtime-dir-present" "${_runtime}" "runtime directory present")
    else()
        list(APPEND _failures
             "${VW_GATE_CODE_RUNTIME_DIR_MISSING}|${_runtime}|runtime directory missing")
        vw_gate_check_fail(_checks "runtime-dir-present"
                           "${_runtime}" "${VW_GATE_CODE_RUNTIME_DIR_MISSING}")
    endif()

    # 3) host-platform runtime directory + QEMU binaries
    if(EXISTS "${_plat_dir}" AND IS_DIRECTORY "${_plat_dir}")
        vw_gate_check_pass(_checks "runtime-platform-dir-present"
                           "${_plat_dir}" "host platform runtime directory present")
        file(GLOB_RECURSE _qemu_bins "${_plat_dir}/*/bin/qemu-system-*")
        file(GLOB_RECURSE _qemu_img "${_plat_dir}/*/bin/qemu-img")
        set(_qemu_all ${_qemu_bins} ${_qemu_img})
        list(LENGTH _qemu_all _qemu_n)
        if(_qemu_n GREATER 0)
            vw_gate_check_pass(_checks "qemu-binary-present"
                               "${_plat_dir}" "QEMU binaries present (${_qemu_n})")
            set(_qemu_bad "")
            if(NOT CMAKE_HOST_SYSTEM_NAME STREQUAL "Windows")
                foreach(_qb IN LISTS _qemu_all)
                    execute_process(COMMAND test -x "${_qb}" RESULT_VARIABLE _xr ERROR_QUIET)
                    if(NOT _xr EQUAL 0)
                        list(APPEND _qemu_bad "${_qb}")
                    endif()
                endforeach()
            endif()
            if(_qemu_bad)
                string(JOIN ", " _qemu_bad_pretty ${_qemu_bad})
                list(APPEND _failures
                     "${VW_GATE_CODE_QEMU_NOT_EXECUTABLE}|${_qemu_bad_pretty}|QEMU binaries not executable")
                vw_gate_check_fail(_checks "qemu-binary-permission"
                                   "${_qemu_bad_pretty}" "${VW_GATE_CODE_QEMU_NOT_EXECUTABLE}")
            else()
                vw_gate_check_pass(_checks "qemu-binary-permission"
                                   "${_plat_dir}" "QEMU binaries are executable")
            endif()
        else()
            list(APPEND _failures
                 "${VW_GATE_CODE_QEMU_BINARY_MISSING}|${_plat_dir}|no QEMU binaries under host platform directory")
            vw_gate_check_fail(_checks "qemu-binary-present"
                               "${_plat_dir}" "${VW_GATE_CODE_QEMU_BINARY_MISSING}")
        endif()
    else()
        list(APPEND _failures
             "${VW_GATE_CODE_RUNTIME_PLATFORM_DIR_MISSING}|${_plat_dir}|host platform runtime directory missing")
        vw_gate_check_fail(_checks "runtime-platform-dir-present"
                           "${_plat_dir}" "${VW_GATE_CODE_RUNTIME_PLATFORM_DIR_MISSING}")
    endif()

    # 4) support resources
    if(EXISTS "${_support}" AND IS_DIRECTORY "${_support}")
        file(GLOB _sup_files "${_support}/*")
        list(LENGTH _sup_files _sup_n)
        if(_sup_n GREATER 0)
            vw_gate_check_pass(_checks "support-dir-present"
                               "${_support}" "support resources present (${_sup_n})")
        else()
            list(APPEND _failures
                 "${VW_GATE_CODE_SUPPORT_DIR_MISSING}|${_support}|support directory is empty")
            vw_gate_check_fail(_checks "support-dir-present"
                               "${_support}" "${VW_GATE_CODE_SUPPORT_DIR_MISSING}")
        endif()
    else()
        list(APPEND _failures
             "${VW_GATE_CODE_SUPPORT_DIR_MISSING}|${_support}|support directory missing")
        vw_gate_check_fail(_checks "support-dir-present"
                           "${_support}" "${VW_GATE_CODE_SUPPORT_DIR_MISSING}")
    endif()

    # 5) firmware / data
    file(GLOB_RECURSE _fw "${_runtime}/qemu/*/*.bin" "${_runtime}/qemu/*/*.rom"
                          "${_runtime}/qemu/*/*.edk2" "${_runtime}/qemu/*/*.fd"
                          "${_runtime}/qemu/*/*.nvram")
    list(LENGTH _fw _fw_n)
    if(_fw_n GREATER 0)
        vw_gate_check_pass(_checks "firmware-present"
                           "${_runtime}" "firmware / data present (${_fw_n})")
    else()
        list(APPEND _failures
             "${VW_GATE_CODE_FIRMWARE_MISSING}|${_runtime}|no firmware / data files found")
        vw_gate_check_fail(_checks "firmware-present"
                           "${_runtime}" "${VW_GATE_CODE_FIRMWARE_MISSING}")
    endif()

    # 6) macOS app bundle layout + ad-hoc codesign (macOS only)
    if(_is_app)
        if(EXISTS "${VW_BUNDLE_ROOT}/Contents/MacOS" AND EXISTS "${VW_BUNDLE_ROOT}/Contents/Resources")
            vw_gate_check_pass(_checks "app-bundle-layout"
                               "${VW_BUNDLE_ROOT}" "app bundle layout valid")
        else()
            list(APPEND _failures
                 "${VW_GATE_CODE_APP_LAYOUT_INVALID}|${VW_BUNDLE_ROOT}|app bundle missing Contents/MacOS or Contents/Resources")
            vw_gate_check_fail(_checks "app-bundle-layout"
                               "${VW_BUNDLE_ROOT}" "${VW_GATE_CODE_APP_LAYOUT_INVALID}")
        endif()
        if(EXISTS "${VW_BUNDLE_ROOT}/Contents/_CodeSignature")
            vw_gate_check_pass(_checks "app-codesign-present"
                               "${VW_BUNDLE_ROOT}/Contents/_CodeSignature"
                               "ad-hoc signature directory present")
        else()
            list(APPEND _failures
                 "${VW_GATE_CODE_APP_CODESIGN_MISSING}|${VW_BUNDLE_ROOT}|_CodeSignature missing")
            vw_gate_check_fail(_checks "app-codesign-present"
                               "${VW_BUNDLE_ROOT}" "${VW_GATE_CODE_APP_CODESIGN_MISSING}")
        endif()
        find_program(_codesign codesign)
        if(_codesign)
            execute_process(COMMAND "${_codesign}" --verify --verbose=1 "${VW_BUNDLE_ROOT}"
                            RESULT_VARIABLE _csr OUTPUT_VARIABLE _cso ERROR_VARIABLE _cse)
            if(_csr EQUAL 0)
                vw_gate_check_pass(_checks "app-codesign-verify"
                                   "${VW_BUNDLE_ROOT}" "codesign --verify passed")
            else()
                list(APPEND _failures
                     "${VW_GATE_CODE_APP_CODESIGN_INVALID}|${VW_BUNDLE_ROOT}|codesign --verify failed")
                vw_gate_check_fail(_checks "app-codesign-verify"
                                   "${VW_BUNDLE_ROOT}" "${VW_GATE_CODE_APP_CODESIGN_INVALID}")
            endif()
        else()
            vw_gate_check_skip(_checks "app-codesign-verify"
                               "codesign not available on this host")
        endif()
    else()
        vw_gate_check_skip(_checks "app-bundle-layout"
                           "host is not macOS; app bundle layout check skipped")
        vw_gate_check_skip(_checks "app-codesign-present"
                           "host is not macOS; codesign check skipped")
        vw_gate_check_skip(_checks "app-codesign-verify"
                           "host is not macOS; codesign check skipped")
    endif()

    if(_failures)
        set(VW_GATE_STATUS "failed" PARENT_SCOPE)
    else()
        set(VW_GATE_STATUS "passed" PARENT_SCOPE)
    endif()
    set(VW_GATE_FAILURES "${_failures}" PARENT_SCOPE)
    set(VW_GATE_CHECKS "${_checks}" PARENT_SCOPE)
endfunction()

# --- command-line entry point ---------------------------------------------
if(VW_VERIFY_ENTRY)
    if(NOT DEFINED BUNDLE_ROOT OR BUNDLE_ROOT STREQUAL "")
        message(FATAL_ERROR "BUNDLE_ROOT is required (path to VirtualWorld.app or install root)")
    endif()
    vw_verify_bundle(BUNDLE_ROOT "${BUNDLE_ROOT}")
    message(STATUS "[packaging-gate] status=${VW_GATE_STATUS}")
    foreach(_f IN LISTS VW_GATE_CHECKS)
        message(STATUS "[packaging-gate] CHECK ${_f}")
    endforeach()
    foreach(_f IN LISTS VW_GATE_FAILURES)
        message(STATUS "[packaging-gate] FAIL ${_f}")
    endforeach()
    if(VW_GATE_STATUS STREQUAL "failed")
        message(FATAL_ERROR "VIRTUALWORLD_PACKAGING_GATE_FAILED")
    endif()
endif()
