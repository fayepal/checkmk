#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2019 tribe29 GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

import os
from typing import Any, Dict, Set, Iterator, Tuple, Optional  # pylint: disable=unused-import

import cmk.utils.paths
import cmk.utils.debug
from cmk.utils.exceptions import MKGeneralException

import cmk.base.config as config
import cmk.base.console as console
import cmk.base.check_utils

from cmk.utils.type_defs import CheckPluginName, InventoryPluginName  # pylint: disable=unused-import
InventoryInfo = Dict[str, Any]

# Inventory plugins have dependencies to check plugins and the inventory
# plugins need the check API. This is the easiest solution to get this
# working at the moment. In future some kind of OOP approach would be better.
# TODO: Clean this up!
#from cmk.base.config import *

# Inventory plugins
inv_info = {}  # type: Dict[InventoryPluginName, InventoryInfo]
# Inventory export hooks
inv_export = {}  # type: Dict[str, Dict[str, Any]]
# The checks are loaded into this dictionary. Each check
_plugin_contexts = {}  # type: Dict[str, Dict[str, Any]]
# becomes a separat sub-dictionary, named by the check name
# These are the contexts of the check include files
_include_contexts = {}  # type: Dict[str, Any]


def load_plugins(get_check_api_context, get_inventory_context):
    # type: (config.GetCheckApiContext, config.GetInventoryApiContext) -> None
    loaded_files = set()  # type: Set[str]
    filelist = config.get_plugin_paths(str(cmk.utils.paths.local_inventory_dir),
                                       cmk.utils.paths.inventory_dir)

    for f in filelist:
        if f[0] == "." or f[-1] == "~":
            continue  # ignore editor backup / temp files

        file_name = os.path.basename(f)
        if file_name in loaded_files:
            continue  # skip already loaded files (e.g. from local)

        try:
            plugin_context = _new_inv_context(get_check_api_context, get_inventory_context)
            known_plugins = inv_info

            _load_plugin_includes(f, plugin_context)

            exec(open(f).read(), plugin_context)  # yapf: disable
            loaded_files.add(file_name)
        except Exception as e:
            console.error("Error in inventory plugin file %s: %s\n", f, e)
            if cmk.utils.debug.enabled():
                raise
            continue

        # Now store the check context for all plugins found in this file
        for check_plugin_name in set(inv_info).difference(known_plugins):
            _plugin_contexts[check_plugin_name] = plugin_context


def _new_inv_context(get_check_api_context, get_inventory_context):
    # type: (config.GetCheckApiContext, config.GetInventoryApiContext) -> Dict
    # Add the data structures where the inventory plugins register with Check_MK
    context = {
        "inv_info": inv_info,
        "inv_export": inv_export,
    }
    # NOTE: For better separation it would be better to copy the values, but
    # this might consume too much memory, so we simply reference them.
    # NOTE: It is possible that check includes are included, so we need the
    # usual check context here, too.
    context.update(config.new_check_context(get_check_api_context))
    context.update(get_inventory_context())
    return context


# Load the definitions of the required include files for this check
# Working with imports when specifying the includes would be much cleaner,
# sure. But we need to deal with the current check API.
def _load_plugin_includes(check_file_path, plugin_context):
    # type: (str, Dict) -> None
    for name in config.includes_of_plugin(check_file_path):
        path = _include_file_path(name)
        try:
            exec(open(path).read(), plugin_context)  # yapf: disable
        except Exception as e:
            console.error("Error in include file %s: %s\n", path, e)
            if cmk.utils.debug.enabled():
                raise


def _include_file_path(name):
    # type: (str) -> str
    local_path = cmk.utils.paths.local_inventory_dir / name
    if local_path.exists():
        return str(local_path)

    shared_path = os.path.join(cmk.utils.paths.inventory_dir, name)
    if os.path.exists(shared_path):
        return shared_path
    return config.check_include_file_path(name)


def is_snmp_plugin(check_plugin_name):
    # type: (str) -> bool
    section_name = cmk.base.check_utils.section_name_of(check_plugin_name)
    return "snmp_info" in inv_info.get(section_name, {}) \
           or cmk.base.check_utils.is_snmp_check(check_plugin_name)


def sorted_inventory_plugins():
    # type: () -> Iterator[Tuple[CheckPluginName, InventoryInfo]]
    # First resolve *all* dependencies. This ensures that there
    # are no cyclic dependencies, and that the 'depends on'
    # relation is transitive.
    resolved_dependencies = {}  # type: Dict[str, Set[str]]

    def resolve_plugin_dependencies(plugin_name, known_dependencies=None):
        # type: (CheckPluginName, Optional[Set[CheckPluginName]]) -> Set[CheckPluginName]
        '''recursively aggregate all plugin dependencies'''
        if known_dependencies is None:
            known_dependencies = set()
        if plugin_name in resolved_dependencies:
            known_dependencies.update(resolved_dependencies[plugin_name])
            return known_dependencies

        try:
            direct_dependencies = set(inv_info[plugin_name].get('depends_on', []))
        except KeyError:
            raise MKGeneralException("unknown plugin dependency: %r" % plugin_name)

        new_dependencies = direct_dependencies - known_dependencies
        known_dependencies.update(new_dependencies)
        for dependency in new_dependencies:
            known_dependencies = resolve_plugin_dependencies(dependency, known_dependencies)
        return known_dependencies

    for plugin_name in inv_info:
        resolved_dependencies[plugin_name] = resolve_plugin_dependencies(plugin_name)
        if plugin_name in resolved_dependencies[plugin_name]:
            raise MKGeneralException("cyclic plugin dependencies for %r" % plugin_name)

    # The plugins are now a partially ordered set with respect to
    # the 'depends on' relation. That means we can iteratively
    # yield the minimal elements
    remaining_plugins = set(inv_info)
    yielded_plugins = set()  # type: Set[str]
    while remaining_plugins:
        for plugin_name in sorted(remaining_plugins):
            dependencies = resolved_dependencies[plugin_name]
            if dependencies <= yielded_plugins:
                yield plugin_name, inv_info[plugin_name]
                yielded_plugins.add(plugin_name)
                remaining_plugins.remove(plugin_name)
