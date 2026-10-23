# GenerateVirtualizationManifest.cmake
#
# Produces a machine-readable release manifest
# (virtualization-release-manifest.json) describing a built / installed
# VirtualWorld bundle: product version, the host OS / arch the manifest was
# generated on, which backends were compiled in, the contrib toggle, a SHA-256
# of the main program, a SHA-256 manifest of every bundled QEMU binary, the
# firmware / support resource counts, and a gate result.
#
# Honesty rule: the manifest NEVER claims real-hardware verification. It records
# `verified: false` and `real_hardware_verified: false`, and the embedded `gate`
# block only describes package-integrity checks actually performed on this host.
#
# Include-safe: defines `vw_scan_bundle()` and `vw_write_manifest()`. Run it
# directly with `-DVW_MANIFEST_ENTRY=1`.

include_guard(GLOBAL)
include("${CMAKE_CURRENT_LIST_DIR}/VerifyVirtualizationBundle.cmake")

set(VW_MANIFEST_SCHEMA "virtualworld.release/1")

# vw_scan_bundle(<BUNDLE_ROOT>)
#   Sets in PARENT_SCOPE (only artifacts that actually exist are filled):
#     VW_MAIN_EXECUTABLE  VW_MAIN_SHA256  VW_MAIN_SIZE
#     VW_RUNTIME_DIR
#     VW_QEMU_BINARIES (rel paths)  VW_QEMU_SHA256 (aligned)  VW_QEMU_SIZES (aligned)
#     VW_FIRMWARE (rel paths)  VW_SUPPORT (rel paths)  VW_SUPPORT_COUNT
function(vw_scan_bundle BUNDLE_ROOT)
    vw_detect_layout("${BUNDLE_ROOT}" _main _runtime _support _is_app)

    set(VW_MAIN_EXECUTABLE "${_main}" PARENT_SCOPE)
    set(VW_RUNTIME_DIR "${_runtime}" PARENT_SCOPE)

    if(EXISTS "${_main}" AND NOT IS_DIRECTORY "${_main}")
        file(SHA256 "${_main}" _sha)
        file(SIZE "${_main}" _size)
    else()
        set(_sha "")
        set(_size 0)
    endif()
    set(VW_MAIN_SHA256 "${_sha}" PARENT_SCOPE)
    set(VW_MAIN_SIZE "${_size}" PARENT_SCOPE)

    vw_host_platform(_host_platform)
    set(_plat_dir "${_runtime}/qemu/${_host_platform}")
    set(_qemu_bins "")
    set(_qemu_shas "")
    set(_qemu_sizes "")
    if(EXISTS "${_plat_dir}")
        file(GLOB_RECURSE _found "${_plat_dir}/*/bin/qemu-system-*")
        file(GLOB_RECURSE _img "${_plat_dir}/*/bin/qemu-img")
        set(_all ${_found} ${_img})
        foreach(_q IN LISTS _all)
            file(RELATIVE_PATH _rel "${BUNDLE_ROOT}" "${_q}")
            file(SHA256 "${_q}" _qsha)
            file(SIZE "${_q}" _qsize)
            list(APPEND _qemu_bins "${_rel}")
            list(APPEND _qemu_shas "${_qsha}")
            list(APPEND _qemu_sizes "${_qsize}")
        endforeach()
    endif()
    set(VW_QEMU_BINARIES "${_qemu_bins}" PARENT_SCOPE)
    set(VW_QEMU_SHA256 "${_qemu_shas}" PARENT_SCOPE)
    set(VW_QEMU_SIZES "${_qemu_sizes}" PARENT_SCOPE)

    file(GLOB_RECURSE _fw "${_runtime}/qemu/*/*.bin" "${_runtime}/qemu/*/*.rom"
                          "${_runtime}/qemu/*/*.edk2" "${_runtime}/qemu/*/*.fd"
                          "${_runtime}/qemu/*/*.nvram")
    set(_fw_rel "")
    foreach(_f IN LISTS _fw)
        file(RELATIVE_PATH _rel "${BUNDLE_ROOT}" "${_f}")
        list(APPEND _fw_rel "${_rel}")
    endforeach()
    set(VW_FIRMWARE "${_fw_rel}" PARENT_SCOPE)

    set(_sup_rel "")
    if(EXISTS "${_support}")
        file(GLOB_RECURSE _sup "${_support}/*")
        foreach(_s IN LISTS _sup)
            if(NOT IS_DIRECTORY "${_s}")
                file(RELATIVE_PATH _rel "${BUNDLE_ROOT}" "${_s}")
                list(APPEND _sup_rel "${_rel}")
            endif()
        endforeach()
    endif()
    set(VW_SUPPORT "${_sup_rel}" PARENT_SCOPE)
    list(LENGTH _sup_rel _sup_n)
    set(VW_SUPPORT_COUNT "${_sup_n}" PARENT_SCOPE)
