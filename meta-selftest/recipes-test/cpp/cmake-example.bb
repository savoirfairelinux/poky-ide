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

# This is a workaround for packages with sources in-tree
# Without this bitbake ... does not work after running the install script.
# newenv['PSEUDO_IGNORE_PATHS'] = newenv['PSEUDO_IGNORE_PATHS'] + ""
# path mismatch [3 links]: ino 37529096 db '/home/adrian/projects/oss/meta-yocto-upstream/projects/poky-oe-glibc-sd/tmp/work/cortexa57-poky-linux/cmake-example/1.0-r0/package/usr/src/debug/cmake-example/1.0-r0/oe-local-files/cmake-example-lib.cpp' req '/home/adrian/projects/oss/meta-yocto-upstream/projects/poky-oe-glibc-sd/workspace/sources/cmake-example/oe-local-files/cmake-example-lib.cpp'.
PACKAGE_DEBUG_SPLIT_STYLE = "debug-without-src"
