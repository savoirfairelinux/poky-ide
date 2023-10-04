#! /usr/bin/env python3
#
# Copyright (C) 2023 Siemens AG
#
# SPDX-License-Identifier: GPL-2.0-only
#

"""Devtool ide plugin"""

import os
import stat
import sys
import logging
import json
import re
import shutil
from argparse import RawTextHelpFormatter
from enum import IntEnum, auto

import bb
from devtool import exec_build_env_command, setup_tinfoil, check_workspace_recipe, DevtoolError, parse_recipe
from devtool.standard import get_real_srctree

SHARED_SYSROOT_RECIPES = ['none', 'meta-ide-support', 'build-sysroots']
SUPPORTED_IDES = ['code', 'none']

logger = logging.getLogger('devtool')


class TargetDevice:
    """SSH remote login parameters"""

    def __init__(self, args):
        self.extraoptions = ''
        if args.no_host_check:
            self.extraoptions += '-o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no'
        self.ssh_sshexec = 'ssh'
        if args.ssh_exec:
            self.ssh_sshexec = args.ssh_exec
        self.ssh_port = ''
        if args.port:
            self.ssh_port = "-p %s" % args.port
        if args.key:
            self.extraoptions += ' -i %s' % args.key

        self.target = args.target
        target_sp = args.target.split('@')
        if len(target_sp) == 1:
            self.login = ""
            self.host = target_sp[0]
        elif len(target_sp) == 2:
            self.login = target_sp[0]
            self.host = target_sp[1]
        else:
            logger.error("Invalid target argument: %s" % args.target)


class RecipeNative:
    def __init__(self, name, target_arch=None):
        self.name = name
        self.target_arch = target_arch
        self.bootstrap_tasks = [self.name + ':do_addto_recipe_sysroot']
        self.staging_bindir_native = None
        self.target_sys = None
        self.__native_bin = None

    def initialize(self, config, workspace, tinfoil):
        recipe_d = parse_recipe(
            config, tinfoil, self.name, appends=True, filter_workspace=False)
        if not recipe_d:
            raise DevtoolError("Parsing %s recipe failed" % self.name)
        self.staging_bindir_native = os.path.realpath(
            recipe_d.getVar('STAGING_BINDIR_NATIVE'))
        self.target_sys = recipe_d.getVar('TARGET_SYS')

    @property
    def native_bin(self):
        if not self.__native_bin:
            raise DevtoolError("native binary name is not defined.")
        return self.__native_bin


class RecipeGdbCross(RecipeNative):
    def __init__(self, args, target_arch, target_device, gdbserver_multi=True):
        super().__init__('gdb-cross-' + target_arch, target_arch)
        self.target_device = target_device
        self.gdb = None
        self.gdbserver_port_next = int(args.gdbserver_port_start)
        self.gdbserver_multi = gdbserver_multi
        self.config_db = {}

    def initialize(self, config, workspace, tinfoil):
        super().initialize(config, workspace, tinfoil)
        gdb_bin = self.target_sys + '-gdb'
        gdb_path = os.path.join(
            self.staging_bindir_native, self.target_sys, gdb_bin)
        self.gdb = gdb_path

    @property
    def host(self):
        return self.target_device.host

    def __gdbserver_start_cmd(self, binary, port):
        if self.gdbserver_multi:
            gdbserver_cmd = "/usr/bin/gdbserver --multi :%s" % (
                port)
        else:
            gdbserver_cmd = "/usr/bin/gdbserver --once :%s %s" % (
                port, binary)
        return "%s %s %s %s 'sh -c \"%s\"'" % (
            self.target_device.ssh_sshexec, self.target_device.ssh_port, self.target_device.extraoptions, self.target_device.target, gdbserver_cmd)

    def setup_gdbserver_config(self, binary, script_dir):
        if binary in self.config_db:
            raise DevtoolError(
                "gdbserver config for binary %s is already generated" % binary)

        port = self.gdbserver_port_next
        self.gdbserver_port_next += 1
        config_entry = {
            "port": port,
        }
        if script_dir:
            cmd_lines = ['#!/bin/sh']
            cmd_lines.append(self.__gdbserver_start_cmd(binary, port))
            binary_name_pretty = binary.replace(os.sep, '-')
            start_script_name = 'gdbserver_start_%d_%s' % (
                port, binary_name_pretty)
            if self.gdbserver_multi:
                start_script_name += "_m"
            start_script_file = os.path.join(script_dir, start_script_name)
            config_entry['gdbserver_start_script'] = start_script_file
            config_entry['pretty_id'] = 'gdbserver start %d %s' % (
                port, binary)

            bb.utils.mkdirhier(script_dir)
            with open(start_script_file, 'w') as script_f:
                script_f.write(os.linesep.join(cmd_lines))
                script_f.write(os.linesep)
            st = os.stat(start_script_file)
            os.chmod(start_script_file, st.st_mode | stat.S_IEXEC)

        self.config_db[binary] = config_entry
        return config_entry

    def get_gdbserver_start_scripts(self):
        for conf in self.config_db.values():
            yield (conf['pretty_id'], conf['gdbserver_start_script'])

    def get_gdbserver_pretty_id(self, binary):
        return self.config_db[binary]['pretty_id']

    def get_gdbserver_port(self, binary):
        return self.config_db[binary]['port']

    def get_gdbserver_start_script(self, binary):
        return self.config_db[binary]['gdbserver_start_script']


