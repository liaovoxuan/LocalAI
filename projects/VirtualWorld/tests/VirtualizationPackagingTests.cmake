# VirtualizationPackagingTests.cmake
#
# Packaging / install release gate for VirtualWorld. This is the script CTest
# runs for the VirtualWorldPackagingGate test (wired in CMakeLists.txt), but it
# can also be driven by hand:
#
#   cmake -DPACKAGING_GATE_MODE=verify \
#         -DBUNDLE_ROOT=.../VirtualWorld.app \
#         -DPRODUCT_VERSION=1.2.3 \
#         -DHOST_OS=Darwin -DHOST_ARCH=arm64 \
#         -DCONTRIB_BACKENDS=OFF \
#         -DBACKEND_MAC=ON -DBACKEND_WINDOWS=ON \
#         -DBACKEND_LINUX_KVM=OFF -DBACKEND_HARMONY=OFF \
#         -DMANIFEST_FILE=virtualization-release-manifest.json \
#         -P tests/VirtualizationPackagingTests.cmake
#
# Modes:
#   verify  - run the bundle-integrity gate AND write the manifest (default)
#   scan    - only write the manifest (no pass/fail verdict)
#
# The gate never fakes success: on a host that cannot verify an item (e.g. a
# Windows-only installer rule on a macOS build host) the corresponding check is
# reported as "skipped". The generated manifest records verified=false and
# real_hardware_verified=false unconditionally.

include("${CMAKE_CURRENT_LIST_DIR}/../cmake/VerifyVirtualizationBundle.cmake")
include("${CMAKE_CURRENT_LIST_DIR}/../cmake/GenerateVirtualizationManifest.cmake")

# --- resolve + normalise inputs --------------------------------------------
if(NOT DEFINED PACKAGING_GATE_MODE OR PACKAGING_GATE_MODE STREQUAL "")
    set(PACKAGING_GATE_MODE "verify")
endif()
if(NOT DEFINED BUNDLE_ROOT OR BUNDLE_ROOT STREQUAL "")
    message(FATAL_ERROR "BUNDLE_ROOT is required (VirtualWorld.app or install root)")
endif()
if(NOT DEFINED MANIFEST_FILE OR MANIFEST_FILE STREQUAL "")
    set(MANIFEST_FILE "${BUNDLE_ROOT}/virtualization-release-manifest.json")
endif()
if(NOT DEFINED HOST_OS OR HOST_OS STREQUAL "")
    set(HOST_OS "${CMAKE_HOST_SYSTEM_NAME}")
endif()
if(NOT DEFINED HOST_ARCH OR HOST_ARCH STREQUAL "")
    set(HOST_ARCH "${CMAKE_HOST_SYSTEM_PROCESSOR}")
endif()
if(NOT DEFINED PRODUCT_VERSION OR PRODUCT_VERSION STREQUAL "")
    set(PRODUCT_VERSION "0.0.0")
endif()
if(NOT DEFINED CONTRIB_BACKENDS OR CONTRIB_BACKENDS STREQUAL "")
    set(CONTRIB_BACKENDS "OFF")
endif()
# Backend compile-in booleans: default to OFF so a missing -D does not crash.
if(NOT DEFINED BACKEND_MAC)
    set(BACKEND_MAC OFF)
endif()
if(NOT DEFINED BACKEND_WINDOWS)
    set(BACKEND_WINDOWS OFF)
endif()
if(NOT DEFINED BACKEND_LINUX_KVM)
    set(BACKEND_LINUX_KVM OFF)
endif()
if(NOT DEFINED BACKEND_HARMONY)
    set(BACKEND_HARMONY OFF)
endif()

# --- scan the bundle (always) ----------------------------------------------
vw_scan_bundle("${BUNDLE_ROOT}")

# --- Mini vMac in-process engine verifications (real execution) -------------
# These also run as dedicated CTests; here the gate re-runs them against the
# built artifacts and records the REAL results. A non-clean spawn scan, or a
# self-prove with any failure across multiple runs, fails the gate (no fake
# green). The product-tree scan distinguishes the Mini vMac launch path (which
# MUST NOT spawn a child process, and must not use PID semantics) from the QEMU
# and platform backends (which legitimately spawn external QEMU processes and
# are outside this constraint).
set(_mv_selfprove_checks 0)
set(_mv_selfprove_failures -1)
set(_mv_selfprove_pass FALSE)
set(_mv_selfprove_runs 3)
set(_mv_scan_clean FALSE)
set(_mv_prod_clean FALSE)
set(_mv_prod_allowed 0)
set(_mv_prod_violation_count 0)
set(_mv_prod_violations "")
set(_mv_prod_viol_json "[]")

