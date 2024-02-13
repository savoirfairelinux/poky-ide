# SPDX-License-Identifier: MIT
#
# Copyright Leica Geosystems AG
#

# systemd-sysext [1] has a simple mechanism for version compatibility:
# the extension to be loaded has to contain a
# /usr/lib/extension-release.d/extension-release.NAME
# with "NAME" *exactly* matching the filename of the extensions
# raw-device filename/
#
# from the extension-release file the "ID" and "VERSION_ID" fields are
# matched against the etc/os-release and the extension is only "merged"
# if no mismatches between NAME, ID, and VERSION_ID.
#
# Link: https://www.freedesktop.org/software/systemd/man/latest/systemd-sysext.html

inherit image

IMAGE_NAME_SUFFIX = ".sysext"
EXTENSION_NAME = "${IMAGE_NAME}${IMAGE_NAME_SUFFIX}.${IMAGE_FSTYPES}"

DEPENDS += " os-release"

sysext_image_mangle_rootfs() {
    R=${IMAGE_ROOTFS}

    # pull a copy of the rootfs version information, which systemd-sysext matches against
    cp -av ${RECIPE_SYSROOT}/${nonarch_libdir}/os-release ${WORKDIR}/extension-release.base

    echo 'EXTENSION_RELOAD_MANAGER=1' >> ${WORKDIR}/extension-release.base

    install -d $R${nonarch_libdir}/extension-release.d
    install -m 0644 ${WORKDIR}/extension-release.base \
        $R${nonarch_libdir}/extension-release.d/extension-release.${EXTENSION_NAME}

    # disable systemd-sysext's strict name checking, so that the image file can be renamed, while still being 'merge'-able
    setfattr -n user.extension-release.strict -v false \
        $R${nonarch_libdir}/extension-release.d/extension-release.${EXTENSION_NAME}
}

ROOTFS_POSTPROCESS_COMMAND += " sysext_image_mangle_rootfs; "
