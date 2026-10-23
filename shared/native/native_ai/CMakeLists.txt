cmake_minimum_required(VERSION 3.20)

project(LocalAI_Native LANGUAGES CXX)

set(LOCALAI_REPO_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../../..")
set(LOCALAI_CORE_SOURCE_DIR "${LOCALAI_REPO_ROOT}/shared/native/localai_cpp")
set(LOCALAI_SHARED_ASSETS_DIR "${LOCALAI_REPO_ROOT}/shared/assets")
set(LOCALAI_SHARED_THIRD_PARTY_DIR "${LOCALAI_REPO_ROOT}/shared/third_party")
set(LOCALAI_SHARED_RUNTIME_DIR "${LOCALAI_REPO_ROOT}/shared/runtime")
set(LOCALAI_ARTIFACTS_DIR "${LOCALAI_REPO_ROOT}/artifacts")

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)

option(LOCALAI_ENABLE_PYTHON_RUNTIME
    "Bundle and launch the transitional Python feature runtime" ${APPLE})
option(LOCALAI_BUNDLE_LLAMA_SOURCE
    "Bundle a source archive of third_party/llama.cpp into macOS app bundles" OFF)
set(LOCALAI_PYTHON_RUNTIME_ROOT "" CACHE PATH
    "Platform-native Python runtime root containing LocalAI and LocalAI_SE directories")

string(TOLOWER "${CMAKE_SYSTEM_NAME}" LOCALAI_SYSTEM_NAME_LOWER)
string(TOLOWER "${CMAKE_SYSTEM_PROCESSOR}" LOCALAI_SYSTEM_PROCESSOR_LOWER)
set(LOCALAI_RESTRICTED_CPP_PLATFORM OFF)
if(LOCALAI_SYSTEM_NAME_LOWER MATCHES "harmony|ohos")
    set(LOCALAI_RESTRICTED_CPP_PLATFORM ON)
elseif(LOCALAI_SYSTEM_PROCESSOR_LOWER MATCHES "loongarch|riscv")
    set(LOCALAI_RESTRICTED_CPP_PLATFORM ON)
elseif(WIN32 AND LOCALAI_SYSTEM_PROCESSOR_LOWER MATCHES "arm64|aarch64")
    set(LOCALAI_RESTRICTED_CPP_PLATFORM ON)
endif()

if(LOCALAI_RESTRICTED_CPP_PLATFORM AND LOCALAI_ENABLE_PYTHON_RUNTIME)
    message(STATUS "Restricted target detected: Python bridge will force the llama.cpp backend")
endif()

if(LOCALAI_ENABLE_PYTHON_RUNTIME AND LOCALAI_PYTHON_RUNTIME_ROOT
        AND NOT IS_DIRECTORY "${LOCALAI_PYTHON_RUNTIME_ROOT}")
    message(FATAL_ERROR "LOCALAI_PYTHON_RUNTIME_ROOT is not a directory: ${LOCALAI_PYTHON_RUNTIME_ROOT}")
endif()

find_package(Qt6 REQUIRED COMPONENTS Widgets Network)

add_executable(LocalAI_Native
    src/AppConfig.cpp
    src/AppConfig.hpp
    src/ChatWindow.cpp
    src/ChatWindow.hpp
    src/ProviderClient.cpp
    src/ProviderClient.hpp
    src/main.cpp
)

target_link_libraries(LocalAI_Native PRIVATE Qt6::Widgets Qt6::Network)

# Include LocalAI C++ core and link it to UI executables
add_subdirectory(${LOCALAI_CORE_SOURCE_DIR} ${CMAKE_BINARY_DIR}/localai_cpp_build)
target_link_libraries(LocalAI_Native PRIVATE localai_core)

add_executable(CloudAI_Native
    src/AppConfig.cpp
    src/AppConfig.hpp
    src/ChatWindow.cpp
    src/ChatWindow.hpp
    src/ProviderClient.cpp
    src/ProviderClient.hpp
    src/main.cpp
)

