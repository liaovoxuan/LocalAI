# CMakeLists.txt for the HarmonyPC HAP native capability test.
# Builds libcaptest.so, which exposes RunCapTests() that runs the SAME
# run_cap_tests() battery used by the shell probe, writing results to the
# app's private file directory and to hilog (the app log).
cmake_minimum_required(VERSION 3.16)
project(captest)

set(CMAKE_CXX_STANDARD 17)

add_library(captest SHARED
    cap_hap_test.cpp
    cap_tests.c
)

target_include_directories(captest PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})

# hilog NDK is the app-log sink on OpenHarmony/HarmonyOS.
target_link_libraries(captest PUBLIC hilog_ndk.z)
