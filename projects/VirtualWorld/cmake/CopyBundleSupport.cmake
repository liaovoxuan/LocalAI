if(NOT DEFINED SOURCE_DIR OR NOT IS_DIRECTORY "${SOURCE_DIR}")
    message(FATAL_ERROR "Support asset source directory is missing: ${SOURCE_DIR}")
endif()

if(NOT DEFINED DESTINATION_DIR OR DESTINATION_DIR STREQUAL "")
    message(FATAL_ERROR "Support asset destination directory is missing.")
endif()

file(REMOVE_RECURSE "${DESTINATION_DIR}")
file(MAKE_DIRECTORY "${DESTINATION_DIR}")
file(COPY "${SOURCE_DIR}/"
     DESTINATION "${DESTINATION_DIR}"
     PATTERN ".DS_Store" EXCLUDE
     PATTERN ".*" EXCLUDE)