class RecipeImage:
    """Handle some image recipe related properties"""

    def __init__(self, name):
        self.combine_dbg_image = False
        self.gdbserver_missing = False
        self.debuginfod = False
        self.name = name
        self.package_debug_split_style = None
        self.rootfs = None
        self.__rootfs_dbg = None
        self.bootstrap_tasks = [self.name + ':do_build']

    def initialize(self, config, tinfoil):
        image_d = parse_recipe(
            config, tinfoil, self.name, appends=True, filter_workspace=False)
        if not image_d:
            raise DevtoolError(
                "Parsing image recipe %s failed" % self.name)
        if 'debuginfod' in image_d.getVar('DISTRO_FEATURES').split():
            # image_config.debuginfod = True
            logger.warning("Support for debuginfod is not implemented yet.")

        self.package_debug_split_style = image_d.getVar(
            'PACKAGE_DEBUG_SPLIT_STYLE')

        workdir = image_d.getVar('WORKDIR')
        self.rootfs = os.path.join(workdir, 'rootfs')
        if image_d.getVar('IMAGE_GEN_DEBUGFS') == "1":
            self.__rootfs_dbg = os.path.join(workdir, 'rootfs-dbg')

        self.combine_dbg_image = bb.data.inherits_class(
            'image-combined-dbg', image_d)

        self.gdbserver_missing = 'gdbserver' not in image_d.getVar(
            'IMAGE_INSTALL')

    @property
    def debug_support(self):
        return bool(self.debuginfod or self.rootfs_dbg)

    @property
    def rootfs_dbg(self):
        if self.__rootfs_dbg and os.path.isdir(self.__rootfs_dbg):
            return self.__rootfs_dbg
        return None

    def solib_search_path_rootfs(self):
        """Search for folders with shared libraries in the rootfs and rootfs-dbg

        This is based on the assumption that the PACKAGE_DEBUG_SPLIT_STYLE variable from the image
        is the global setting which is used by most packages. Even if this variable does not seem
        to make sense in the image context.
        """
        rootfs_solib_search_path = []
        rootfs_dbg_solib_search_path = []
        if self.package_debug_split_style in ['debug-with-srcpkg', '.debug']:
            if self.combine_dbg_image:
                rootfs_dbg_solib_search_path = [
                    "/lib", "/lib/.debug", "/usr/lib", "/usr/lib/.debug"]
            else:
                logger.warn(
                    'Adding IMAGE_CLASSES += "image-combined-dbg" offers better remote debugging experience.')
                rootfs_solib_search_path = [
                    "/lib", "/usr/lib"]
                rootfs_dbg_solib_search_path = [
                    "/lib/.debug", "/usr/lib/.debug"]
        elif self.package_debug_split_style == 'debug-file-directory':
            rootfs_dbg_solib_search_path = ["/usr/lib/debug"]
        else:
            logger.warning(
                "Cannot find solib search path for a rootfs built with PACKAGE_DEBUG_SPLIT_STYLE=%s." % self.package_debug_split_style)

        sym_dirs = []
        for dbgdir in rootfs_solib_search_path:
            sym_dirs.append(os.path.join(
                self.rootfs, dbgdir.lstrip('/')))
        for dbgdir in rootfs_dbg_solib_search_path:
            sym_dirs.append(os.path.join(
                self.rootfs_dbg, dbgdir.lstrip('/')))

        return sym_dirs


class RecipeMetaIdeSupport:
    """Handle some meta-ide-support recipe related properties"""

    def __init__(self):
        self.bootstrap_tasks = ['meta-ide-support:do_build']
        self.topdir = None
        self.datadir = None
        self.deploy_dir_image = None
        self.build_sys = None
        # From toolchain-scripts
        self.real_multimach_target_sys = None

    def initialize(self, config, tinfoil):
        meta_ide_support_d = parse_recipe(
            config, tinfoil, 'meta-ide-support', appends=True, filter_workspace=False)
        if not meta_ide_support_d:
            raise DevtoolError("Parsing meta-ide-support recipe failed")

        self.topdir = meta_ide_support_d.getVar('TOPDIR')
        self.datadir = meta_ide_support_d.getVar('datadir')
        self.deploy_dir_image = meta_ide_support_d.getVar(
            'DEPLOY_DIR_IMAGE')
        self.build_sys = meta_ide_support_d.getVar('BUILD_SYS')
        self.real_multimach_target_sys = meta_ide_support_d.getVar(
            'REAL_MULTIMACH_TARGET_SYS')


class RecipeBuildSysroots:
    def __init__(self):
        self.standalone_sysroot = None
        self.standalone_sysroot_native = None
        self.bootstrap_tasks = ['build-sysroots:do_build']

    def initialize(self, config, tinfoil):
        build_sysroots_d = parse_recipe(
            config, tinfoil, 'build-sysroots', appends=True, filter_workspace=False)
        if not build_sysroots_d:
            raise DevtoolError("Parsing build-sysroots recipe failed")
        self.standalone_sysroot = build_sysroots_d.getVar(
            'STANDALONE_SYSROOT')
        self.standalone_sysroot_native = build_sysroots_d.getVar(
            'STANDALONE_SYSROOT_NATIVE')


class SharedSysrootsEnv:
    def __init__(self):
        self.ide_support = None
        self.build_sysroots = None

    def initialize(self, ide_support, build_sysroots):
        self.ide_support = ide_support
        self.build_sysroots = build_sysroots

    def __vscode_update_kits(self):
        """Expose the toolchain of the dSDK"""
        datadir = self.ide_support.datadir
        deploy_dir_image = self.ide_support.deploy_dir_image
        real_multimach_target_sys = self.ide_support.real_multimach_target_sys
        standalone_sysroot_native = self.build_sysroots.standalone_sysroot_native
        vscode_ws_path = os.path.join(
            os.environ['HOME'], '.local', 'share', 'CMakeTools')
        cmake_kits_path = os.path.join(vscode_ws_path, 'cmake-tools-kits.json')
        oecmake_generator = "Ninja"
        env_script = os.path.join(
            deploy_dir_image, 'environment-setup-' + real_multimach_target_sys)

        logger.info("updating %s" % cmake_kits_path)

        if not os.path.isdir(vscode_ws_path):
            os.makedirs(vscode_ws_path)
        cmake_kits_old = []
        if os.path.exists(cmake_kits_path):
            with open(cmake_kits_path, 'r', encoding='utf-8') as cmake_kits_file:
                cmake_kits_old = json.load(cmake_kits_file)
        cmake_kits = cmake_kits_old.copy()

        cmake_kit_new = {
            "name": "OE " + real_multimach_target_sys,
            "environmentSetupScript": env_script,
            "toolchainFile": standalone_sysroot_native + datadir + "/cmake/OEToolchainConfig.cmake",
            "preferredGenerator": {
                "name": oecmake_generator
            }
        }

        def merge_kit(cmake_kits, cmake_kit_new):
            i = 0
            while i < len(cmake_kits):
                if 'environmentSetupScript' in cmake_kits[i] and \
                        cmake_kits[i]['environmentSetupScript'] == cmake_kit_new['environmentSetupScript']:
                    cmake_kits[i] = cmake_kit_new
                    return
                i += 1
            cmake_kits.append(cmake_kit_new)
        merge_kit(cmake_kits, cmake_kit_new)

        if cmake_kits != cmake_kits_old:
            bb.note("Updating: %s" % cmake_kits_path)
            with open(cmake_kits_path, 'w', encoding='utf-8') as cmake_kits_file:
                json.dump(cmake_kits, cmake_kits_file, indent=4)

    def setup_ide(self, args):
        if args.ide == 'code':
            self.__vscode_update_kits()


