cmake_minimum_required(VERSION 3.20)
project(LocalAINativeComplete LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)

find_package(Qt6 6.2 REQUIRED COMPONENTS Core Network Widgets)
enable_testing()

add_library(localai_core STATIC
    src/CompleteConfig.cpp
    src/CompleteProvider.cpp
)
target_include_directories(localai_core PUBLIC src)
target_link_libraries(localai_core PUBLIC Qt6::Core Qt6::Network)

add_executable(LocalAI MACOSX_BUNDLE
    src/GuiMainComplete.cpp
    src/CompleteChatWindow.cpp
    src/CompleteChatWindow.hpp
)
target_link_libraries(LocalAI PRIVATE localai_core Qt6::Widgets)
set_target_properties(LocalAI PROPERTIES
    MACOSX_BUNDLE_BUNDLE_NAME "LocalAI"
    MACOSX_BUNDLE_GUI_IDENTIFIER "com.localai.desktop"
    MACOSX_BUNDLE_SHORT_VERSION_STRING "1.0"
    MACOSX_BUNDLE_BUNDLE_VERSION "100"
)

add_executable(LocalAI_SE MACOSX_BUNDLE src/TerminalMainComplete.cpp)
target_link_libraries(LocalAI_SE PRIVATE localai_core)
set_target_properties(LocalAI_SE PROPERTIES
    MACOSX_BUNDLE_BUNDLE_NAME "LocalAI SE"
    MACOSX_BUNDLE_GUI_IDENTIFIER "com.localai.se"
    MACOSX_BUNDLE_SHORT_VERSION_STRING "1.0"
    MACOSX_BUNDLE_BUNDLE_VERSION "100"
    MACOSX_BUNDLE_INFO_PLIST "${CMAKE_CURRENT_SOURCE_DIR}/cmake/LocalAISE-Info.plist.in"
)

add_executable(CompleteConfigTests tests/CompleteConfigTests.cpp)
target_link_libraries(CompleteConfigTests PRIVATE localai_core Qt6::Core)
add_test(NAME CompleteConfigTests COMMAND CompleteConfigTests)