# (0) FORCE-REBUILD the engine archive + self-prove binary BEFORE any scan or
# self-prove run. A prior gate run compared against STALE build artifacts (the
# self-prove binary reported an old check count and the gate produced a false
# failure). Rebuilding guarantees the gate always measures the freshly built
# engine, never a leftover from an earlier configuration. This is a hard part
# of the gate: if the rebuild cannot produce fresh artifacts, downstream
# verification is meaningless and the gate must fail (never fake-green).
set(_mv_rebuilt_engine FALSE)
set(_mv_rebuilt_selfprove FALSE)
set(_mv_rebuilt FALSE)
# VW_GATE_CMAKE_BINARY_DIR / VW_GATE_CMAKE_COMMAND are passed by the
# add_test wiring (CMakeLists.txt). They use non-reserved names on purpose:
# CMAKE_BINARY_DIR / CMAKE_COMMAND are built-in variables that `cmake -P`
# ignores when supplied via -D, so referencing them directly would resolve
# empty and the force-rebuild would silently no-op.
if(DEFINED VW_GATE_CMAKE_BINARY_DIR AND DEFINED VW_GATE_CMAKE_COMMAND
   AND NOT VW_GATE_CMAKE_BINARY_DIR STREQUAL "" AND NOT VW_GATE_CMAKE_COMMAND STREQUAL "")
    set(_mv_rebuilt_engine TRUE)
    set(_mv_rebuilt_selfprove TRUE)
    execute_process(
        COMMAND "${VW_GATE_CMAKE_COMMAND}" --build "${VW_GATE_CMAKE_BINARY_DIR}" --target minivmac_engine_standalone
        OUTPUT_VARIABLE _mv_rb_eng_out RESULT_VARIABLE _mv_rb_eng_rc)
    if(NOT _mv_rb_eng_rc EQUAL 0)
        set(_mv_rebuilt_engine FALSE)
        message(STATUS "[packaging-gate] FORCE-REBUILD minivmac_engine_standalone FAILED (rc=${_mv_rb_eng_rc})")
    else()
        message(STATUS "[packaging-gate] FORCE-REBUILD minivmac_engine_standalone OK")
    endif()
    execute_process(
        COMMAND "${VW_GATE_CMAKE_COMMAND}" --build "${VW_GATE_CMAKE_BINARY_DIR}" --target VirtualWorldMiniVmacInProcessTests
        OUTPUT_VARIABLE _mv_rb_sp_out RESULT_VARIABLE _mv_rb_sp_rc)
    if(NOT _mv_rb_sp_rc EQUAL 0)
        set(_mv_rebuilt_selfprove FALSE)
        message(STATUS "[packaging-gate] FORCE-REBUILD VirtualWorldMiniVmacInProcessTests FAILED (rc=${_mv_rb_sp_rc})")
    else()
        message(STATUS "[packaging-gate] FORCE-REBUILD VirtualWorldMiniVmacInProcessTests OK")
    endif()
    if(NOT _mv_rebuilt_engine OR NOT _mv_rebuilt_selfprove)
        set(_mv_rebuilt FALSE)
    else()
        set(_mv_rebuilt TRUE)
    endif()
else()
    message(STATUS "[packaging-gate] FORCE-REBUILD skipped (VW_GATE_CMAKE_BINARY_DIR/VW_GATE_CMAKE_COMMAND not provided)")
endif()

if(DEFINED PYTHON3_EXECUTABLE AND NOT PYTHON3_EXECUTABLE STREQUAL "")
    set(_mv_py "${PYTHON3_EXECUTABLE}")
else()
    set(_mv_py "python3")
endif()

# (1) Engine spawn scan: the engine sources + produced archive must be CLEAN.
execute_process(
    COMMAND ${_mv_py} "${CMAKE_CURRENT_LIST_DIR}/../third_party/minivmac/inprocess/scan_spawn_apis.py"
    WORKING_DIRECTORY "${CMAKE_CURRENT_LIST_DIR}"
    OUTPUT_VARIABLE _mv_scan_out RESULT_VARIABLE _mv_scan_rc)