target_compile_definitions(CloudAI_Native PRIVATE CLOUDAI_BUILD=1)
target_link_libraries(CloudAI_Native PRIVATE Qt6::Widgets Qt6::Network)
target_link_libraries(CloudAI_Native PRIVATE localai_core)

add_executable(LocalAI_SE_Native
    src/AppConfig.cpp
    src/AppConfig.hpp
    src/ChatWindow.cpp
    src/ChatWindow.hpp
    src/ProviderClient.cpp
    src/ProviderClient.hpp
    src/main.cpp
)

target_compile_definitions(LocalAI_SE_Native PRIVATE LOCALAI_SE_BUILD=1)
target_link_libraries(LocalAI_SE_Native PRIVATE Qt6::Widgets Qt6::Network localai_core)

foreach(_target LocalAI_Native CloudAI_Native LocalAI_SE_Native)
    if(LOCALAI_ENABLE_PYTHON_RUNTIME)
        target_compile_definitions(${_target} PRIVATE LOCALAI_ENABLE_PYTHON_RUNTIME=1)
    endif()
    if(LOCALAI_RESTRICTED_CPP_PLATFORM)
        target_compile_definitions(${_target} PRIVATE LOCALAI_RESTRICTED_CPP_PLATFORM=1)
    endif()
endforeach()

add_executable(PythonMigrationTranslator
    src/PythonMigrationTranslator.cpp
)
if(WIN32)
    target_link_libraries(PythonMigrationTranslator PRIVATE Shell32)
endif()

add_dependencies(LocalAI_Native PythonMigrationTranslator)
add_dependencies(CloudAI_Native PythonMigrationTranslator)
add_dependencies(LocalAI_SE_Native PythonMigrationTranslator)

