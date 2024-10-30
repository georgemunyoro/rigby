cmake_minimum_required(VERSION 3.5)

project(
    rigby
    VERSION 0.0.0
    DESCRIPTION "RGB LED controller"
    LANGUAGES CXX C
)

set(CMAKE_CXX_STANDARD 20)

add_executable(
    rigby
    src/main.cpp
)

target_link_libraries(
    rigby
)