if(_mv_scan_rc EQUAL 0 AND _mv_scan_out MATCHES "RESULT: CLEAN")
    set(_mv_scan_clean TRUE)
endif()

# (2) Product-tree spawn scan: proves the *product* (not just the engine) does
# not spawn for the Mini vMac path. QEMU / platform backends are allow-listed
# because spawning external QEMU is by design and outside this constraint; they
# must NOT be treated as violations ("do not false-positive them").
#
# NOTE: VirtualMachineWindow is in the allow-list because the coarse product
# scan cannot distinguish Mini vMac (isSystemSoftwareConfig) branches from
# QEMU branches within the same file. The dedicated branch-level scan in
# step (2b) below provides precise Mini vMac path verification for this file.
set(_mv_allowed_files
    VirtualizationCapabilities
    QemuEngine
    VirtualMachineWindow
    RuntimeManager
    Platform
    LinuxKvmEngine
    HarmonyVirtualizationEngine
    WindowsHypervisorEngine)
execute_process(
    COMMAND ${_mv_py} "${CMAKE_CURRENT_LIST_DIR}/../third_party/minivmac/inprocess/scan_spawn_apis.py" --product
    WORKING_DIRECTORY "${CMAKE_CURRENT_LIST_DIR}"
    OUTPUT_VARIABLE _mv_prod_out RESULT_VARIABLE _mv_prod_rc)
string(REPLACE "\n" ";" _mv_prod_lines "${_mv_prod_out}")
foreach(_line ${_mv_prod_lines})
    if(_line MATCHES "^  (src/[^:]+):([0-9]+) ([A-Za-z0-9_]+) ::")
        set(_hit_file "${CMAKE_MATCH_1}")
        set(_hit_api "${CMAKE_MATCH_3}")
        get_filename_component(_hit_base "${_hit_file}" NAME_WE)
        set(_allowed FALSE)
        foreach(_af ${_mv_allowed_files})
            if(_hit_base STREQUAL "${_af}")
                set(_allowed TRUE)
                break()
            endif()
        endforeach()
        if(_allowed)
            math(EXPR _mv_prod_allowed "${_mv_prod_allowed} + 1")
        else()
            math(EXPR _mv_prod_violation_count "${_mv_prod_violation_count} + 1")
            list(APPEND _mv_prod_violations "${_hit_file}:${CMAKE_MATCH_2} ${_hit_api}")
            message(STATUS "[packaging-gate] Mini vMac spawn violation: ${_hit_file}:${CMAKE_MATCH_2} ${_hit_api}")
        endif()
    endif()
endforeach()
if(_mv_prod_violation_count EQUAL 0)
    set(_mv_prod_clean TRUE)
else()
    set(_mv_viol_arr "")
    list(LENGTH _mv_prod_violations _vn)
    math(EXPR _vlast "${_vn} - 1")
    foreach(_i RANGE ${_vlast})
        list(GET _mv_prod_violations ${_i} _v)
        vw_json_str(_vj "${_v}")
        list(APPEND _mv_viol_arr "${_vj}")
    endforeach()
    string(JOIN "," _mv_viol_joined ${_mv_viol_arr})
    set(_mv_prod_viol_json "[${_mv_viol_joined}]")
endif()

# (2b) Branch-level Mini vMac path scan: the coarse product-tree scan above
# cannot distinguish Mini vMac (isSystemSoftwareConfig) branches from QEMU
# branches within VirtualMachineWindow.cpp, so it allow-lists the entire file.
# This dedicated scan parses the source to identify isSystemSoftwareConfig
# branches and verifies that no forbidden spawn/PID APIs appear in them.
# Exit code 0 = clean, 1 = hard constraint violation.
set(_mv_branch_clean FALSE)
execute_process(
    COMMAND ${_mv_py} "${CMAKE_CURRENT_LIST_DIR}/../tools/verify_minivmac_inprocess_path.py"
        "--src-dir" "${CMAKE_CURRENT_LIST_DIR}/../src"
    WORKING_DIRECTORY "${CMAKE_CURRENT_LIST_DIR}"
    OUTPUT_VARIABLE _mv_branch_out RESULT_VARIABLE _mv_branch_rc)
if(_mv_branch_rc EQUAL 0)
    set(_mv_branch_clean TRUE)
    message(STATUS "[packaging-gate] Mini vMac branch-level scan: CLEAN")