class BuildTool(IntEnum):
    UNDEFINED = auto()
    CMAKE = auto()
    MESON = auto()


class RecipeModified:

    def __init__(self, name):
        self.name = name
        self.bootstrap_tasks = [name + ':do_install']
        # workspace
        self.real_srctree = None
        self.srctree = None
        self.temp_dir = None
        self.bbappend = None
        # recipe variables from d.getVar
        self.b = None
        self.base_libdir = None
        self.bpn = None
        self.d = None
        self.fakerootcmd = None
        self.fakerootenv = None
        self.libdir = None
        self.max_process = None
        self.package_arch = None
        self.path = None
        self.recipe_sysroot = None
        self.recipe_sysroot_native = None
        self.staging_incdir = None
        self.strip_cmd = None
        self.target_arch = None
        self.workdir = None
        # replicate bitbake build environment
        self.__exported_vars = None
        self.cmd_compile = None
        # main build tool used by this recipe
        self.build_tool = BuildTool.UNDEFINED
        # build_tool = cmake
        self.oecmake_generator = None
        self.__cmake_cache_vars = None
        # build_tool = meson
        self.meson_buildtype = None
        self.meson_wrapper = None
        self.mesonopts = None
        self.extra_oemeson = None
        self.meson_cross_file = None
        # vscode
        self.dot_code_dir = None

    def initialize(self, config, workspace, tinfoil):
        recipe_d = parse_recipe(
            config, tinfoil, self.name, appends=True, filter_workspace=False)
        if not recipe_d:
            raise DevtoolError("Parsing %s recipe failed" % self.name)

        # Verify this recipe is built as externalsrc setup by devtool modify
        workspacepn = check_workspace_recipe(
            workspace, self.name, bbclassextend=True)
        self.srctree = workspace[workspacepn]['srctree']
        # Need to grab this here in case the source is within a subdirectory
        self.real_srctree = get_real_srctree(
            self.srctree, recipe_d.getVar('S'), recipe_d.getVar('WORKDIR'))
        self.bbappend = workspace[workspacepn]['bbappend']

        self.temp_dir = os.path.join(config.workspace_path, 'temp', self.name)
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

        self.b = recipe_d.getVar('B')
        self.base_libdir = recipe_d.getVar('base_libdir')
        self.bpn = recipe_d.getVar('BPN')
        self.d = recipe_d.getVar('D')
        self.fakerootcmd = recipe_d.getVar('FAKEROOTCMD')
        self.fakerootenv = recipe_d.getVar('FAKEROOTENV')
        self.libdir = recipe_d.getVar('libdir'),
        self.max_process = int(recipe_d.getVar(
            "BB_NUMBER_THREADS") or os.cpu_count() or 1)
        self.package_arch = recipe_d.getVar('PACKAGE_ARCH')
        self.path = recipe_d.getVar('PATH')
        self.recipe_sysroot = os.path.realpath(
            recipe_d.getVar('RECIPE_SYSROOT'))
        self.recipe_sysroot_native = os.path.realpath(
            recipe_d.getVar('RECIPE_SYSROOT_NATIVE'))
        self.staging_incdir = os.path.realpath(
            recipe_d.getVar('STAGING_INCDIR'))
        self.strip_cmd = recipe_d.getVar('STRIP')
        self.target_arch = recipe_d.getVar('TARGET_ARCH')
        self.workdir = os.path.realpath(recipe_d.getVar('WORKDIR'))

        self.__init_exported_variables(recipe_d)

        if bb.data.inherits_class('cmake', recipe_d):
            self.oecmake_generator = recipe_d.getVar('OECMAKE_GENERATOR')
            self.__init_cmake_preset_cache(recipe_d)
            self.build_tool = BuildTool.CMAKE
        elif bb.data.inherits_class('meson', recipe_d):
            self.meson_buildtype = recipe_d.getVar('MESON_BUILDTYPE')
            self.mesonopts = recipe_d.getVar('MESONOPTS')
            self.extra_oemeson = recipe_d.getVar('EXTRA_OEMESON')
            self.meson_cross_file = recipe_d.getVar('MESON_CROSS_FILE')
            self.build_tool = BuildTool.MESON

        self.dot_code_dir = os.path.join(self.srctree, '.vscode')

    def append_to_bbappend(self, append_text):
        with open(self.bbappend, 'a') as bbap:
            bbap.write(append_text)

    def remove_from_bbappend(self, append_text):
        with open(self.bbappend, 'r') as bbap:
            text = bbap.read()
        new_text = text.replace(append_text, '')
        with open(self.bbappend, 'w') as bbap:
            bbap.write(new_text)

    def debug_build_config(self, args):
        """Explicitely set for example CMAKE_BUILD_TYPE to Debug if not defined otherwise"""
        if self.build_tool == BuildTool.CMAKE:
            append_text = os.linesep + \
                'OECMAKE_ARGS:append = " -DCMAKE_BUILD_TYPE:STRING=Debug"' + os.linesep
            if args.debug_build_config and not 'CMAKE_BUILD_TYPE' in self.__cmake_cache_vars:
                self.__cmake_cache_vars['CMAKE_BUILD_TYPE'] = {
                    "type": "STRING",
                    "value": "Debug",
                }
                self.append_to_bbappend(append_text)
            elif 'CMAKE_BUILD_TYPE' in self.__cmake_cache_vars:
                del self.__cmake_cache_vars['CMAKE_BUILD_TYPE']
                self.remove_from_bbappend(append_text)
        elif self.build_tool == BuildTool.MESON:
            append_text = os.linesep + 'MESON_BUILDTYPE = "debug"' + os.linesep
            if args.debug_build_config and self.meson_buildtype != "debug":
                self.mesonopts.replace(
                    '--buildtype ' + self.meson_buildtype, '--buildtype debug')
                self.append_to_bbappend(append_text)
            elif self.meson_buildtype == "debug":
                self.mesonopts.replace(
                    '--buildtype debug', '--buildtype plain')
                self.remove_from_bbappend(append_text)
        elif args.debug_build_config:
            logger.warn(
                "--debug-build-config is not implemented for this build tool yet.")

    def solib_search_path_sysroot(self):
        return [os.path.join(self.recipe_sysroot, p) for p in ['lib', 'usr/lib']]

    def solib_search_path(self, image):
        return image.solib_search_path_rootfs() + self.solib_search_path_sysroot()

    def solib_search_path_str(self, image):
        return ':'.join(self.solib_search_path(image))

    def __init_exported_variables(self, d):
        """Find all variables with export flag set."""
        exported_vars = {}

        vars = (key for key in d.keys() if not key.startswith(
            "__") and not d.getVarFlag(key, "func", False))
        for var in vars:
            func = d.getVarFlag(var, "func", False)
            if d.getVarFlag(var, 'python', False) and func:
                continue
            export = d.getVarFlag(var, "export", False)
            unexport = d.getVarFlag(var, "unexport", False)
            if not export and not unexport and not func:
                continue
            if unexport:
                continue

            val = d.getVar(var)
            if val is None:
                continue
            if set(var) & set("-.{}+"):
                logger.warn(
                    "Warning: Found invalid character in variable name %s", str(var))
                continue
            varExpanded = d.expand(var)
            val = str(val)

            if varExpanded.startswith("BASH_FUNC_"):
                varExpanded = varExpanded[10:-2]
                val = val[3:]  # Strip off "() "
                logger.warn("Warning: BASH_FUNC_ is not exported to cmake presets (%s() %s)" % (
                    varExpanded, val))
                continue

            if func:
                code_line = "line: {0}, file: {1}\n".format(
                    d.getVarFlag(var, "lineno", False),
                    d.getVarFlag(var, "filename", False))
                val = val.rstrip('\n')
                logger.warn("Warning: exported shell function %s() is not exported (%s)" %
                            (varExpanded, code_line))
                continue

            if export:
                exported_vars[varExpanded] = val.strip()
                continue

        self.__exported_vars = exported_vars

    def __init_cmake_preset_cache(self, d):
        """Replicate the cmake configure arguments with all details to share on build folder between bitbake and SDK."""
        site_file = os.path.join(self.workdir, 'site-file.cmake')
        if os.path.exists(site_file):
            print("Warning: site-file.cmake is not supported")

        cache_vars = {}
        oecmake_args = d.getVar('OECMAKE_ARGS').split()
        extra_oecmake = d.getVar('EXTRA_OECMAKE').split()
        for param in oecmake_args + extra_oecmake:
            d_pref = "-D"
            if param.startswith(d_pref):
                param = param[len(d_pref):]
            else:
                print("Error: expected a -D")
            param_s = param.split('=', 1)
            param_nt = param_s[0].split(':', 1)

            def handle_undefined_variable(var):
                if var.startswith('${') and var.endswith('}'):
                    return ''
                else:
                    return var
            # Example: FOO=ON
            if len(param_nt) == 1:
                cache_vars[param_s[0]] = handle_undefined_variable(param_s[1])
            # Example: FOO:PATH=/tmp
            elif len(param_nt) == 2:
                cache_vars[param_nt[0]] = {
                    "type": param_nt[1],
                    "value": handle_undefined_variable(param_s[1]),
                }
            else:
                print("Error: cannot parse %s" % param)
        self.__cmake_cache_vars = cache_vars

    def __cmake_preset(self):
        toolchain_file = os.path.join(self.workdir, 'toolchain.cmake')
        preset_name = self.bpn + "-" + self.package_arch
        preset_display_name = self.bpn + ": " + self.package_arch
        cmake_executable = os.path.join(
            self.recipe_sysroot_native, 'usr', 'bin', 'cmake')
        self.cmd_compile = cmake_executable + " --build --preset " + preset_name

        preset_dict_configure = {
            "name": preset_name,
            "displayName": preset_display_name,
            "description": "Bitbake build environment for the recipe %s compiled for %s" % (self.bpn, self.package_arch),
            "binaryDir": self.b,
            "generator": self.oecmake_generator,
            "toolchainFile": toolchain_file,
            "cacheVariables": self.__cmake_cache_vars,
            "environment": self.__exported_vars,
            "cmakeExecutable": cmake_executable
        }

        preset_dict_build = {
            "name": preset_name,
            "displayName": preset_display_name,
            "description": "Bitbake build environment for the recipe %s compiled for %s" % (self.bpn, self.package_arch),
            "configurePreset": preset_name,
            "inheritConfigureEnvironment": True
        }

        preset_dict_test = {
            "name": preset_name,
            "displayName": preset_display_name,
            "description": "Bitbake build environment for the recipe %s compiled for %s" % (self.bpn, self.package_arch),
            "configurePreset": preset_name,
            "inheritConfigureEnvironment": True
        }

        preset_dict = {
            "version": 3,  # cmake 3.21, backward compatible with kirkstone
            "configurePresets": [preset_dict_configure],
            "buildPresets": [preset_dict_build],
            "testPresets": [preset_dict_test]
        }

        logger.info("generating cmake preset for recipe %s" % self.bpn)
        preset_file = os.path.join(self.real_srctree, 'CMakeUserPresets.json')
        with open(preset_file, 'w') as outfile:
            json.dump(preset_dict, outfile, indent=4)

    @staticmethod
    def update_json_file(dot_code_dir, json_file, update_dict):
        json_path = os.path.join(dot_code_dir, json_file)
        logger.info("Updating vscode %s (%s)" % (json_file, json_path))
        if not os.path.exists(dot_code_dir):
            os.makedirs(dot_code_dir)
        try:
            with open(json_path) as f:
                orig_dict = json.load(f)
        except json.decoder.JSONDecodeError:
            logger.info(
                "Decoding %s failed. Probably because of comments in the json file" % json_path)
            orig_dict = {}
        except FileNotFoundError:
            orig_dict = {}
        orig_dict.update(update_dict)
        with open(json_path, 'w') as f:
            json.dump(orig_dict, f, indent=4)

    def __vscode_settings_cmake(self, settings_dict):
        """Add cmake specific settings to settings.json.

        Note: most settings are passed to the cmake preset.
        """
        if self.build_tool != BuildTool.CMAKE:
            return
        settings_dict["cmake.configureOnOpen"] = True
        settings_dict["cmake.sourceDirectory"] = self.real_srctree

    def __gen_meson_wrapper(self):
        """Generate a wrapper script to call meson with cross environment"""
        bb.utils.mkdirhier(self.temp_dir)
        meson_wrapper = os.path.join(self.temp_dir, 'meson')
        meson_real = os.path.join(
            self.recipe_sysroot_native, 'usr', 'bin', 'meson.real')
        with open(meson_wrapper, 'w') as mwrap:
            mwrap.write("#!/bin/sh" + os.linesep)
            for var, val in self.__exported_vars.items():
                mwrap.write('export %s="%s"' % (var, val) + os.linesep)
            mwrap.write("unset CC CXX CPP LD AR NM STRIP" + os.linesep)
            private_temp = os.path.join(self.b, "meson-private", "tmp")
            mwrap.write('mkdir -p "%s"' % private_temp + os.linesep)
            mwrap.write('export TMPDIR="%s"' % private_temp + os.linesep)
            mwrap.write('exec "%s" "$@"' % meson_real + os.linesep)
        st = os.stat(meson_wrapper)
        os.chmod(meson_wrapper, st.st_mode | stat.S_IEXEC)
        self.meson_wrapper = meson_wrapper
        self.cmd_compile = meson_wrapper + " compile -C " + self.b

    def __vscode_settings_meson(self, settings_dict):
        if self.build_tool != BuildTool.MESON:
            return
        settings_dict["mesonbuild.mesonPath"] = self.meson_wrapper

        confopts = self.mesonopts.split()
        confopts += self.meson_cross_file.split()
        confopts += self.extra_oemeson.split()
        settings_dict["mesonbuild.configureOptions"] = confopts
        settings_dict["mesonbuild.buildFolder"] = self.b

    def vscode_settings(self):
        files_excludes = {
            "**/.git/**": True,
            "**/oe-local-files/**": True,
            "**/oe-logs/**": True,
            "**/oe-workdir/**": True,
            "**/source-date-epoch/**": True
        }
        python_exclude = [
            "**/.git/**",
            "**/oe-local-files/**",
            "**/oe-logs/**",
            "**/oe-workdir/**",
            "**/source-date-epoch/**"
        ]
        settings_dict = {
            "files.watcherExclude": files_excludes,
            "files.exclude": files_excludes,
            "python.analysis.exclude": python_exclude
        }
        self.__vscode_settings_cmake(settings_dict)
        self.__vscode_settings_meson(settings_dict)

        settings_file = 'settings.json'
        RecipeModified.update_json_file(
            self.dot_code_dir, settings_file, settings_dict)

    def __vscode_extensions_cmake(self, recommendations):
        if self.build_tool != BuildTool.CMAKE:
            return
        recommendations += [
            "twxs.cmake",
            "ms-vscode.cmake-tools",
            "ms-vscode.cpptools",
            "ms-vscode.cpptools-extension-pack",
            "ms-vscode.cpptools-themes"
        ]

    def __vscode_extensions_meson(self, recommendations):
        if self.build_tool != BuildTool.MESON:
            return
        recommendations += [
            'mesonbuild.mesonbuild',
            "ms-vscode.cpptools",
            "ms-vscode.cpptools-extension-pack",
            "ms-vscode.cpptools-themes"
        ]

    def vscode_extensions(self):
        recommendations = []
        self.__vscode_extensions_cmake(recommendations)
        self.__vscode_extensions_meson(recommendations)
        extensions_file = 'extensions.json'
        RecipeModified.update_json_file(
            self.dot_code_dir, extensions_file, {"recommendations": recommendations})

    def which(self, executable):
        bin_path = shutil.which(executable, path=self.path)
        if not bin_path:
            raise DevtoolError(
                'Cannot find %s. Probably the recipe %s is not built yet.' % (executable, self.bpn))
        return bin_path

    @staticmethod
    def vscode_intelli_sense_mode(compiler):
        unknown = False
        linux = ''
        if 'linux' in compiler:
            linux = "linux-"

        cxx = 'unknown-'
        if 'g++' in compiler:
            cxx = 'gcc-'
        elif 'clang' in compiler:
            cxx = 'clang-'
        else:
            unknown = True

        arch = 'unknown'
        if 'aarch64' in compiler:
            arch = 'arm64'
        elif 'arm' in compiler:
            arch = 'arm'
        elif 'x86_64' in compiler:
            arch = 'x64'
        elif 'i386' in compiler:
            arch = 'x86'
        else:
            unknown = True

        mode = linux + cxx + arch
        if unknown:
            logger.warn(
                "Cannot guess intelliSenseMode for compiler %s (%s)" % (compiler, mode))
            return '${default}'
        return mode

    def vscode_c_cpp_properties(self):
        properties_dict = {
            "name": "Yocto " + self.package_arch,
        }
        if self.build_tool == BuildTool.CMAKE:
            properties_dict["configurationProvider"] = "ms-vscode.cmake-tools"
        elif self.build_tool == BuildTool.MESON:
            properties_dict["configurationProvider"] = "mesonbuild.mesonbuild"

        properties_dicts = {
            "configurations": [
                properties_dict
            ],
            "version": 4
        }
        prop_file = 'c_cpp_properties.json'
        dot_code_dir = os.path.join(self.srctree, '.vscode')
        RecipeModified.update_json_file(
            dot_code_dir, prop_file, properties_dicts)

    def vscode_launch_cppdbg(self, image, gdb_cross, binary):
        gdb_cross.setup_gdbserver_config(binary, self.temp_dir)
        pretty_id = gdb_cross.get_gdbserver_pretty_id(binary)
        gdbserver_port = gdb_cross.get_gdbserver_port(binary)

        launch_config = {
            "name": pretty_id,
            "type": "cppdbg",
            "request": "launch",
            "program": os.path.join(self.d, binary.lstrip('/')),
            "stopAtEntry": True,
            "cwd": "${workspaceFolder}",
            "environment": [],
            "externalConsole": False,
            "MIMode": "gdb",
            "preLaunchTask": pretty_id,
            "miDebuggerPath": gdb_cross.gdb,
            "miDebuggerServerAddress": "%s:%d" % (gdb_cross.host, gdbserver_port)
        }

        # Search for header files in recipe-sysroot.
        src_file_map = {
            "/usr/include": os.path.join(self.recipe_sysroot, "usr", "include")
        }
        # First of all search for not stripped binaries in the image folder.
        # These binaries are copied (and optionally stripped) by deploy-target
        setup_commands = [
            {
                "description": "sysroot",
                "text": "set sysroot " + self.d
            }
        ]

        if image.rootfs_dbg:
            launch_config['additionalSOLibSearchPath'] = self.solib_search_path_str(
                image)
            src_file_map["/usr/src/debug"] = os.path.join(
                image.rootfs_dbg, "usr", "src", "debug")
        else:
            logger.warning(
                "Cannot setup debug symbols configuration for GDB. IMAGE_GEN_DEBUGFS is not enabled.")

        launch_config['sourceFileMap'] = src_file_map
        launch_config['setupCommands'] = setup_commands
        return launch_config

    @staticmethod
    def is_elf_file(file_path):
        with open(file_path, "rb") as f:
            data = f.read(4)
        if data == b'\x7fELF':
            return True
        return False

    def find_installed_binaries(self):
        """find all executable elf files in the image directory"""
        binaries = []
        d_len = len(self.d)
        re_so = re.compile('.*\.so[.0-9]*$')
        for root, _, files in os.walk(self.d, followlinks=False):
            for file in files:
                if os.path.islink(file):
                    continue
                if re_so.match(file):
                    continue
                abs_name = os.path.join(root, file)
                if os.access(abs_name, os.X_OK) and RecipeModified.is_elf_file(abs_name):
                    binaries.append(abs_name[d_len:])
        return binaries

    def vscode_launch(self, image, gdb_cross):
        binaries = self.find_installed_binaries()
        configurations = [self.vscode_launch_cppdbg(
            image, gdb_cross, binary) for binary in binaries]
        launch_dict = {
            "version": "0.2.0",
            "configurations": configurations
        }
        launch_file = 'launch.json'
        RecipeModified.update_json_file(
            self.dot_code_dir, launch_file, launch_dict)

    @staticmethod
    def get_unique_gdbinit_name(binary):
        return 'gdbinit' + binary.replace(os.sep, '-')

    def none_launch(self, image, gdb_cross):
        """generate a gdbinit file per executable"""
        binaries = self.find_installed_binaries()
        for binary in binaries:
            gdb_cross.setup_gdbserver_config(binary, self.temp_dir)
            gdbserver_port = str(gdb_cross.get_gdbserver_port(binary))
            if gdb_cross.gdbserver_multi:
                target_help = '#   gdbserver --multi :' + gdbserver_port
                remote_cmd = 'target extended-remote '
            else:
                target_help = '#   gdbserver :' + gdbserver_port + ' ' + binary
                remote_cmd = 'target remote '
            gdbinit_path = os.path.join(
                self.real_srctree, RecipeModified.get_unique_gdbinit_name(binary))

            gdbinit_lines = ['# This file is generated by devtool ide']
            gdbinit_lines.append('# On the remote target:')
            gdbinit_lines.append(target_help)
            gdbinit_lines.append('# On the build machine:')
            gdbinit_lines.append('#   cd ' + self.real_srctree)
            gdbinit_lines.append(
                '#   ' + gdb_cross.gdb + ' -ix ' + gdbinit_path)

            gdbinit_lines.append('set sysroot ' + self.d)
            gdbinit_lines.append('set substitute-path "/usr/include" "' +
                                 os.path.join(self.recipe_sysroot, 'usr', 'include') + '"')
            if image.debuginfod:
                gdbinit_lines.append('set debuginfod enabled on')
            else:
                gdbinit_lines.append('set debuginfod enabled off')
                if image.rootfs_dbg:
                    gdbinit_lines.append(
                        'set solib-search-path "' + self.solib_search_path_str(image) + '"')
                    gdbinit_lines.append('set substitute-path "/usr/src/debug" "' + os.path.join(
                        image.rootfs_dbg, 'usr', 'src', 'debug') + '"')
            gdbinit_lines.append(
                remote_cmd + gdb_cross.host + ':' + gdbserver_port)
            gdbinit_lines.append('set remote exec-file ' + binary)
            gdbinit_lines.append('run ' + os.path.join(self.d, binary))

            with open(gdbinit_path, 'w') as gdbinit_file:
                gdbinit_file.write('\n'.join(gdbinit_lines))

    def gen_fakeroot_install_script(self):
        """Run the run.do_install script from bitbake under pseudo so that it picks up the appropriate file permissions"""
        cmd_lines = ['#!/bin/sh']
        # Ensure the do compile step gets always executed without pseuso before do install
        if self.cmd_compile:
            cmd_compile = "( cd %s && %s)" % (
                self.real_srctree, self.cmd_compile)
            cmd_lines.append(cmd_compile)
        if not os.access(self.fakerootcmd, os.X_OK):
            raise DevtoolError(
                "pseudo executable %s could not be found" % self.fakerootcmd)
        run_do_install = os.path.join(self.workdir, 'temp', 'run.do_install')

        if not os.access(run_do_install, os.X_OK):
            raise DevtoolError(
                "run script does not exists: %s" % run_do_install)

        # Set up the appropriate environment
        newenv = dict(os.environ)
        for varvalue in self.fakerootenv.split():
            if '=' in varvalue:
                splitval = varvalue.split('=', 1)
                newenv[splitval[0]] = splitval[1]

        # Cleanup TMPDIR before calling do_install independently from bitbake
        # This is anyway outdated after do_install has been executed.
        # But maybe there should be a cleaner solution here.
        rm_in_workdir = ' '.join([os.path.join(self.workdir, d) for d in [
                                 "package", "packages-split", "pkgdata", "sstate-install-package", "debugsources.list", "*.spec"]])
        install_cmd = '%s /bin/sh -c "rm -rf %s/* %s && %s"' % (
            self.fakerootcmd, self.d, rm_in_workdir, run_do_install)
        for var, val in newenv.items():
            cmd_lines.append('export %s="%s"' % (var, val))
        cmd_lines.append(install_cmd)
        return self.write_script(cmd_lines, 'bb_run_do_install')

    def gen_deploy_target_script(self, args):
        """Generate a quicker (works without tinfoil) variant of devtool target-deploy"""
        cmd_lines = ['#!/usr/bin/env python3']
        cmd_lines.append('import sys')
        cmd_lines.append('devtool_sys_path = %s' % str(sys.path))
        cmd_lines.append('devtool_sys_path.reverse()')
        cmd_lines.append('for p in devtool_sys_path:')
        cmd_lines.append('    if p not in sys.path:')
        cmd_lines.append('        sys.path.insert(0, p)')
        cmd_lines.append('from devtool.deploy import deploy_cached')
        args_filter = ['debug', 'dry_run', 'key', 'no_check_space', 'no_host_check',
                       'no_preserve', 'port', 'recipename', 'show_status', 'ssh_exec', 'strip', 'target']
        filtered_args_dict = {key: value for key, value in vars(
            args).items() if key in args_filter}
        cmd_lines.append('filtered_args_dict = %s' % str(filtered_args_dict))
        cmd_lines.append('class Dict2Class(object):')
        cmd_lines.append('    def __init__(self, my_dict):')
        cmd_lines.append('        for key in my_dict:')
        cmd_lines.append('            setattr(self, key, my_dict[key])')
        cmd_lines.append('filtered_args = Dict2Class(filtered_args_dict)')
        cmd_lines.append('deploy_cached("%s", "%s", "%s", "%s", "%s", "%s", %d, "%s", "%s", filtered_args)' %
                         (self.d, self.workdir, self.path, self.strip_cmd,
                          self.libdir, self.base_libdir, self.max_process,
                          self.fakerootcmd, self.fakerootenv))
        return self.write_script(cmd_lines, 'deploy_target')

    def gen_install_deploy_script(self, args):
        cmd_lines = ['#!/bin/sh']
        cmd_lines.append(self.gen_fakeroot_install_script())
        cmd_lines.append(self.gen_deploy_target_script(args))
        return self.write_script(cmd_lines, 'install_and_deploy')

    def write_script(self, cmd_lines, script_name):
        bb.utils.mkdirhier(self.temp_dir)
        script_file = os.path.join(self.temp_dir, script_name)
        with open(script_file, 'w') as script_f:
            script_f.write(os.linesep.join(cmd_lines))
        st = os.stat(script_file)
        os.chmod(script_file, st.st_mode | stat.S_IEXEC)
        return script_file

    def vscode_tasks(self, args, gdb_cross):
        run_install_deploy = self.gen_install_deploy_script(args)
        install_task_name = "install && deploy-target %s" % self.bpn
        tasks_dict = {
            "version": "2.0.0",
            "tasks": [
                {
                    "label": install_task_name,
                    "type": "shell",
                    "command": run_install_deploy,
                    "problemMatcher": []
                }
            ]
        }
        for pretty_id, start_script in gdb_cross.get_gdbserver_start_scripts():
            tasks_dict['tasks'].append(
                {
                    "label": pretty_id,
                    "type": "shell",
                    "isBackground": True,
                    "dependsOn": [
                        install_task_name
                    ],
                    "command": start_script,
                    "problemMatcher": [
                        {
                            "pattern": [
                                {
                                    "regexp": ".",
                                    "file": 1,
                                    "location": 2,
                                    "message": 3
                                }
                            ],
                            "background": {
                                "activeOnStart": True,
                                "beginsPattern": ".",
                                "endsPattern": ".",
                            }
                        }
                    ]
                })
        tasks_file = 'tasks.json'
        RecipeModified.update_json_file(
            self.dot_code_dir, tasks_file, tasks_dict)

    def setup_ide(self, args, image, gdb_cross):
        if self.build_tool == BuildTool.CMAKE:
            self.__cmake_preset()
        if self.build_tool == BuildTool.MESON:
            self.__gen_meson_wrapper()

        if args.ide == 'code':
            self.vscode_settings()
            self.vscode_extensions()
            self.vscode_c_cpp_properties()
            if args.target:
                self.vscode_launch(image, gdb_cross)
                self.vscode_tasks(args, gdb_cross)
        if args.ide == 'none' and self.build_tool == BuildTool.CMAKE:
            self.none_launch(image, gdb_cross)

        if (image.gdbserver_missing):
            logger.warning(
                "gdbserver not installed in image. Remote debugging will not be available")


