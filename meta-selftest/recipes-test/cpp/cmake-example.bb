#
# Copyright OpenEmbedded Contributors
#
# SPDX-License-Identifier: MIT
#

SUMMARY = "A C++ example compiled with cmake."

inherit cmake

require cpp-example.inc

SRC_URI += "\
    file://CMakeLists.txt \
"

FILES:${PN}-ptest += "${bindir}/test-cmake-example"
