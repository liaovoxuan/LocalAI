cmake_minimum_required(VERSION 3.16)
project(CloudAI_Source_CPP LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

option(CLOUDAI_USE_LIBCURL "Enable the libcurl HTTP backend" OFF)
option(CLOUDAI_BUILD_TESTS "Build CloudAI C++ smoke tests" ON)

add_library(cloudai_core
    src/CloudAIHarmony.cpp
)
target_include_directories(cloudai_core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)

if(CLOUDAI_USE_LIBCURL)
    find_package(CURL REQUIRED)
    target_compile_definitions(cloudai_core PUBLIC CLOUDAI_USE_LIBCURL=1)
    target_link_libraries(cloudai_core PUBLIC CURL::libcurl)
endif()

add_executable(cloudai_cli src/main.cpp)
target_link_libraries(cloudai_cli PRIVATE cloudai_core)

if(CLOUDAI_BUILD_TESTS)
    enable_testing()
    add_executable(cloudai_core_smoke tests/core_smoke.cpp)
    target_link_libraries(cloudai_core_smoke PRIVATE cloudai_core)
    add_test(NAME cloudai_core_smoke COMMAND cloudai_core_smoke)
endif()