def ide_setup(args, config, basepath, workspace):
    bootstap_tasks = []

    tinfoil = setup_tinfoil(config_only=False, basepath=basepath)
    try:
        # Provide a rootfs and the corresponding debug symbols via rootfs-dbg or debuginfod
        image_config = RecipeImage(args.image)
        image_config.initialize(config, tinfoil)
        bootstap_tasks += image_config.bootstrap_tasks

        target_device = TargetDevice(args)

        sdk_env = None
        gdb_cross = None
        if args.recipename in SHARED_SYSROOT_RECIPES:
            ide_support = RecipeMetaIdeSupport()
            ide_support.initialize(config, tinfoil)
            bootstap_tasks += ide_support.bootstrap_tasks

            build_sysroots = RecipeBuildSysroots()
            build_sysroots.initialize(config, tinfoil)
            bootstap_tasks += build_sysroots.bootstrap_tasks

            sdk_env = SharedSysrootsEnv()
            sdk_env.initialize(ide_support, build_sysroots)
        else:
            sdk_env = RecipeModified(args.recipename)
            sdk_env.initialize(config, workspace, tinfoil)
            bootstap_tasks += sdk_env.bootstrap_tasks

            gdb_multi_mode = not bool(args.ide == 'code')
            gdb_cross = RecipeGdbCross(
                args, sdk_env.target_arch, target_device, gdb_multi_mode)
            gdb_cross.initialize(config, workspace, tinfoil)
            bootstap_tasks += gdb_cross.bootstrap_tasks
    finally:
        tinfoil.shutdown()

    shared_sysroot = bool(args.recipename in SHARED_SYSROOT_RECIPES)
    if not shared_sysroot:
        sdk_env.debug_build_config(args)

    if not args.skip_bitbake:
        bb_cmd = 'bitbake '
        if args.bitbake_k:
            bb_cmd += "-k "
        bb_cmd += ' '.join(bootstap_tasks)
        exec_build_env_command(config.init_path, basepath, bb_cmd, watch=True)

    if shared_sysroot:
        sdk_env.setup_ide(args)
    else:
        sdk_env.setup_ide(args, image_config, gdb_cross)


