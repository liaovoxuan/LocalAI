if(NOT CMAKE_HOST_SYSTEM_NAME STREQUAL "Darwin")
    return()
endif()

if(NOT DEFINED RUNTIME_ROOT OR NOT EXISTS "${RUNTIME_ROOT}")
    message(FATAL_ERROR "RUNTIME_ROOT is missing or does not exist: ${RUNTIME_ROOT}")
endif()

set(_qemu_lib_dir "${RUNTIME_ROOT}/lib")
file(MAKE_DIRECTORY "${_qemu_lib_dir}")

file(GLOB _qemu_seed_files
    "${RUNTIME_ROOT}/bin/qemu-*"
    "${RUNTIME_ROOT}/bin/*.dylib")

set(_queue ${_qemu_seed_files})
set(_seen "")

while(_queue)
    list(POP_FRONT _queue _item)
    if(NOT EXISTS "${_item}")
        continue()
    endif()
    list(FIND _seen "${_item}" _seen_index)
    if(NOT _seen_index EQUAL -1)
        continue()
    endif()
    list(APPEND _seen "${_item}")

    execute_process(
        COMMAND otool -L "${_item}"
        OUTPUT_VARIABLE _otool_output
        ERROR_QUIET)

    string(REGEX MATCHALL "(/opt/homebrew|/usr/local)[^\n\t ]+\\.dylib" _deps "${_otool_output}")
    list(REMOVE_DUPLICATES _deps)

    get_filename_component(_item_dir "${_item}" DIRECTORY)
    if(_item_dir STREQUAL _qemu_lib_dir)
        set(_loader_prefix "@loader_path")
    else()
        set(_loader_prefix "@loader_path/../lib")
    endif()

    foreach(_dep IN LISTS _deps)
        get_filename_component(_dep_name "${_dep}" NAME)
        set(_dest "${_qemu_lib_dir}/${_dep_name}")
        if(NOT EXISTS "${_dest}")
            file(COPY_FILE "${_dep}" "${_dest}")
            file(CHMOD "${_dest}"
                PERMISSIONS OWNER_READ OWNER_WRITE OWNER_EXECUTE
                            GROUP_READ GROUP_EXECUTE
                            WORLD_READ WORLD_EXECUTE)
            list(APPEND _queue "${_dest}")
        endif()
        execute_process(
            COMMAND install_name_tool -change "${_dep}" "${_loader_prefix}/${_dep_name}" "${_item}"
            ERROR_QUIET)
    endforeach()
endwhile()

file(GLOB _bundled_qemu_libs "${_qemu_lib_dir}/*.dylib")
foreach(_lib IN LISTS _bundled_qemu_libs)
    get_filename_component(_lib_name "${_lib}" NAME)
    execute_process(
        COMMAND install_name_tool -id "@loader_path/${_lib_name}" "${_lib}"
        ERROR_QUIET)
endforeach()

find_program(_codesign codesign)
if(_codesign)
    set(_hvf_entitlements "")
    if(DEFINED QEMU_SOURCE_DIR AND EXISTS "${QEMU_SOURCE_DIR}/accel/hvf/entitlements.plist")
        set(_hvf_entitlements "${QEMU_SOURCE_DIR}/accel/hvf/entitlements.plist")
    endif()

    foreach(_lib IN LISTS _bundled_qemu_libs)
        execute_process(
            COMMAND "${_codesign}" --force --sign - "${_lib}"
            ERROR_QUIET)
    endforeach()

    file(GLOB _bundled_qemu_bins "${RUNTIME_ROOT}/bin/qemu-*")
    foreach(_bin IN LISTS _bundled_qemu_bins)
        if(_bin MATCHES "/qemu-system-.*$")
            if(_hvf_entitlements)
                execute_process(
                    COMMAND "${_codesign}" --force --sign - --entitlements "${_hvf_entitlements}" "${_bin}"
                    ERROR_QUIET)
            else()
                execute_process(
                    COMMAND "${_codesign}" --force --sign - "${_bin}"
                    ERROR_QUIET)
            endif()
        else()
            execute_process(
                COMMAND "${_codesign}" --force --sign - "${_bin}"
                ERROR_QUIET)
        endif()
    endforeach()
endif()
