# -*- coding: utf-8 -*-

"""
Health check framework for Kapowarr.

Each check function returns a list of HealthCheckEvent instances describing
any issues found. An empty list means the check passed.
"""

import os
import shutil
from typing import List

from backend.base.logging import LOGGER
from backend.features.notifications import HealthCheckEvent


def _check_root_folders() -> List[HealthCheckEvent]:
    """Check that all configured root folders exist and are accessible."""
    issues: List[HealthCheckEvent] = []
    try:
        from backend.implementations.root_folders import RootFolders
        folders = RootFolders().get_folder_list()
    except Exception:
        LOGGER.exception('Health check: failed to retrieve root folders')
        return issues

    for folder in folders:
        if not os.path.isdir(folder):
            issues.append(HealthCheckEvent(
                level='error',
                message=f'Root folder does not exist: {folder}',
                check_type='RootFolder'
            ))
        elif not os.access(folder, os.R_OK | os.W_OK):
            issues.append(HealthCheckEvent(
                level='error',
                message=f'Root folder is not readable/writable: {folder}',
                check_type='RootFolder'
            ))
    return issues


def _check_disk_space() -> List[HealthCheckEvent]:
    """Warn when any root folder's partition has less than 1 GB free."""
    issues: List[HealthCheckEvent] = []
    try:
        from backend.implementations.root_folders import RootFolders
        folders = RootFolders().get_folder_list()
    except Exception:
        LOGGER.exception('Health check: failed to retrieve root folders')
        return issues

    seen_devs: set = set()
    for folder in folders:
        if not os.path.isdir(folder):
            continue
        try:
            dev = os.stat(folder).st_dev
            if dev in seen_devs:
                continue
            seen_devs.add(dev)

            usage = shutil.disk_usage(folder)
            free_gb = usage.free / (1024 ** 3)
            if free_gb < 1.0:
                issues.append(HealthCheckEvent(
                    level='warning',
                    message=(
                        f'Low disk space on {folder}: '
                        f'{free_gb:.1f} GB free'
                    ),
                    check_type='DiskSpace'
                ))
        except Exception:
            LOGGER.exception(
                'Health check: failed to check disk space for %s', folder
            )
    return issues


def _check_comicvine_api_key() -> List[HealthCheckEvent]:
    """Verify the ComicVine API key is configured."""
    issues: List[HealthCheckEvent] = []
    try:
        from backend.internals.settings import Settings
        sv = Settings().sv
        api_key = getattr(sv, 'comicvine_api_key', None) or ''
        if not api_key.strip():
            issues.append(HealthCheckEvent(
                level='error',
                message='ComicVine API key is not set or invalid.',
                check_type='ComicVineApiKey'
            ))
    except Exception:
        LOGGER.exception('Health check: failed to check CV API key')
    return issues


_CHECKS = [
    _check_root_folders,
    _check_disk_space,
    _check_comicvine_api_key,
]


def run_health_checks() -> List[HealthCheckEvent]:
    """Run all registered health checks and return a flat list of issues."""
    issues: List[HealthCheckEvent] = []
    for check in _CHECKS:
        try:
            issues.extend(check())
        except Exception:
            LOGGER.exception(
                'Health check %s raised an unexpected error', check.__name__
            )
    return issues