endfunction()

# vw_write_manifest(OUTPUT_FILE <path> PRODUCT_VERSION <ver> HOST_OS <os>
#                   HOST_ARCH <arch> CONTRIB_BACKENDS <bool>
#                   BACKENDS_MAC <bool> BACKENDS_WINDOWS <bool>
#                   BACKENDS_LINUX_KVM <bool> BACKENDS_HARMONY <bool>
#                   QEMU_BINARIES <list> QEMU_SHA256 <list> QEMU_SIZES <list>
#                   FIRMWARE <list> SUPPORT <list>
#                   [GATE_RESULT_JSON <json>])
#   Composes and writes the manifest JSON.
function(vw_write_manifest)
    set(options)
    set(oneValueArgs OUTPUT_FILE PRODUCT_VERSION HOST_OS HOST_ARCH CONTRIB_BACKENDS
                      BACKENDS_MAC BACKENDS_WINDOWS BACKENDS_LINUX_KVM BACKENDS_HARMONY
                      GATE_RESULT_JSON SCHEMA
                      MINIVMAC_SELFPROVE_CHECKS MINIVMAC_SELFPROVE_FAILURES
                      MINIVMAC_SELFPROVE_PASS MINIVMAC_SELFPROVE_RUNS
                      MINIVMAC_SPAWN_SCAN_CLEAN
                      MINIVMAC_PRODUCT_SCAN_CLEAN MINIVMAC_PRODUCT_SCAN_ALLOWED
                      MINIVMAC_PRODUCT_SCAN_VIOLATIONS MINIVMAC_PRODUCT_SCAN_VIOLATIONS_JSON
                      MINIVMAC_BRANCH_SCAN_CLEAN
                      MINIVMAC_REBUILT)
    set(multiValueArgs QEMU_BINARIES QEMU_SHA256 QEMU_SIZES FIRMWARE SUPPORT)
    cmake_parse_arguments(VW "${options}" "${oneValueArgs}" "${multiValueArgs}" ${ARGN})

    if(NOT DEFINED VW_SCHEMA OR VW_SCHEMA STREQUAL "")
        set(VW_SCHEMA "${VW_MANIFEST_SCHEMA}")
    endif()
    if(NOT DEFINED VW_PRODUCT_VERSION OR VW_PRODUCT_VERSION STREQUAL "")
        set(VW_PRODUCT_VERSION "0.0.0")
    endif()
    if(NOT DEFINED VW_HOST_OS OR VW_HOST_OS STREQUAL "")
        set(VW_HOST_OS "${CMAKE_HOST_SYSTEM_NAME}")
    endif()
    if(NOT DEFINED VW_HOST_ARCH OR VW_HOST_ARCH STREQUAL "")
        set(VW_HOST_ARCH "${CMAKE_HOST_SYSTEM_PROCESSOR}")
    endif()
    if(NOT DEFINED VW_CONTRIB_BACKENDS OR VW_CONTRIB_BACKENDS STREQUAL "")
        set(VW_CONTRIB_BACKENDS "OFF")
    endif()

    vw_json_str(_schema "${VW_SCHEMA}")
    vw_json_str(_version "${VW_PRODUCT_VERSION}")
    vw_json_str(_host_os "${VW_HOST_OS}")
    vw_json_str(_host_arch "${VW_HOST_ARCH}")
    string(TIMESTAMP _generated "%Y-%m-%dT%H:%M:%SZ" UTC)
    vw_json_str(_generated "${_generated}")
    vw_json_bool(_contrib "${VW_CONTRIB_BACKENDS}")
    vw_json_bool(_bm "${VW_BACKENDS_MAC}")
    vw_json_bool(_bw "${VW_BACKENDS_WINDOWS}")
    vw_json_bool(_bl "${VW_BACKENDS_LINUX_KVM}")
    vw_json_bool(_bh "${VW_BACKENDS_HARMONY}")

    # Mini vMac in-process engine verifications (real results, never declared).
    vw_json_bool(_mv_sp_pass "${VW_MINIVMAC_SELFPROVE_PASS}")
    vw_json_bool(_mv_scan "${VW_MINIVMAC_SPAWN_SCAN_CLEAN}")
    vw_json_bool(_mv_prod_clean "${VW_MINIVMAC_PRODUCT_SCAN_CLEAN}")
    if(NOT DEFINED VW_MINIVMAC_SELFPROVE_CHECKS OR VW_MINIVMAC_SELFPROVE_CHECKS STREQUAL "")
        set(VW_MINIVMAC_SELFPROVE_CHECKS 0)
    endif()
    if(NOT DEFINED VW_MINIVMAC_SELFPROVE_FAILURES OR VW_MINIVMAC_SELFPROVE_FAILURES STREQUAL "")
        set(VW_MINIVMAC_SELFPROVE_FAILURES -1)
    endif()
    if(NOT DEFINED VW_MINIVMAC_SELFPROVE_RUNS OR VW_MINIVMAC_SELFPROVE_RUNS STREQUAL "")
        set(VW_MINIVMAC_SELFPROVE_RUNS 1)
    endif()
    if(NOT DEFINED VW_MINIVMAC_PRODUCT_SCAN_ALLOWED OR VW_MINIVMAC_PRODUCT_SCAN_ALLOWED STREQUAL "")
        set(VW_MINIVMAC_PRODUCT_SCAN_ALLOWED 0)
    endif()
    if(NOT DEFINED VW_MINIVMAC_PRODUCT_SCAN_VIOLATIONS OR VW_MINIVMAC_PRODUCT_SCAN_VIOLATIONS STREQUAL "")
        set(VW_MINIVMAC_PRODUCT_SCAN_VIOLATIONS 0)
    endif()
    if(NOT DEFINED VW_MINIVMAC_PRODUCT_SCAN_VIOLATIONS_JSON OR VW_MINIVMAC_PRODUCT_SCAN_VIOLATIONS_JSON STREQUAL "")
        set(VW_MINIVMAC_PRODUCT_SCAN_VIOLATIONS_JSON "[]")
    endif()
    if(NOT DEFINED VW_MINIVMAC_REBUILT OR VW_MINIVMAC_REBUILT STREQUAL "")
        set(VW_MINIVMAC_REBUILT FALSE)
    endif()
    if(NOT DEFINED VW_MINIVMAC_BRANCH_SCAN_CLEAN OR VW_MINIVMAC_BRANCH_SCAN_CLEAN STREQUAL "")
        set(VW_MINIVMAC_BRANCH_SCAN_CLEAN FALSE)
    endif()
    vw_json_bool(_mv_rebuilt "${VW_MINIVMAC_REBUILT}")
    vw_json_bool(_mv_branch_clean "${VW_MINIVMAC_BRANCH_SCAN_CLEAN}")

    vw_json_str(_main_path "${VW_MAIN_EXECUTABLE}")
    vw_json_str(_main_sha "${VW_MAIN_SHA256}")

    # QEMU binary entries.
    set(_qemu_arr "")
    list(LENGTH VW_QEMU_BINARIES _qemu_n)
    if(_qemu_n GREATER 0)
        math(EXPR _qemu_last "${_qemu_n} - 1")
        foreach(_i RANGE ${_qemu_last})
            list(GET VW_QEMU_BINARIES ${_i} _qp)
            list(GET VW_QEMU_SHA256 ${_i} _qs)
            list(GET VW_QEMU_SIZES ${_i} _qsz)
            vw_json_str(_jp "${_qp}")
            vw_json_str(_js "${_qs}")
            list(APPEND _qemu_arr "{\"path\":${_jp},\"sha256\":${_js},\"size\":${_qsz}}")
        endforeach()
    endif()
    string(JOIN "," _qemu_joined ${_qemu_arr})

    list(LENGTH VW_FIRMWARE _fw_n)

    # Gate result: embed verbatim if provided, otherwise mark not-run.
    if(DEFINED VW_GATE_RESULT_JSON AND NOT VW_GATE_RESULT_JSON STREQUAL "")
        set(_gate "${VW_GATE_RESULT_JSON}")
    else()
        set(_gate "{\"status\":\"not_run\"}")
    endif()

    set(_json
"{
  \"schema\": ${_schema},
  \"product_version\": ${_version},
  \"generated_at\": ${_generated},
  \"host\": {\"os\": ${_host_os}, \"arch\": ${_host_arch}},
  \"verified\": false,
  \"real_hardware_verified\": false,
  \"contrib_backends\": ${_contrib},
  \"backends\": {\"macos\": ${_bm}, \"windows\": ${_bw}, \"linux_kvm\": ${_bl}, \"harmony\": ${_bh}},
  \"main_program\": {\"path\": ${_main_path}, \"sha256\": ${_main_sha}, \"size\": ${VW_MAIN_SIZE}},
  \"qemu_binaries\": [${_qemu_joined}],
  \"firmware_count\": ${_fw_n},
  \"support_count\": ${VW_SUPPORT_COUNT},
  \"gate\": ${_gate},
  \"minivmac_inprocess\": {\"engine_selfprove_checks\": ${VW_MINIVMAC_SELFPROVE_CHECKS}, \"engine_selfprove_failures\": ${VW_MINIVMAC_SELFPROVE_FAILURES}, \"engine_selfprove_pass\": ${_mv_sp_pass}, \"engine_selfprove_runs\": ${VW_MINIVMAC_SELFPROVE_RUNS}, \"spawn_scan_clean\": ${_mv_scan}, \"force_rebuild_fresh\": ${_mv_rebuilt}, \"product_spawn_scan\": {\"minivmac_path_clean\": ${_mv_prod_clean}, \"allowed_hits\": ${VW_MINIVMAC_PRODUCT_SCAN_ALLOWED}, \"violation_hits\": ${VW_MINIVMAC_PRODUCT_SCAN_VIOLATIONS}, \"violations\": ${VW_MINIVMAC_PRODUCT_SCAN_VIOLATIONS_JSON}}, \"branch_spawn_scan\": {\"minivmac_path_clean\": ${_mv_branch_clean}}}
}")
    file(WRITE "${VW_OUTPUT_FILE}" "${_json}")