if(APPLE)
    set(NATIVE_APP_ICON "${LOCALAI_SHARED_ASSETS_DIR}/icons/localai_dark.icns")
    set_source_files_properties("${NATIVE_APP_ICON}" PROPERTIES MACOSX_PACKAGE_LOCATION "Resources")
    target_sources(LocalAI_Native PRIVATE "${NATIVE_APP_ICON}")
    target_sources(CloudAI_Native PRIVATE "${NATIVE_APP_ICON}")
    target_sources(LocalAI_SE_Native PRIVATE "${NATIVE_APP_ICON}")
    set_target_properties(LocalAI_Native PROPERTIES
        MACOSX_BUNDLE TRUE
        OUTPUT_NAME "LocalAI"
        MACOSX_BUNDLE_BUNDLE_NAME "LocalAI"
        MACOSX_BUNDLE_GUI_IDENTIFIER "com.liaoboxuan.localai"
        MACOSX_BUNDLE_ICON_FILE "localai_dark.icns")
    set_target_properties(CloudAI_Native PROPERTIES
        MACOSX_BUNDLE TRUE
        OUTPUT_NAME "CloudAI"
        MACOSX_BUNDLE_BUNDLE_NAME "CloudAI"
        MACOSX_BUNDLE_GUI_IDENTIFIER "com.liaoboxuan.cloudai"
        MACOSX_BUNDLE_ICON_FILE "localai_dark.icns")
    set_target_properties(LocalAI_SE_Native PROPERTIES
        MACOSX_BUNDLE TRUE
        OUTPUT_NAME "LocalAI_SE"
        MACOSX_BUNDLE_BUNDLE_NAME "LocalAI SE"
        MACOSX_BUNDLE_GUI_IDENTIFIER "com.liaoboxuan.localaise"
        MACOSX_BUNDLE_ICON_FILE "localai_dark.icns")
    foreach(_target LocalAI_Native LocalAI_SE_Native)
        add_custom_command(TARGET ${_target} POST_BUILD
            COMMAND ${CMAKE_COMMAND} -E make_directory "$<TARGET_BUNDLE_CONTENT_DIR:${_target}>/Resources"
            COMMAND ${CMAKE_COMMAND} -E make_directory "$<TARGET_BUNDLE_CONTENT_DIR:${_target}>/Helpers"
            COMMAND ${CMAKE_COMMAND} -E copy_if_different
                "$<TARGET_FILE:PythonMigrationTranslator>"
                "$<TARGET_BUNDLE_CONTENT_DIR:${_target}>/Helpers/PythonMigrationTranslator"
            VERBATIM)
        if(LOCALAI_BUNDLE_LLAMA_SOURCE)
            add_custom_command(TARGET ${_target} POST_BUILD
                COMMAND ${CMAKE_COMMAND} -E tar
                    "cf"
                    "$<TARGET_BUNDLE_CONTENT_DIR:${_target}>/Resources/llama.cpp-source.tar"
                    --format=gnutar
                    -C "${LOCALAI_SHARED_THIRD_PARTY_DIR}" llama.cpp
                VERBATIM)
        endif()
        if(EXISTS "${LOCALAI_SHARED_RUNTIME_DIR}/llama.cpp")
            add_custom_command(TARGET ${_target} POST_BUILD
                COMMAND ${CMAKE_COMMAND} -E copy_directory
                    "${LOCALAI_SHARED_RUNTIME_DIR}/llama.cpp"
                    "$<TARGET_BUNDLE_CONTENT_DIR:${_target}>/Resources/runtime/llama.cpp"
                VERBATIM)
        endif()
    endforeach()
    if(LOCALAI_ENABLE_PYTHON_RUNTIME AND EXISTS "${LOCALAI_ARTIFACTS_DIR}/LocalAI")
        add_custom_command(TARGET LocalAI_Native POST_BUILD
            COMMAND ${CMAKE_COMMAND} -E copy_directory
                "${LOCALAI_ARTIFACTS_DIR}/LocalAI"
                "$<TARGET_BUNDLE_CONTENT_DIR:LocalAI_Native>/Resources/python-runtime/LocalAI"
            VERBATIM)
    endif()
    if(LOCALAI_ENABLE_PYTHON_RUNTIME AND EXISTS "${LOCALAI_ARTIFACTS_DIR}/LocalAI-SE")
        add_custom_command(TARGET LocalAI_SE_Native POST_BUILD
            COMMAND ${CMAKE_COMMAND} -E copy_directory
                "${LOCALAI_ARTIFACTS_DIR}/LocalAI-SE"
                "$<TARGET_BUNDLE_CONTENT_DIR:LocalAI_SE_Native>/Resources/python-runtime/LocalAI_SE"
            VERBATIM)
    endif()
endif()

include(CTest)
if(BUILD_TESTING)
    add_executable(NativeAppConfigTests
        tests/AppConfigTests.cpp
        src/AppConfig.cpp
        src/AppConfig.hpp
    )
    target_include_directories(NativeAppConfigTests PRIVATE src)
    target_link_libraries(NativeAppConfigTests PRIVATE Qt6::Core)
    add_test(NAME NativeAppConfigTests COMMAND NativeAppConfigTests)

    add_executable(PythonTranslatorEnvironmentProbe
        tests/PythonTranslatorEnvironmentProbe.cpp
    )
    add_test(NAME PythonTranslatorSelfCheck
        COMMAND PythonMigrationTranslator --self-check)
    add_test(NAME PythonTranslatorBridgeTest
        COMMAND PythonMigrationTranslator --run
            $<TARGET_FILE:PythonTranslatorEnvironmentProbe> --force-llama-cpp)
endif()

install(TARGETS LocalAI_Native CloudAI_Native LocalAI_SE_Native
    RUNTIME DESTINATION .
    BUNDLE DESTINATION .)
install(TARGETS PythonMigrationTranslator RUNTIME DESTINATION helpers)
if(LOCALAI_ENABLE_PYTHON_RUNTIME AND LOCALAI_PYTHON_RUNTIME_ROOT)
    install(DIRECTORY "${LOCALAI_PYTHON_RUNTIME_ROOT}/" DESTINATION python-runtime)
endif()

set(CPACK_PACKAGE_NAME "LocalAI-Native")
set(CPACK_PACKAGE_VERSION "1.0")
include(CPack)
