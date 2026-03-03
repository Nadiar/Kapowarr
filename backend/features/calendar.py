# -*- coding: utf-8 -*-

"""
Calendar feature: fetch new comic issues by date range from ComicVine,
optionally filtered by publisher, enriched with Kapowarr library status.

Includes an in-memory cache keyed by date range so repeat requests
(e.g. navigating back to the same week) are instant.
"""

from time import time
from typing import Any, Dict, List, Optional, Sequence, Tuple, TypedDict

from backend.base.logging import LOGGER
from backend.internals.db import get_db

# Cache TTL in seconds (24 hours)
_CACHE_TTL = 86400

# In-memory cache: {(start, end): (timestamp, issues)}
_calendar_cache: Dict[Tuple[str, str], Tuple[float, list]] = {}
_CACHE_MAX_ENTRIES = 20


def clear_calendar_cache() -> None:
    """Evict all entries from the in-memory calendar cache.

    Called by ``RefreshCalendarCache`` after populating localcv.db so the
    next calendar page-view picks up the freshly cached data.
    """
    _calendar_cache.clear()
    LOGGER.debug('In-memory calendar cache cleared')


class CalendarIssue(TypedDict):
    """A comic issue returned by the calendar endpoint."""
    comicvine_id: int
    volume_id: int
    volume_name: str
    issue_number: str
    title: Optional[str]
    store_date: Optional[str]
    cover_date: Optional[str]
    effective_date: str
    image_url: str
    publisher_name: Optional[str]
    publisher_id: Optional[int]
    site_url: str
    # Library status fields
    in_library: bool
    monitored: bool
    volume_monitored: bool
    volume_id_local: Optional[int]
    has_files: bool


class PublisherPreset(TypedDict):
    """A preset publisher for filtering."""
    id: int
    name: str


# Hardcoded publisher presets with ComicVine IDs
PUBLISHER_PRESETS: List[PublisherPreset] = [
    {"id": 31, "name": "Marvel"},
    {"id": 10, "name": "DC Comics"},
    {"id": 1868, "name": "Image"},
    {"id": 1190, "name": "IDW Publishing"},
    {"id": 314, "name": "Dark Horse Comics"},
    {"id": 4141, "name": "BOOM! Studios"},
    {"id": 1300, "name": "Dynamite Entertainment"},
    {"id": 2849, "name": "VIZ Media"},
    {"id": 582, "name": "Oni Press"},
    {"id": 10984, "name": "Kodansha Comics"},
    {"id": 291, "name": "Archie Comics"},
    {"id": 6450, "name": "Titan Comics"},
    {"id": 89, "name": "Fantagraphics Books"},
    {"id": 222, "name": "Drawn and Quarterly"},
    {"id": 1649, "name": "Zenescope Entertainment"},
    {"id": 11068, "name": "Vault Comics"},
    {"id": 10838, "name": "AfterShock Comics"},
    {"id": 12551, "name": "AWA Studios"},
    {"id": 295, "name": "Antarctic Press"},
    {"id": 12237, "name": "Mad Cave Studios"},
]

# Lookup set for quick membership checks
_PRESET_IDS = {p["id"] for p in PUBLISHER_PRESETS}


def get_publisher_presets() -> List[PublisherPreset]:
    """Return the list of preset publishers for the calendar filter UI."""
    return PUBLISHER_PRESETS