endfunction()

# --- command-line entry point ---------------------------------------------
if(VW_MANIFEST_ENTRY)
    if(NOT DEFINED BUNDLE_ROOT OR BUNDLE_ROOT STREQUAL "")
        message(FATAL_ERROR "BUNDLE_ROOT is required")
    endif()
    if(NOT DEFINED OUTPUT_FILE OR OUTPUT_FILE STREQUAL "")
        set(OUTPUT_FILE "${BUNDLE_ROOT}/virtualization-release-manifest.json")
    endif()
    vw_scan_bundle("${BUNDLE_ROOT}")
    vw_write_manifest(
        OUTPUT_FILE "${OUTPUT_FILE}"
        PRODUCT_VERSION "${PRODUCT_VERSION}"
        HOST_OS "${HOST_OS}"
        HOST_ARCH "${HOST_ARCH}"
        CONTRIB_BACKENDS "${CONTRIB_BACKENDS}"
        BACKENDS_MAC "${BACKEND_MAC}"
        BACKENDS_WINDOWS "${BACKEND_WINDOWS}"
        BACKENDS_LINUX_KVM "${BACKEND_LINUX_KVM}"
        BACKENDS_HARMONY "${BACKEND_HARMONY}"
        QEMU_BINARIES ${VW_QEMU_BINARIES}
        QEMU_SHA256 ${VW_QEMU_SHA256}
        QEMU_SIZES ${VW_QEMU_SIZES}
        FIRMWARE ${VW_FIRMWARE}
        SUPPORT ${VW_SUPPORT})
    message(STATUS "[manifest] wrote ${OUTPUT_FILE}")
endif()
