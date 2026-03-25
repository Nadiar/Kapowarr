# -*- coding: utf-8 -*-

"""
Background tasks and their handling
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from asyncio import run
from datetime import date, timedelta
from threading import Event, Thread, Timer
from time import sleep, time
from typing import Dict, List, Tuple, Type, Union

from flask import Flask

from backend.base.custom_exceptions import (InvalidComicVineApiKey,
                                            TaskNotDeletable, TaskNotFound)
from backend.base.helpers import Singleton, get_subclasses
from backend.base.logging import LOGGER
from backend.features.download_queue import DownloadHandler
from backend.features.search import auto_search
from backend.implementations.comicvine import ComicVine
from backend.implementations.conversion import mass_convert
from backend.implementations.file_matching import scan_files
from backend.implementations.naming import mass_rename
from backend.implementations.volumes import (Volume, delete_orphaned_issues,
                                             determine_special_version,
                                             refresh_and_scan,
                                             refresh_special_versions,
                                             update_volume_metadata,
                                             upsert_issues)
from backend.internals.db import close_db, commit, get_db
from backend.internals.server import (TaskAddedEvent, TaskEndedEvent,
                                      TaskStatusEvent, WebSocket)


class Task(ABC):
    stop: bool
    message: str
    action: str
    display_title: str
    category: str
    priority: int = 10

    @property
    @abstractmethod
    def volume_id(self) -> Union[int, None]:
        ...

    @property
    @abstractmethod
    def issue_id(self) -> Union[int, None]:
        ...

    @abstractmethod
    def __init__(self, **kwargs) -> None:
        ...

    def _ensure_yield_event(self) -> Event:
        """Lazily initialise the yield event."""
        if not hasattr(self, '_yield_event'):
            self._yield_event = Event()
            self._yield_event.set()
        return self._yield_event

    def check_yield(self) -> None:
        """Call between units of work. Blocks if a higher-priority
        task is waiting; resumes when it finishes."""
        evt = self._ensure_yield_event()
        evt.wait()

    @abstractmethod
    def run(self) -> Union[None, List[Tuple[str, int, Union[int, None]]]]:
        """Run the task

        Returns:
            Union[None, List[Tuple[str, int, Union[int, None]]]]:
            Either `None` if the task has no result or
            `List[Tuple[str, int, Union[int, None]]]` if the task returns
            search results.
        """
        ...

# =====================
# Issue tasks
# =====================


class AutoSearchIssue(Task):
    "Do an automatic search for an issue"

    stop = False
    message = ''
    action = 'auto_search_issue'
    display_title = 'Auto Search'
    category = 'download'
    priority = 1

    @property
    def volume_id(self) -> int:
        return self._volume_id

    @property
    def issue_id(self) -> int:
        return self._issue_id

    def __init__(self, volume_id: int, issue_id: int) -> None:
        """Create the task

        Args:
            volume_id (int): The id of the volume in which the issue is
            issue_id (int): The id of the issue to search for
        """
        self._volume_id = volume_id
        self._issue_id = issue_id
        return

    def run(self) -> List[Tuple[str, int, Union[int, None]]]:
        volume = Volume(self._volume_id)
        volume_title = volume.vd.title
        issue_number = volume.get_issue(self._issue_id).get_data().issue_number
        self.message = f'Searching for {volume_title} #{issue_number}'
        WebSocket().emit(TaskStatusEvent(self.message))

        # Get search results and download them
        results = auto_search(self._volume_id, self._issue_id)
        if results:
            return [
                (result['link'], self._volume_id, self._issue_id)
                for result in results
            ]
        return []


class MassRenameIssue(Task):
    "Trigger a mass rename for an issue"

    stop = False
    message = ''
    action = 'mass_rename_issue'
    display_title = 'Mass Rename'
    category = ''
    priority = 1

    @property
    def volume_id(self) -> int:
        return self._volume_id

    @property
    def issue_id(self) -> int:
        return self._issue_id

    def __init__(
        self,
        volume_id: int,
        issue_id: int,
        filepath_filter: List[str] = []
    ) -> None:
        """Create the task

        Args:
            volume_id (int): The ID of the volume for which to perform the task.
            issue_id (int): The ID of the issue for which to perform the task.
            filepath_filter (List[str], optional): Only rename files in this
            list.
                Defaults to [].
        """
        self._volume_id = volume_id
        self._issue_id = issue_id
        self.filepath_filter = filepath_filter
        return

    def run(self) -> None:
        volume = Volume(self._volume_id)
        volume_title = volume.vd.title
        issue_number = volume.get_issue(self._issue_id).get_data().issue_number
        self.message = f'Renaming files for {volume_title} #{issue_number}'
        WebSocket().emit(TaskStatusEvent(self.message))

        mass_rename(
            self._volume_id,
            self._issue_id,
            filepath_filter=self.filepath_filter,
            update_websocket=True
        )

        return


class MassConvertIssue(Task):
    "Trigger a mass convert for an issue"

    stop = False
    message = ''
    action = 'mass_convert_issue'
    display_title = 'Mass Convert'
    category = ''
    priority = 1

    @property
    def volume_id(self) -> int:
        return self._volume_id

    @property
    def issue_id(self) -> int:
        return self._issue_id

    def __init__(
        self,
        volume_id: int,
        issue_id: int,
        filepath_filter: List[str] = []
    ) -> None:
        """Create the task

        Args:
            volume_id (int): The ID of the volume for which to perform the task.
            issue_id (int): The ID of the issue for which to perform the task.
            filepath_filter (List[str], optional): Only rename files in this
            list.
                Defaults to [].
        """
        self._volume_id = volume_id
        self._issue_id = issue_id
        self.filepath_filter = filepath_filter
        return

    def run(self) -> None:
        volume = Volume(self._volume_id)
        volume_title = volume.vd.title
        issue_number = volume.get_issue(self._issue_id).get_data().issue_number
        self.message = f'Converting files for {volume_title} #{issue_number}'
        WebSocket().emit(TaskStatusEvent(self.message))

        mass_convert(
            self._volume_id,
            self._issue_id,
            filepath_filter=self.filepath_filter,
            update_websocket_progress=True,
            update_websocket_files=True
        )

        return

# =====================
# Volume tasks
# =====================


class AutoSearchVolume(Task):
    "Do an automatic search for a volume"

    stop = False
    message = ''
    action = 'auto_search'
    display_title = 'Auto Search'
    category = 'download'
    priority = 1

    @property
    def volume_id(self) -> int:
        return self._volume_id

    @property
    def issue_id(self) -> None:
        return None

    def __init__(self, volume_id: int) -> None:
        """Create the task

        Args:
            volume_id (int): The id of the volume to search for
        """
        self._volume_id = volume_id
        return

    def run(self) -> List[Tuple[str, int, Union[int, None]]]:
        volume_title = Volume(self._volume_id).vd.title
        self.message = f'Searching for {volume_title}'
        WebSocket().emit(TaskStatusEvent(self.message))

        # Get search results and download them
        results = auto_search(self._volume_id)
        if results:
            return [
                (result['link'], self._volume_id, None)
                for result in results
            ]
        return []


class RefreshAndScanVolume(Task):
    "Trigger a refresh and scan for a volume"

    stop = False
    message = ''
    action = 'refresh_and_scan'
    display_title = 'Refresh And Scan'
    category = ''
    priority = 2

    @property
    def volume_id(self) -> int:
        return self._volume_id

    @property
    def issue_id(self) -> None:
        return None

    def __init__(self, volume_id: int) -> None:
        """Create the task

        Args:
            volume_id (int): The id of the volume for which to perform the task
        """
        self._volume_id = volume_id
        return

    def run(self) -> None:
        volume_title = Volume(self._volume_id).vd.title
        self.message = f'Updating info on {volume_title}'
        WebSocket().emit(TaskStatusEvent(self.message))

        try:
            refresh_and_scan(self._volume_id, update_websocket=True)
        except InvalidComicVineApiKey:
            pass

        return


class MassRenameVolume(Task):
    "Trigger a mass rename for a volume"

    stop = False
    message = ''
    action = 'mass_rename'
    display_title = 'Mass Rename'
    category = ''
    priority = 1

    @property
    def volume_id(self) -> int:
        return self._volume_id

    @property
    def issue_id(self) -> None:
        return None

    def __init__(
        self,
        volume_id: int,
        filepath_filter: List[str] = []
    ) -> None:
        """Create the task

        Args:
            volume_id (int): The ID of the volume for which to perform the task.
            filepath_filter (List[str], optional): Only rename files in this
            list.
                Defaults to [].
        """
        self._volume_id = volume_id
        self.filepath_filter = filepath_filter
        return

    def run(self) -> None:
        volume_title = Volume(self._volume_id).vd.title
        self.message = f'Renaming files for {volume_title}'
        WebSocket().emit(TaskStatusEvent(self.message))

        mass_rename(
            self._volume_id,
            filepath_filter=self.filepath_filter,
            update_websocket=True
        )

        return


class MassConvertVolume(Task):
    "Trigger a mass convert for a volume"

    stop = False
    message = ''
    action = 'mass_convert'
    display_title = 'Mass Convert'
    category = ''
    priority = 1

    @property
    def volume_id(self) -> int:
        return self._volume_id

    @property
    def issue_id(self) -> None:
        return None

    def __init__(
        self,
        volume_id: int,
        filepath_filter: List[str] = []
    ) -> None:
        """Create the task

        Args:
            volume_id (int): The ID of the volume for which to perform the task.
            filepath_filter (List[str], optional): Only convert files in this
            list.
                Defaults to [].
        """
        self._volume_id = volume_id
        self.filepath_filter = filepath_filter
        return

    def run(self) -> None:
        volume_title = Volume(self._volume_id).vd.title
        self.message = f'Converting files for {volume_title}'
        WebSocket().emit(TaskStatusEvent(self.message))

        mass_convert(
            self._volume_id,
            filepath_filter=self.filepath_filter,
            update_websocket_progress=True,
            update_websocket_files=True
        )

        return

# =====================
# Library tasks
# =====================


class SyncIssues(Task):
    "Fetch new/updated issues from ComicVine for all monitored volumes"

    stop = False
    message = ''
    action = 'sync_issues'
    display_title = 'Sync Issues'
    description = (
        'Fetches new and updated issues from ComicVine for all monitored'
        ' volumes, using the timestamp of the last successful sync.'
    )
    category = ''
    priority = 3

    @property
    def volume_id(self) -> None:
        return None

    @property
    def issue_id(self) -> None:
        return None

    def __init__(self) -> None:
        return

    def run(self) -> None:
        self.message = 'Syncing issues from ComicVine'
        WebSocket().emit(TaskStatusEvent(self.message))
        cursor = get_db()

        row = cursor.execute(
            "SELECT value FROM config WHERE key='last_issue_sync'"
        ).fetchonedict()
        last_sync = int(row['value']) if row else 0
        since = last_sync - 7200

        volumes = cursor.execute(
            "SELECT comicvine_id, id, last_cv_fetch "
            "FROM volumes WHERE monitored = 1;"
        ).fetchalldict()
        if not volumes:
            return

        cv_to_id_fetch = {
            v['comicvine_id']: (v['id'], v['last_cv_fetch'])
            for v in volumes
        }
        volume_cv_ids = tuple(cv_to_id_fetch.keys())

        cv = ComicVine()
        issue_datas = run(cv.fetch_issues_since(volume_cv_ids, since))

        if issue_datas:
            upsert_issues(cv_to_id_fetch, issue_datas)

        cursor.execute(
            "UPDATE config SET value=? WHERE key='last_issue_sync'",
            (str(int(time())),)
        )
        cursor.connection.commit()
        return


class RefreshMetadata(Task):
    "Refresh volume metadata from ComicVine for stale volumes"

    stop = False
    message = ''
    action = 'refresh_metadata'
    display_title = 'Refresh Metadata'
    description = (
        'Refreshes volume metadata (cover images and descriptions) from'
        ' ComicVine for volumes not updated in over 30 days or missing data.'
    )
    category = ''
    priority = 3

    @property
    def volume_id(self) -> None:
        return None

    @property
    def issue_id(self) -> None:
        return None

    def __init__(self) -> None:
        return

    def run(self) -> None:
        self.message = 'Refreshing volume metadata'
        WebSocket().emit(TaskStatusEvent(self.message))
        cursor = get_db()
        thirty_days_ago = time() - (30 * 86400)

        volumes = cursor.execute(
            """
            SELECT comicvine_id, id, last_cv_fetch
            FROM volumes
            WHERE last_cv_fetch <= :thirty_days_ago
               OR id NOT IN (
                   SELECT volume_id FROM volumes_covers
                   WHERE cover IS NOT NULL
               )
               OR description IS NULL OR description = ''
            ORDER BY last_cv_fetch ASC;
            """,
            {'thirty_days_ago': thirty_days_ago}
        ).fetchalldict()

        if not volumes:
            return

        cv_to_id_fetch = {
            v['comicvine_id']: (v['id'], v['last_cv_fetch'])
            for v in volumes
        }
        cv = ComicVine()
        volume_datas = run(cv.fetch_volumes(tuple(cv_to_id_fetch.keys())))

        if volume_datas:
            update_volume_metadata(cv_to_id_fetch, volume_datas)

        self.check_yield()
        return


class ScanFiles(Task):
    "Scan files on disk for all monitored volumes"

    stop = False
    message = ''
    action = 'scan_files'
    display_title = 'Scan Files'
    description = (
        'Scans files on disk for all monitored volumes and updates'
        ' which issues have local files.'
    )
    category = ''
    priority = 3

    @property
    def volume_id(self) -> None:
        return None

    @property
    def issue_id(self) -> None:
        return None

    def __init__(self) -> None:
        return

    def run(self) -> None:
        cursor = get_db()
        volume_ids = [
            row[0] for row in cursor.execute(
                "SELECT id FROM volumes WHERE monitored = 1;"
            )
        ]
        total = len(volume_ids)
        ws = WebSocket()
        for idx, vid in enumerate(volume_ids, 1):
            self.check_yield()
            if self.stop:
                break
            self.message = f'Scanning files {idx}/{total}'
            ws.emit(TaskStatusEvent(self.message))
            scan_files(vid)
        return


class SpecialVersionRefresh(Task):
    "Re-determine the special version type for all non-locked volumes"

    stop = False
    message = ''
    action = 'special_version_refresh'
    display_title = 'Refresh Special Versions'
    description = (
        'Re-evaluates the special version type (TPB, Hardcover, One Shot,'
        ' etc.) for all volumes that have not been manually locked.'
    )
    category = ''
    priority = 4

    @property
    def volume_id(self) -> None:
        return None

    @property
    def issue_id(self) -> None:
        return None

    def __init__(self) -> None:
        return

    def run(self) -> None:
        cursor = get_db()
        volume_ids = [
            row[0] for row in cursor.execute(
                "SELECT id FROM volumes "
                "WHERE special_version_locked = 0;"
            )
        ]
        ws = WebSocket()
        for vid in volume_ids:
            self.check_yield()
            if self.stop:
                break
            result = determine_special_version(vid)
            cursor.execute(
                "UPDATE volumes "
                "SET special_version = ? "
                "WHERE id = ? AND special_version_locked = 0",
                (result, vid)
            )
        commit()
        return


class SearchAll(Task):
    "Trigger an automatic search for each volume in the library"

    stop = False
    message = ''
    action = 'search_all'
    display_title = 'Search All'
    description = (
        'Searches for missing issues across every monitored volume in the'
        ' library. Volumes with recent consecutive misses are deprioritised.'
    )
    category = 'download'
    priority = 5

    @property
    def volume_id(self) -> None:
        return None

    @property
    def issue_id(self) -> None:
        return None

    def __init__(self) -> None:
        return

    def run(self) -> List[Tuple[str, int, Union[int, None]]]:
        from backend.internals.db_models_search_stats import (
            get_total_volume_count, record_search_hit, record_search_miss)

        cursor = get_db(force_new=True)
        # Only search volumes that have at least one open issue
        # (monitored issue with no file) — skip fully downloaded volumes.
        # Priority scoring: volumes with fewer consecutive misses are searched
        # first. Within the same miss count, newer volumes (higher ID) come
        # first. vol_count is used to scale the penalty so one miss pushes a
        # volume behind all untested ones.
        vol_count = get_total_volume_count()
        volumes = cursor.execute(
            """
            SELECT DISTINCT v.id, v.title,
                (v.id - COALESCE(vss.consecutive_misses, 0) * :vol_count)
                    AS priority
            FROM volumes v
            INNER JOIN issues i ON i.volume_id = v.id
            LEFT JOIN issues_files if_ ON if_.issue_id = i.id
            LEFT JOIN volume_search_stats vss ON vss.volume_id = v.id
            WHERE v.monitored = 1
              AND i.monitored = 1
              AND if_.issue_id IS NULL
            ORDER BY priority DESC;
            """,
            {'vol_count': vol_count}
        ).fetchall()
        downloads: List[Tuple[str, int, Union[int, None]]] = []
        ws = WebSocket()
        for volume_id, volume_title, _priority in volumes:
            if self.stop:
                break
            self.check_yield()
            if self.stop:
                break
            self.message = f'Searching for {volume_title}'
            ws.emit(TaskStatusEvent(self.message))
            # Get search results and download them
            results = auto_search(volume_id)
            if results:
                record_search_hit(volume_id)
                downloads += [
                    (result['link'], volume_id, None)
                    for result in results
                ]
            else:
                record_search_miss(volume_id)
        return downloads


class RefreshCalendar(Task):
    """Pre-warm the calendar cache with +/- 1 week from today."""

    stop = False
    message = ''
    action = 'refresh_calendar'
    display_title = 'Refresh Calendar'
    description = (
        'Pre-warms the calendar cache with releases in the \u00b17-day window'
        ' around today so that calendar page loads are instant.'
    )
    category = 'maintenance'
    interval = 86400  # 24 hours
    priority = 3

    @property
    def volume_id(self) -> None:
        return None

    @property
    def issue_id(self) -> None:
        return None

    def __init__(self) -> None:
        return

    def run(self) -> List[Tuple[str, int, Union[int, None]]]:
        from backend.features.calendar import get_calendar
        today = date.today()
        window_start = (today - timedelta(days=7)).isoformat()
        window_end = (today + timedelta(days=7)).isoformat()

        self.message = 'Refreshing calendar cache'
        ws = WebSocket()
        ws.emit(TaskStatusEvent(self.message))

        # Force refresh to update the cache
        get_calendar(window_start, window_end, force_refresh=True)

        self.message = 'Calendar cache refreshed'
        ws.emit(TaskStatusEvent(self.message))

        return []


class SearchRecent(Task):
    """Search for missing issues released in the past 2 weeks or releasing
    in the next week."""

    stop = False
    message = ''
    action = 'search_recent'
    display_title = 'Search Recent'
    description = (
        'Searches for missing issues released in the past 2 weeks or'
        ' releasing in the next week, using ComicVine store dates and'
        ' GetComics weekly packs.'
    )
    category = 'download'
    priority = 3

    @property
    def volume_id(self) -> None:
        return None

    @property
    def issue_id(self) -> None:
        return None

    def __init__(self) -> None:
        return

    def run(self) -> List[Tuple[str, int, Union[int, None]]]:
        today = date.today()
        window_start = (today - timedelta(days=14)).isoformat()
        window_end = (today + timedelta(days=7)).isoformat()

        # Use calendar data (keyed by store_date, the actual shelf date) so we
        # find genuinely recent releases rather than relying on the local `date`
        # column which may hold cover_date (printed 2-3 months after release).
        # get_calendar() serves from the in-memory cache when available, so
        # this is free if the calendar page was visited recently.
        from backend.features.calendar import get_calendar
        calendar_issues = get_calendar(window_start, window_end)

        downloads: List[Tuple[str, int, Union[int, None]]] = []
        ws = WebSocket()
        # Track volumes already searched at volume level so we don't
        # repeat for every issue in the same volume.
        searched_volumes: set = set()

        for issue in calendar_issues:
            if self.stop:
                break
            if not issue['in_library'] or not issue['volume_monitored']:
                continue

            vol_id = issue['volume_id_local']
            if vol_id is None:
                continue

            iss_id = issue['issue_id_local']

            if iss_id is not None:
                # Issue exists in local DB — check it individually
                if not issue['monitored'] or issue['has_files']:
                    continue
                self.message = (
                    f'Searching for {issue["volume_name"]} '
                    f'#{issue.get("issue_number", "?")}'
                )
                ws.emit(TaskStatusEvent(self.message))
                results = auto_search(vol_id, iss_id)
                if results:
                    downloads += [
                        (result['link'], vol_id, iss_id)
                        for result in results
                    ]
            else:
                # Issue not yet in local DB (CV knows about it but
                # Kapowarr hasn't synced). Refresh the volume first
                # so the issue gets added locally, then search by
                # the specific issue — not the whole volume.
                if vol_id in searched_volumes:
                    continue
                searched_volumes.add(vol_id)
                self.message = (
                    f'Syncing {issue["volume_name"]} '
                    f'(new issue #{issue.get("issue_number", "?")})'
                )
                ws.emit(TaskStatusEvent(self.message))
                try:
                    from backend.implementations.volumes import \
                        refresh_and_scan
                    refresh_and_scan(vol_id)
                except Exception:
                    LOGGER.warning(
                        'Failed to refresh volume %d '
                        'during SearchRecent',
                        vol_id
                    )
                    continue

                # After refresh, look up the now-synced issue by
                # its ComicVine ID so we search only this issue.
                cv_issue_id = issue.get('comicvine_id')
                local_iss_id = None
                if cv_issue_id:
                    row = get_db().execute(
                        "SELECT id FROM issues "
                        "WHERE comicvine_id = ?",
                        (cv_issue_id,)
                    ).fetchone()
                    if row:
                        local_iss_id = row[0]

                self.message = (
                    f'Searching for {issue["volume_name"]} '
                    f'#{issue.get("issue_number", "?")}'
                )
                ws.emit(TaskStatusEvent(self.message))
                results = auto_search(vol_id, local_iss_id)
                if results:
                    downloads += [
                        (result['link'], vol_id, local_iss_id)
                        for result in results
                    ]

        # Phase 2: scan GetComics weekly packs for available issues.
        # This catches issues that have been released this week but haven't yet
        # been indexed by regular CV-based searches.
        if not self.stop:
            try:
                from asyncio import run as _async_run

                from backend.base.helpers import AsyncSession
                from backend.implementations.getcomics import \
                    scrape_weekly_packs
                from backend.implementations.matching import match_title

                self.message = 'Scanning GetComics weekly packs'
                ws.emit(TaskStatusEvent(self.message))

                async def _fetch_packs():
                    async with AsyncSession() as session:
                        return await scrape_weekly_packs(
                            session, pack_count=3)

                pack_articles = _async_run(_fetch_packs())

                if pack_articles:
                    # Build lookup for monitored issues that have no files.
                    open_rows = get_db().execute(
                        """
                        SELECT v.id AS volume_id, v.title,
                               i.id AS issue_id,
                               i.calculated_issue_number
                        FROM volumes v
                        INNER JOIN issues i ON i.volume_id = v.id
                        LEFT JOIN issues_files if_
                            ON if_.issue_id = i.id
                        WHERE v.monitored = 1
                          AND i.monitored = 1
                          AND if_.issue_id IS NULL
                        """
                    ).fetchalldict()

                    # Group by volume: {volume_id: (title, {iss_num: iss_id})}
                    vol_data = {}
                    for row in open_rows:
                        vid = row['volume_id']
                        if vid not in vol_data:
                            vol_data[vid] = (row['title'], {})
                        iss_num = row['calculated_issue_number']
                        vol_data[vid][1][iss_num] = row['issue_id']

                    # Set of links already queued (avoid duplicates)
                    queued_links = {d[0] for d in downloads}

                    for article in pack_articles:
                        if self.stop:
                            break
                        art_link = article.get('link', '')
                        art_series = (article.get('series') or '').strip()
                        art_issue_num = article.get('issue_number')
                        if not art_link or not art_series:
                            continue
                        if art_link in queued_links:
                            continue
                        for vid, (vol_title, issue_map) in (
                            vol_data.items()
                        ):
                            if match_title(
                                vol_title, art_series,
                                allow_contains=True
                            ):
                                iss_id = issue_map.get(art_issue_num)
                                if iss_id is not None:
                                    downloads.append(
                                        (art_link, vid, iss_id))
                                    queued_links.add(art_link)
                                    break

            except Exception:
                LOGGER.warning(
                    'SearchRecent Phase 2 (weekly packs) failed',
                    exc_info=True
                )

        return downloads


class HealthCheck(Task):
    """Run health checks and dispatch notifications for any issues found."""

    stop = False
    message = ''
    action = 'health_check'
    display_title = 'Health Check'
    description = (
        'Runs library health checks (missing root folders, invalid API keys,'
        ' etc.) and dispatches any configured notifications for issues found.'
    )
    category = 'maintenance'
    priority = 3

    @property
    def volume_id(self) -> None:
        return None

    @property
    def issue_id(self) -> None:
        return None

    def __init__(self) -> None:
        return

    def run(self) -> List[Tuple[str, int, Union[int, None]]]:
        from backend.features.health_checks import run_health_checks
        from backend.features.notifications import NotificationService

        self.message = 'Running health checks'
        ws = WebSocket()
        ws.emit(TaskStatusEvent(self.message))

        issues = run_health_checks()

        if issues:
            ns = NotificationService()
            for issue in issues:
                try:
                    ns.notify_health_check(issue)
                except Exception:
                    LOGGER.exception(
                        'Failed to dispatch health check notification'
                    )

        count = len(issues)
        self.message = (
            f'Health check complete: {count} issue(s) found'
            if count else 'Health check complete: no issues found'
        )
        ws.emit(TaskStatusEvent(self.message))

        return []


# =====================
# Task handling
# =====================
# Maps action attr to class for all tasks
# Only works for classes that directly inherit from Task
task_library: Dict[str, Type[Task]] = {
    c.action: c
    for c in get_subclasses(Task)
}


INTERVAL_TASK_DEFAULTS: Dict[str, int] = {
    'sync_issues': 86400,
    'refresh_metadata': 604800,
    'scan_files': 86400,
    'special_version_refresh': 86400,
    'search_all': 604800,
    'refresh_calendar': 86400,
    'search_recent': 86400,
    'health_check': 86400,
}


def ensure_interval_task_rows() -> None:
    """Repair task_intervals rows for legacy and missing interval tasks."""
    cursor = get_db()
    now = round(time())

    removed = cursor.execute(
        "DELETE FROM task_intervals WHERE task_name = 'update_all';"
    ).rowcount
    if removed:
        LOGGER.info('Removed legacy task interval row: update_all')

    cursor.executemany(
        "INSERT OR IGNORE INTO task_intervals (task_name, interval, next_run)"
        " VALUES (?, ?, ?);",
        [
            (task_name, interval, now)
            for task_name, interval in INTERVAL_TASK_DEFAULTS.items()
            if task_name in task_library
        ]
    )

    cursor.connection.commit()
    return


class TaskHandler(metaclass=Singleton):
    "Note: Singleton"

    queue: List[dict] = []
    task_interval_waiter: Union[Timer, None] = None

    def __init__(self) -> None:
        """Setup the handler"""
        handler_context = Flask('handler')
        handler_context.teardown_appcontext(close_db)
        self.context = handler_context.app_context
        return

    def __run_task(self, task: Task) -> None:
        """Run a task

        Args:
            task (Task): The task to run
        """
        LOGGER.debug(f'Running task {task.display_title}')
        with self.context():
            _start = round(time())
            socket = WebSocket()
            try:
                result = task.run()
                _duration = round(time()) - _start
                cursor = get_db()

                # Note in history
                cursor.execute(
                    "INSERT INTO task_history VALUES (?,?,?,?);",
                    (task.action, task.display_title, round(time()), _duration)
                )

                if not task.stop:
                    if task.category == 'download' and result:
                        DownloadHandler().add_multiple(
                            (link, volume_id, issue_id, False)
                            for link, volume_id, issue_id in result
                        )

                    LOGGER.info(f'Finished task {task.display_title}')

            except Exception:
                LOGGER.exception(
                    'An error occured while trying to run a task: ')
                task.message = 'AN ERROR OCCURED'
                socket.emit(TaskStatusEvent(task.message))
                sleep(1.5)

            finally:
                if not task.stop:
                    socket.emit(TaskEndedEvent(task))
                    self.queue.pop(0)
                    self._process_queue()

        return

    def _process_queue(self) -> None:
        """
        Handle the queue. In the case that there is something in the queue and
        it isn't already running, start the task. This can safely be called
        multiple times while a task is going or while there is nothing in the
        queue. Also resumes paused tasks when they reach the front.
        """
        if not self.queue:
            return

        first_entry = self.queue[0]
        if first_entry['status'] == 'paused':
            first_entry['status'] = 'running'
            first_entry['task']._ensure_yield_event().set()
        elif first_entry['status'] != 'running':
            first_entry['status'] = 'running'
            first_entry['thread'].start()
        return

    def add(self, task: Task) -> int:
        """Add a task to the queue

        Args:
            task (Task): The task to add to the queue

        Returns:
            int: The id of the entry in the queue
        """
        LOGGER.debug(f'Adding task to queue: {task.display_title}')
        id = self.queue[-1]['id'] + 1 if self.queue else 1
        task_data = {
            'task': task,
            'id': id,
            'status': 'queued',
            'thread': Thread(
                target=self.__run_task,
                args=(task,),
                name=f"TaskThread-{id}"
            )
        }

        # Check for priority preemption: if a lower-priority task
        # is running and this task has higher priority, pause it.
        if (
            self.queue
            and self.queue[0]['status'] == 'running'
            and task.priority < self.queue[0]['task'].priority
        ):
            running_entry = self.queue[0]
            running_task = running_entry['task']
            LOGGER.info(
                'Preempting %s (pri %d) for %s (pri %d)',
                running_task.display_title,
                running_task.priority,
                task.display_title,
                task.priority
            )
            running_entry['status'] = 'paused'
            running_task._ensure_yield_event().clear()
            # Insert at position 0 so it runs next
            self.queue.insert(0, task_data)
        else:
            # Insert before any paused task so queued tasks
            # drain before the paused task resumes.
            insert_idx = len(self.queue)
            for i, entry in enumerate(self.queue):
                if entry['status'] == 'paused':
                    insert_idx = i
                    break
            self.queue.insert(insert_idx, task_data)

        LOGGER.info(f'Added task: {task.display_title} ({id})')
        WebSocket().emit(TaskAddedEvent(task))
        self._process_queue()
        return id

    @staticmethod
    def task_for_volume_running(volume_id: int) -> bool:
        """Whether or not there is a task in the queue that targets the volume.

        Args:
            volume_id (int): The volume ID to check for.

        Returns:
            bool: Whether or not a task is in the queue targeting the volume.
        """
        return any(
            t
            for t in TaskHandler.queue
            if (isinstance(t['task'], (SyncIssues, RefreshMetadata, ScanFiles,
                                       SpecialVersionRefresh,
                                       SearchAll, SearchRecent))
                or t['task'].volume_id == volume_id)
        )

    def __check_intervals(self) -> None:
        "Check if any interval task needs to be run and add to queue if so"
        LOGGER.debug('Checking task intervals')
        with self.context():
            current_time = time()
            ensure_interval_task_rows()

            cursor = get_db()
            interval_tasks = cursor.execute(
                "SELECT task_name, interval, next_run FROM task_intervals;"
            ).fetchall()
            LOGGER.debug(f'Task intervals: {list(map(dict, interval_tasks))}')

            # Collect task types already queued/running to prevent duplicates
            queued_actions = {
                t['task'].action for t in self.queue
            }

            for task in interval_tasks:
                if task['next_run'] <= current_time:
                    # Update next_run regardless of whether we queue
                    # (prevents accumulating missed intervals)
                    next_run = round(current_time + task['interval'])
                    cursor.execute(
                        "UPDATE task_intervals SET next_run = ? WHERE task_name = ?;",
                        (next_run, task['task_name']))

                    # Skip if this task type is already queued or running
                    if task['task_name'] in queued_actions:
                        LOGGER.info(
                            'Skipping %s — already in queue',
                            task['task_name']
                        )
                        continue

                    # Add task to queue
                    task_class = task_library.get(task['task_name'])
                    if task_class is None:
                        LOGGER.warning(
                            'Skipping unknown interval task: %s',
                            task['task_name']
                        )
                        continue

                    inst = task_class()
                    self.add(inst)

        self.handle_intervals()
        return

    def handle_intervals(self) -> None:
        "Find next time an interval task needs to be run"
        with self.context():
            ensure_interval_task_rows()
            next_run = get_db().execute(
                "SELECT MIN(next_run) FROM task_intervals"
            ).fetchone()[0]

        if next_run is None:
            next_run = round(time()) + 60

        timedelta = next_run - round(time()) + 1
        LOGGER.debug(f'Next interval task is in {timedelta} seconds')

        self.task_interval_waiter = Timer(timedelta, self.__check_intervals)
        self.task_interval_waiter.name = "TaskIntervalThread"
        self.task_interval_waiter.start()
        return

    def stop_handle(self) -> None:
        "Stop the task handler"
        LOGGER.debug('Stopping task thread')

        if self.task_interval_waiter:
            self.task_interval_waiter.cancel()

        # Stop all tasks — resume paused ones first so they
        # can observe the stop flag and exit cleanly.
        for entry in self.queue:
            entry['task'].stop = True
            if entry['status'] == 'paused':
                entry['task']._ensure_yield_event().set()

        if self.queue and self.queue[0]['status'] in (
            'running', 'paused'
        ):
            self.queue[0]['thread'].join()

        return

    def __format_entry(self, task: dict) -> dict:
        """Format a queue entry for API response

        Args:
            t (dict): The queue entry

        Returns:
            dict: The formatted queue entry
        """
        return {
            'id': task['id'],
            'action': task['task'].action,
            'display_title': task['task'].display_title,
            'status': task['status'],
            'message': task['task'].message,
            'volume_id': task['task'].volume_id,
            'issue_id': task['task'].issue_id
        }

    def get_all(self) -> List[dict]:
        """Get all tasks in the queue

        Returns:
            List[dict]: A list with all tasks in the queue.
                Formatted using `self.__format_entry()`.
        """
        return [self.__format_entry(t) for t in self.queue]

    def get_one(self, task_id: int) -> dict:
        """Get one task from the queue based on it's id

        Args:
            task_id (int): The id of the task to get from the queue

        Raises:
            TaskNotFound: The id doesn't match with any task in the queue

        Returns:
            dict: The info of the task in the queue.
                Formatted using `self.__format_entry()`.
        """
        for entry in self.queue:
            if entry['id'] == task_id:
                return self.__format_entry(entry)
        raise TaskNotFound(task_id)

    def remove(self, task_id: int) -> None:
        """Remove a task from the queue, including the currently running task.

        Args:
            task_id (int): The id of the task to delete from the queue

        Raises:
            TaskNotFound: The id doesn't map to any task in the queue
        """
        # Find the raw queue entry (get_one returns a formatted dict, not usable
        # here)
        raw_entry = None
        for entry in self.queue:
            if entry['id'] == task_id:
                raw_entry = entry
                break

        if raw_entry is None:
            raise TaskNotFound(task_id)

        was_active = raw_entry['status'] in ('running', 'paused')

        # Signal the task to stop at the next safe checkpoint
        raw_entry['task'].stop = True

        # Resume paused tasks so they can observe the stop flag
        if raw_entry['status'] == 'paused':
            raw_entry['task']._ensure_yield_event().set()

        if was_active:
            # Wait for the thread to honour the stop flag and exit.
            # __run_task's finally block skips queue cleanup when task.stop is True,
            # so we handle it here instead.
            raw_entry['thread'].join()

        try:
            self.queue.remove(raw_entry)
        except ValueError:
            # Task finished naturally between our check and the join —
            # __run_task already cleaned up the queue, nothing left to do.
            return

        LOGGER.info(
            f'Removed task: {raw_entry["task"].display_title} ({task_id})')
        WebSocket().emit(TaskEndedEvent(raw_entry['task']))

        if was_active:
            # Kick off the next queued task (normally done by __run_task)
            self._process_queue()

        return


def get_task_history(offset: int = 0) -> List[dict]:
    """Get the task history in blocks of 50.

    Args:
        offset (int, optional): The offset of the list.
            The higher the number, the deeper into history you go.

            Defaults to 0.

    Returns:
        List[dict]: The history entries.
    """
    result = get_db().execute(
        """
        SELECT
            task_name, display_title, run_at, duration_seconds
        FROM task_history
        ORDER BY run_at DESC
        LIMIT 50
        OFFSET ?;
        """,
        (offset * 50,)
    ).fetchalldict()
    return result


def delete_task_history() -> None:
    "Delete the complete task history"
    LOGGER.info(f'Deleting task history')
    get_db().execute("DELETE FROM task_history;")
    return


def get_task_planning() -> List[dict]:
    """Get the planning of each interval task (interval, next run and last run)

    Returns:
        List[dict]: List of interval tasks and their planning
    """
    ensure_interval_task_rows()

    tasks = get_db().execute(
        """
        SELECT
            i.task_name, interval, next_run, run_at AS last_run
        FROM task_intervals i
        LEFT JOIN (
            SELECT
                task_name,
                MAX(run_at) AS run_at
            FROM task_history
            GROUP BY task_name
        ) h
        ON i.task_name = h.task_name;
        """
    ).fetchalldict()

    for t in tasks:
        task_class = task_library.get(t['task_name'])
        if task_class is None:
            LOGGER.warning(
                'Unknown interval task in planning: %s',
                t['task_name']
            )
            t['display_name'] = t['task_name'].replace('_', ' ').title()
            t['description'] = ''
        else:
            t['display_name'] = task_class.display_title
            t['description'] = getattr(task_class, 'description', '')

    return tasks
