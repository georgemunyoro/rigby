cmake_minimum_required(VERSION 3.5)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

project(
    rigby
    VERSION 0.0.0
    DESCRIPTION "RGB LED controller"
    LANGUAGES CXX C
)

set(CMAKE_CXX_STANDARD 20)

# ImGui
set(IMGUI_DIR "./lib/imgui")
include_directories(${IMGUI_DIR} ${IMGUI_DIR}/backends)

# SDL2
set(SDL2_DIR "./lib/SDL")
add_subdirectory(${SDL2_DIR})
include_directories(${SDL2_DIR}/include)

# spdlog
set(SPDLOG_DIR "./lib/spdlog")
add_subdirectory(${SPDLOG_DIR})
include_directories(${SPDLOG_DIR}/include)

# OpenRGB SDK
set(ORGB_SDK_DIR "./lib/OpenRGB-cppSDK")
add_subdirectory(${ORGB_SDK_DIR})
include_directories(${ORGB_SDK_DIR}/include)

add_library(
    application
    src/application.cpp src/application.h
    src/device-controller.cpp src/device-controller.h
)

add_executable(
    rigby
    src/main.cpp
    ${IMGUI_DIR}/backends/imgui_impl_sdl2.cpp
    ${IMGUI_DIR}/backends/imgui_impl_sdlrenderer2.cpp
    ${IMGUI_DIR}/imgui.cpp
    ${IMGUI_DIR}/imgui_draw.cpp
    ${IMGUI_DIR}/imgui_demo.cpp
    ${IMGUI_DIR}/imgui_tables.cpp
    ${IMGUI_DIR}/imgui_widgets.cpp
)

target_link_libraries(
    rigby
    application
    SDL2::SDL2
    spdlog
    orgbsdk
)
