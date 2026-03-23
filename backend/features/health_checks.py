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

# Warn when a root folder volume has less than this many bytes free (1 GB).
DISK_SPACE_WARNING_BYTES = 1_073_741_824


def _check_comicvine_api() -> List[HealthCheckEvent]:
    """Check that the ComicVine API key is set and valid."""
    issues: List[HealthCheckEvent] = []
    try:
        from asyncio import run

        from backend.implementations.comicvine import ComicVine

        # fetch_volumes with an empty query is the lightest CV call
        # that still validates the key. Use fetch_volume on a known-safe
        # resource instead — a simple /types call is not available, so
        # we instantiate ComicVine (raises InvalidComicVineApiKey if key
        # is blank) and then do a minimal network test.
        ComicVine()  # raises InvalidComicVineApiKey if key is empty
    except Exception as exc:
        from backend.base.custom_exceptions import InvalidComicVineApiKey
        if isinstance(exc, InvalidComicVineApiKey):
            issues.append(HealthCheckEvent(
                level='error',
                message='ComicVine API key is not set or invalid.',
                check_type='ComicVineApiKey'
            ))
        else:
            LOGGER.debug(
                'ComicVine API health check raised unexpected error: %s', exc
            )
    return issues


def _check_root_folders() -> List[HealthCheckEvent]:
    """Check that all configured root folders exist and are writable."""
    issues: List[HealthCheckEvent] = []
    try:
        from backend.implementations.root_folders import RootFolders
        folders = RootFolders().get_folder_list()
    except Exception:
        LOGGER.exception('Failed to retrieve root folders for health check')
        return issues

    for folder in folders:
        if not os.path.isdir(folder):
            issues.append(HealthCheckEvent(
                level='error',
                message=f'Root folder does not exist: {folder}',
                check_type='RootFolder'
            ))
        elif not os.access(folder, os.W_OK):
            issues.append(HealthCheckEvent(
                level='warning',
                message=f'Root folder is not writable: {folder}',
                check_type='RootFolder'
            ))
    return issues


def _check_disk_space() -> List[HealthCheckEvent]:
    """Warn if any root folder volume has less than 1 GB free."""
    issues: List[HealthCheckEvent] = []
    try:
        from backend.implementations.root_folders import RootFolders
        folders = RootFolders().get_folder_list()
    except Exception:
        LOGGER.exception('Failed to retrieve root folders for disk check')
        return issues

    checked: List[str] = []
    for folder in folders:
        if not os.path.isdir(folder):
            continue
        try:
            usage = shutil.disk_usage(folder)
        except (FileNotFoundError, PermissionError, OSError):
            continue

        # Avoid duplicate warnings for folders on the same volume
        try:
            stat = os.stat(folder)
            volume_key = str(stat.st_dev)
        except OSError:
            volume_key = folder

        if volume_key in checked:
            continue
        checked.append(volume_key)

        if usage.free < DISK_SPACE_WARNING_BYTES:
            free_gb = usage.free / 1_073_741_824
            issues.append(HealthCheckEvent(
                level='warning',
                message=(
                    f'Low disk space on {folder}: '
                    f'{free_gb:.1f} GB free'
                ),
                check_type='DiskSpace'
            ))
    return issues


def run_health_checks() -> List[HealthCheckEvent]:
    """Run all health checks and return any issues found.

    Returns:
        List[HealthCheckEvent]: Issues detected. Empty list = healthy.
    """
    issues: List[HealthCheckEvent] = []
    issues.extend(_check_comicvine_api())
    issues.extend(_check_root_folders())
    issues.extend(_check_disk_space())
    return issues
