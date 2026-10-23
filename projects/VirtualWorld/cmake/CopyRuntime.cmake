if(NOT DEFINED SOURCE_DIR OR NOT IS_DIRECTORY "${SOURCE_DIR}")
    message(FATAL_ERROR "Runtime source directory is missing: ${SOURCE_DIR}")
endif()

if(NOT DEFINED DESTINATION_DIR OR DESTINATION_DIR STREQUAL "")
    message(FATAL_ERROR "Runtime destination directory is missing.")
endif()

get_filename_component(_destination_name "${DESTINATION_DIR}" NAME)
if(NOT _destination_name STREQUAL "runtime")
    message(FATAL_ERROR "Refusing to replace unexpected runtime destination: ${DESTINATION_DIR}")
endif()

file(REMOVE_RECURSE "${DESTINATION_DIR}")
file(MAKE_DIRECTORY "${DESTINATION_DIR}")
file(COPY "${SOURCE_DIR}/"
     DESTINATION "${DESTINATION_DIR}"
     PATTERN ".DS_Store" EXCLUDE
     PATTERN ".gitkeep" EXCLUDE
     PATTERN "include" EXCLUDE
     REGEX "/[^/]+ [0-9]+(\\.[^/]*)?$" EXCLUDE)
