cmake_minimum_required(VERSION 3.16)
project(CloudAIHarmonyNative LANGUAGES CXX)

add_library(cloudai_harmony SHARED
    ../../../../src/CloudAIHarmony.cpp
)

target_include_directories(cloudai_harmony PRIVATE
    ../../../../include
)

target_compile_features(cloudai_harmony PRIVATE cxx_std_17)
