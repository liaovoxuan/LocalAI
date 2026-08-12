cmake_minimum_required(VERSION 3.20)

project(LocalAI_Native LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)

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

add_executable(PythonMigrationTranslator
    src/PythonMigrationTranslator.cpp
)