def get_default_ide():
    for an_ide in SUPPORTED_IDES[:-1]:
        if shutil.which(an_ide):
            return an_ide
    return SUPPORTED_IDES[-1:]


def register_commands(subparsers, context):
    """Register devtool subcommands from this plugin"""
    parser_ide = subparsers.add_parser('ide', help='Setup the IDE (VSCode)',
                                       description='Configure the IDE to work with the source code of a recipe.',
                                       group='working', order=50, formatter_class=RawTextHelpFormatter)
    parser_ide.add_argument(
        'recipename', help='Generate a IDE configuration in the workspace of the given recipe. '
        'By default the workspace is configured to use the recipe sysroot prepared by devtool modify. '
        'For some special recipes the generated configuration referes to the shared sysroots '
        'provided by meta-ide-setup and build-sysroots recipes. '
        'The following recipes use a shared sysroot: %s' % SHARED_SYSROOT_RECIPES)
    parser_ide.add_argument(
        'image', help='The image running on the target device. This is required for remote debugging.'
        'It is important to deploy the image built by this command to the target device because '
        'otherwise the debug symbols used on the build machine (rootfs-dbg) are probably out of sync '
        'with the binaries executed on the target device.')
    parser_ide.add_argument(
        '-i', '--ide', choices=SUPPORTED_IDES, default=get_default_ide(),
        help='Setup the configuration for this IDE')
    parser_ide.add_argument(
        '-t', '--target', default='root@192.168.7.2',
        help='Live target machine running an ssh server: user@hostname.')
    parser_ide.add_argument(
        '-G', '--gdbserver-port-start', default="1234", help='port where gdbserver is listening.')
    parser_ide.add_argument(
        '-c', '--no-host-check', help='Disable ssh host key checking', action='store_true')
    parser_ide.add_argument(
        '-e', '--ssh-exec', help='Executable to use in place of ssh')
    parser_ide.add_argument(
        '-P', '--port', help='Specify ssh port to use for connection to the target')
    parser_ide.add_argument(
        '-I', '--key', help='Specify ssh private key for connection to the target')
    parser_ide.add_argument(
        '--skip-bitbake', help='Generate IDE configuration but skip calling bibtake to update the SDK.', action='store_true')
    parser_ide.add_argument(
        '-k', '--bitbake-k', help='Pass -k parameter to bitbake', action='store_true')
    parser_ide.add_argument(
        '--no-strip', help='Do not strip executables prior to deploy', dest='strip', action='store_false')
    parser_ide.add_argument(
        '-n', '--dry-run', help='List files to be undeployed only', action='store_true')
    parser_ide.add_argument(
        '-s', '--show-status', help='Show progress/status output', action='store_true')
    parser_ide.add_argument(
        '-p', '--no-preserve', help='Do not preserve existing files', action='store_true')
    parser_ide.add_argument(
        '--no-check-space', help='Do not check for available space before deploying', action='store_true')
    parser_ide.add_argument(
        '--debug-build-config', help='Use debug build flags, for example set CMAKE_BUILD_TYPE=Debug', action='store_true')
    parser_ide.set_defaults(func=ide_setup)

    # TODO: Better support for multiple recipes. E.g. a list of recipes with auto-detection for the image recipe or all modified recipes
