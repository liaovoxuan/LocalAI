cmake_minimum_required(VERSION 3.16)
project(CloudAIHarmony LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

option(CLOUDAI_USE_LIBCURL "Build with libcurl HTTP backend" OFF)

add_library(cloudai_harmony_core
    src/CloudAIHarmony.cpp
)

target_include_directories(cloudai_harmony_core
    PUBLIC
        ${CMAKE_CURRENT_SOURCE_DIR}/include
)

if(CLOUDAI_USE_LIBCURL)
    find_package(CURL REQUIRED)
    target_compile_definitions(cloudai_harmony_core PUBLIC CLOUDAI_USE_LIBCURL=1)
    target_link_libraries(cloudai_harmony_core PUBLIC CURL::libcurl)
endif()

add_executable(cloudai_harmony_cli
    src/main.cpp
)

target_link_libraries(cloudai_harmony_cli PRIVATE cloudai_harmony_core)