else()
    message(STATUS "[packaging-gate] Mini vMac branch-level scan: VIOLATION (rc=${_mv_branch_rc})")
    message(STATUS "[packaging-gate] branch scan output:\n${_mv_branch_out}")
endif()

# (3) Engine self-prove: run multiple times. During the engine's history
# (rev3: a callback off-by-one; rev4: an intermittent SIGABRT from a test that
# raced stop() against the destructor — a test self-UB, now fixed) single-pass
# results were not sufficient proof of stability, so we keep the 3-run
# repetition. Current revision (rev7) is 139 checks / 0 failures per variant.
if(DEFINED SELFPROVE_EXE AND EXISTS "${SELFPROVE_EXE}")
    set(_mv_sp_all_ok TRUE)
    foreach(_r RANGE 2)  # 3 runs (0, 1, 2)
        execute_process(COMMAND "${SELFPROVE_EXE}"
            OUTPUT_VARIABLE _mv_sp_out_${_r} RESULT_VARIABLE _mv_sp_rc_${_r})
        if(NOT _mv_sp_out_${_r} MATCHES "checks: ([0-9]+), failures: ([0-9]+)")
            set(_mv_sp_all_ok FALSE)
            message(STATUS "[packaging-gate] self-prove run ${_r}: output unparseable (rc=${_mv_sp_rc_${_r}})")
        else()
            set(_c "${CMAKE_MATCH_1}")
            set(_f "${CMAKE_MATCH_2}")
            if(_r EQUAL 0)
                set(_mv_selfprove_checks "${_c}")
            endif()
            if(NOT _mv_sp_rc_${_r} EQUAL 0 OR NOT _f EQUAL 0)
                set(_mv_sp_all_ok FALSE)
            endif()
            message(STATUS "[packaging-gate] self-prove run ${_r}: checks=${_c} failures=${_f}")
        endif()
    endforeach()
    if(_mv_sp_all_ok)
        set(_mv_selfprove_pass TRUE)
        set(_mv_selfprove_failures 0)
    endif()
endif()

# --- optional integrity gate ------------------------------------------------
set(_gate_json "{\"status\":\"not_run\"}")
set(_do_gate FALSE)
if(PACKAGING_GATE_MODE STREQUAL "verify" OR PACKAGING_GATE_MODE STREQUAL "all")
    set(_do_gate TRUE)
endif()

