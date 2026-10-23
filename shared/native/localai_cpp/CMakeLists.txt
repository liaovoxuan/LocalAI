cmake_minimum_required(VERSION 3.16)
project(LocalAI_Native_CPP LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

option(LOCALAI_USE_LIBCURL "Build with libcurl HTTP backend" OFF)

add_library(localai_core
    src/LocalAI.cpp
    $<$<BOOL:${LOCALAI_USE_LIBCURL}>:src/CurlHttpClient.cpp>
)

target_include_directories(localai_core
    PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include
)

if(LOCALAI_USE_LIBCURL)
    find_package(CURL REQUIRED)
    target_compile_definitions(localai_core PUBLIC LOCALAI_USE_LIBCURL=1)
    target_link_libraries(localai_core PUBLIC CURL::libcurl)
endif()

add_executable(localai_cli
    src/main.cpp
)

target_link_libraries(localai_cli PRIVATE localai_core)