def _enrich_with_library_status(
    issues: List[CalendarIssue]
) -> None:
    """Cross-reference calendar issues with the Kapowarr library DB.

    Modifies issues in-place to set the library status fields:
    in_library, monitored, volume_monitored, volume_id_local, has_files.

    Args:
        issues: List of CalendarIssue dicts to enrich.
    """
    if not issues:
        return

    db = get_db()

    # Collect unique CV volume IDs and issue IDs
    cv_volume_ids = {i['volume_id'] for i in issues}
    cv_issue_ids = {i['comicvine_id'] for i in issues}

    # Look up which volumes are in the library
    # Returns: {cv_volume_id: (local_id, monitored)}
    volume_map: Dict[int, Dict[str, Any]] = {}
    if cv_volume_ids:
        placeholders = ','.join('?' * len(cv_volume_ids))
        rows = db.execute(
            f"SELECT id, comicvine_id, monitored "
            f"FROM volumes WHERE comicvine_id IN ({placeholders});",
            tuple(cv_volume_ids)
        ).fetchall()
        for row in rows:
            volume_map[row[1]] = {
                'local_id': row[0],
                'monitored': bool(row[2])
            }

    # Look up which issues are in the library and whether they have files
    # Returns: {cv_issue_id: (monitored, has_files)}
    issue_map: Dict[int, Dict[str, Any]] = {}
    if cv_issue_ids:
        placeholders = ','.join('?' * len(cv_issue_ids))
        rows = db.execute(
            f"SELECT i.comicvine_id, i.monitored, "
            f"  EXISTS(SELECT 1 FROM issues_files WHERE issue_id = i.id) "
            f"FROM issues i WHERE i.comicvine_id IN ({placeholders});",
            tuple(cv_issue_ids)
        ).fetchall()
        for row in rows:
            issue_map[row[0]] = {
                'monitored': bool(row[1]),
                'has_files': bool(row[2])
            }

    # Apply status to each issue
    for issue in issues:
        vol_info = volume_map.get(issue['volume_id'])
        iss_info = issue_map.get(issue['comicvine_id'])

        issue['in_library'] = vol_info is not None
        issue['volume_id_local'] = (
            vol_info['local_id'] if vol_info else None
        )
        issue['volume_monitored'] = (
            vol_info['monitored'] if vol_info else False
        )
        issue['monitored'] = (
            iss_info['monitored'] if iss_info else False
        )
        issue['has_files'] = (
            iss_info['has_files'] if iss_info else False
        )


def get_calendar(
    start_date: str,
    end_date: str,
    publisher_ids: Optional[Sequence[int]] = None,
    force_refresh: bool = False
) -> List[CalendarIssue]:
    """Fetch new comic issues for a date range, optionally filtered by publisher.

    Uses an in-memory cache keyed by (start_date, end_date) with a 24-hour
    TTL so repeated requests for the same range are instant. Library status
    is always refreshed from the DB (cheap local query).

    Args:
        start_date: Start of date range in YYYY-MM-DD format.
        end_date: End of date range in YYYY-MM-DD format.
        publisher_ids: Optional list of ComicVine publisher IDs to filter by.
            If None or empty, all publishers are included.
        force_refresh: If True, bypass the cache and re-fetch from CV API.

    Returns:
        List of CalendarIssue dicts sorted by store_date ascending.
    """
    from backend.features.calendar_cv import fetch_calendar_issues

    cache_key = (start_date, end_date)
    now = time()

    # Check cache
    cached = _calendar_cache.get(cache_key)
    if not force_refresh and cached and (now - cached[0]) < _CACHE_TTL:
        LOGGER.debug('Calendar cache hit for %s to %s', start_date, end_date)
        # Deep copy so library status refresh doesn't persist across requests
        issues = [dict(i) for i in cached[1]]
    else:
        LOGGER.info('Calendar cache miss for %s to %s', start_date, end_date)
        issues = fetch_calendar_issues(start_date, end_date)

        # Store in cache (evict oldest if over limit)
        if len(_calendar_cache) >= _CACHE_MAX_ENTRIES:
            oldest_key = min(_calendar_cache, key=lambda k: _calendar_cache[k][0])
            del _calendar_cache[oldest_key]
        _calendar_cache[cache_key] = (now, issues)

    # Filter by publisher if requested
    if publisher_ids:
        id_set = set(publisher_ids)
        issues = [
            issue for issue in issues
            if issue["publisher_id"] in id_set
        ]

    # Always refresh library status (cheap local DB query)
    _enrich_with_library_status(issues)

    return issues
