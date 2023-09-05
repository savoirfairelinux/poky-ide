#
# Copyright OpenEmbedded Contributors
#
# SPDX-License-Identifier: MIT
#

SUMMARY = "A C++ example compiled with meson."

inherit pkgconfig meson

require cpp-example.inc

SRC_URI += "\
    file://meson.build \
"

FILES:${PN}-ptest += "${bindir}/test-mesonex"
