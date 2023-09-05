#
# Copyright OpenEmbedded Contributors
#
# SPDX-License-Identifier: MIT
#

cmake_minimum_required(VERSION 3.22)

project(cmake-example
  VERSION 1.0.0
  LANGUAGES CXX
)

option(BUILD_SHARED_LIBS "Build using shared libraries" ON)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED On)
set(CMAKE_CXX_EXTENSIONS Off)

include(GNUInstallDirs)

# Find json-c
# find_package(PkgConfig REQUIRED)
# pkg_check_modules(JSONC REQUIRED json-c)
find_package(json-c)

# A simple library linking json-c library found by pkgconfig
add_library(cmake-example-lib cpp-example-lib.cpp cpp-example-lib.hpp)
set_target_properties(cmake-example-lib PROPERTIES 
    VERSION ${PROJECT_VERSION}
    SOVERSION ${PROJECT_VERSION_MAJOR}
)
target_link_libraries(cmake-example-lib PRIVATE json-c::json-c)
# target_link_libraries(cmake-example-lib ${JSONC_LIBRARIES})
# target_include_directories(cmake-example-lib PUBLIC ${JSONC_INCLUDE_DIRS})
# target_compile_options(cmake-example-lib PUBLIC ${JSONC_CFLAGS_OTHER})
install(TARGETS cmake-example-lib
    INCLUDES DESTINATION ${CMAKE_INSTALL_INCLUDEDIR}
    ARCHIVE DESTINATION ${CMAKE_INSTALL_LIBDIR}
    LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR}
)

# A simple executable linking the library
add_executable(cmake-example cpp-example.cpp)
target_link_libraries(cmake-example PRIVATE cmake-example-lib)

install(TARGETS cmake-example
    RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR}
)

# A simple test executable for testing the library
add_executable(test-cmake-example test-cpp-example.cpp)
target_link_libraries(test-cmake-example PRIVATE cmake-example-lib)

install(TARGETS test-cmake-example
    RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR}
)

include(CTest)
add_test(NAME test-cmake-example COMMAND test-cmake-example)