if(_do_gate)
    vw_verify_bundle(BUNDLE_ROOT "${BUNDLE_ROOT}")

    # Compose the gate result JSON from the checks + failures.
    set(_checks_json "")
    list(LENGTH VW_GATE_CHECKS _nc)
    if(_nc GREATER 0)
        list(APPEND _checks_json ${VW_GATE_CHECKS})
    endif()
    string(JOIN "," _checks_joined ${_checks_json})

    set(_fail_json "")
    list(LENGTH VW_GATE_FAILURES _nf)
    if(_nf GREATER 0)
        math(EXPR _flast "${_nf} - 1")
        foreach(_i RANGE ${_flast})
            list(GET VW_GATE_FAILURES ${_i} _f)
            # _f is "CODE|precise-path|message"
            string(REPLACE "|" ";" _fparts "${_f}")
            list(GET _fparts 0 _code)
            list(GET _fparts 1 _fpath)
            list(GET _fparts 2 _fmsg)
            vw_json_str(_jc "${_code}")
            vw_json_str(_jp "${_fpath}")
            vw_json_str(_jm "${_fmsg}")
            list(APPEND _fail_json "{\"code\":${_jc},\"path\":${_jp},\"message\":${_jm}}")
        endforeach()
    endif()
    string(JOIN "," _fail_joined ${_fail_json})

    vw_json_str(_status "${VW_GATE_STATUS}")
    set(_gate_json "{\"status\":${_status},\"checks\":[${_checks_joined}],\"failure_count\":${_nf},\"failures\":[${_fail_joined}]}")

    # Mini vMac in-process verifications are part of the release gate: a non-clean
    # spawn scan or a self-prove with failures > 0 fails the gate (never faked).
    if(NOT _mv_scan_clean)
        list(APPEND VW_GATE_FAILURES
             "MINIVMAC_SCAN|spawn-api-scan|spawn-API scan not CLEAN (RESULT: SPAWN-APIS-FOUND)")
        set(VW_GATE_STATUS "failed")
    endif()
    if(NOT _mv_selfprove_pass)
        list(APPEND VW_GATE_FAILURES
             "MINIVMAC_SELFPROVE|minivmac-self-prove|engine self-prove not at 0 failures across all ${_mv_selfprove_runs} runs (checks=${_mv_selfprove_checks} failures=${_mv_selfprove_failures})")
        set(VW_GATE_STATUS "failed")
    endif()
    # The product-tree scan is a HARD-CONSTRAINT check: the Mini vMac launch
    # path must not spawn a child process and must not use PID semantics. Any
    # non-allow-listed spawn-API hit (e.g. src/main.cpp) is treated as a hard
    # constraint violation, not merely a compile failure.
    if(NOT _mv_prod_clean)
        list(APPEND VW_GATE_FAILURES
             "MINIVMAC_PRODUCT_SPAWN|minivmac-product-spawn|HARD CONSTRAINT VIOLATION: Mini v Mac launch path spawns a child process (forbidden: no process spawn + no PID semantics). Violations: ${_mv_prod_violations}")
        set(VW_GATE_STATUS "failed")
    endif()
    # The branch-level scan is a HARD-CONSTRAINT check: the Mini vMac
    # (isSystemSoftwareConfig) path in VirtualMachineWindow.cpp must not
    # contain any forbidden spawn/PID APIs. This is the precise complement
    # to the coarse product-tree scan which allow-lists the entire file.
    if(NOT _mv_branch_clean)
        list(APPEND VW_GATE_FAILURES
             "MINIVMAC_BRANCH_SPAWN|minivmac-branch-spawn|HARD CONSTRAINT VIOLATION: Mini vMac isSystemSoftwareConfig branch contains forbidden spawn/PID APIs (branch-level scan failed). Run: python3 tools/verify_minivmac_inprocess_path.py --verbose")
        set(VW_GATE_STATUS "failed")
    endif()
    # The force-rebuild at the top of this section must have produced fresh
    # engine + self-prove artifacts. If it failed, downstream verification is
    # meaningless (it would compare against stale leftovers), so fail loudly.
    if(NOT _mv_rebuilt)
        list(APPEND VW_GATE_FAILURES
             "MINIVMAC_REBUILD|minivmac-force-rebuild|gate could not rebuild fresh engine/self-prove artifacts (engine_ok=${_mv_rebuilt_engine} selfprove_ok=${_mv_rebuilt_selfprove}); refusing to verify against possibly-stale artifacts")
        set(VW_GATE_STATUS "failed")
    endif()
endif()

# --- write the manifest (always) -------------------------------------------
vw_write_manifest(
    OUTPUT_FILE "${MANIFEST_FILE}"
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
    SUPPORT ${VW_SUPPORT}
    GATE_RESULT_JSON "${_gate_json}"
    MINIVMAC_SELFPROVE_CHECKS "${_mv_selfprove_checks}"
    MINIVMAC_SELFPROVE_FAILURES "${_mv_selfprove_failures}"
    MINIVMAC_SELFPROVE_PASS "${_mv_selfprove_pass}"
    MINIVMAC_SELFPROVE_RUNS "${_mv_selfprove_runs}"
    MINIVMAC_SPAWN_SCAN_CLEAN "${_mv_scan_clean}"
    MINIVMAC_PRODUCT_SCAN_CLEAN "${_mv_prod_clean}"
    MINIVMAC_PRODUCT_SCAN_ALLOWED "${_mv_prod_allowed}"
    MINIVMAC_PRODUCT_SCAN_VIOLATIONS "${_mv_prod_violation_count}"
    MINIVMAC_PRODUCT_SCAN_VIOLATIONS_JSON "${_mv_prod_viol_json}"
    MINIVMAC_BRANCH_SCAN_CLEAN "${_mv_branch_clean}"
    MINIVMAC_REBUILT "${_mv_rebuilt}")

message(STATUS "[packaging-gate] wrote manifest: ${MANIFEST_FILE}")
message(STATUS "[packaging-gate] mode=${PACKAGING_GATE_MODE} gate-status=${VW_GATE_STATUS}")

if(_do_gate AND VW_GATE_STATUS STREQUAL "failed")
    message(FATAL_ERROR "VIRTUALWORLD_PACKAGING_GATE_FAILED")
endif()
