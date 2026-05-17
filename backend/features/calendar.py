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
    parent_publisher_name: Optional[str]
    parent_publisher_id: Optional[int]
    site_url: str
    # Library status fields
    in_library: bool
    monitored: bool
    volume_monitored: bool
    volume_id_local: Optional[int]
    issue_id_local: Optional[int]
    has_files: bool


# Publisher presets — name-based (IDs vary between CV and CV proxies)
PUBLISHER_PRESETS: List[str] = [
    "Marvel",
    "DC Comics",
    "Image",
    "IDW Publishing",
    "Dark Horse Comics",
    "BOOM! Studios",
    "Dynamite Entertainment",
    "VIZ Media",
    "Oni Press",
    "Kodansha Comics",
    "Archie Comics",
    "Titan Comics",
    "Fantagraphics Books",
    "Drawn and Quarterly",
    "Zenescope Entertainment",
    "Vault Comics",
    "AfterShock Comics",
    "AWA Studios",
    "Antarctic Press",
    "Mad Cave Studios",
]

# Lookup set for quick membership checks
_PRESET_NAMES = set(PUBLISHER_PRESETS)


def get_publisher_presets() -> List[str]:
    """Return the list of preset publisher names for the calendar filter."""
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
    # Returns: {cv_issue_id: (local_id, monitored, has_files)}
    issue_map: Dict[int, Dict[str, Any]] = {}
    if cv_issue_ids:
        placeholders = ','.join('?' * len(cv_issue_ids))
        rows = db.execute(
            f"SELECT i.id, i.comicvine_id, i.monitored, "
            f"  EXISTS(SELECT 1 FROM issues_files WHERE issue_id = i.id) "
            f"FROM issues i WHERE i.comicvine_id IN ({placeholders});",
            tuple(cv_issue_ids)
        ).fetchall()
        for row in rows:
            issue_map[row[1]] = {
                'local_id': row[0],
                'monitored': bool(row[2]),
                'has_files': bool(row[3])
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
        issue['issue_id_local'] = (
            iss_info['local_id'] if iss_info else None
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
    publisher_names: Optional[Sequence[str]] = None,
    force_refresh: bool = False
) -> List[CalendarIssue]:
    """Fetch new comic issues for a date range, optionally filtered.

    Uses an in-memory cache keyed by (start_date, end_date) with a
    24-hour TTL so repeated requests for the same range are instant.
    Library status is always refreshed from the DB.

    Args:
        start_date: Start of date range in YYYY-MM-DD format.
        end_date: End of date range in YYYY-MM-DD format.
        publisher_names: Optional list of publisher names to filter by.
            If None or empty, all publishers are included.
        force_refresh: If True, bypass the cache and re-fetch.

    Returns:
        List of CalendarIssue dicts sorted by store_date ascending.
    """
    from backend.features.calendar_cv import fetch_calendar_issues

    cache_key = (start_date, end_date)
    now = time()

    # Check cache — exact match first, then look for a superset range
    # Cache stores raw issues without library status (enrichment is always
    # fresh)
    cached_issues: Optional[List[CalendarIssue]] = None

    if not force_refresh:
        # Exact match
        cached = _calendar_cache.get(cache_key)
        if cached and (now - cached[0]) < _CACHE_TTL:
            LOGGER.debug(
                'Calendar cache hit for %s to %s',
                start_date,
                end_date)
            cached_issues = [dict(i) for i in cached[1]]
        else:
            # Check if any cached range fully contains the requested range
            for (cs, ce), (ts, cached_range_issues) in _calendar_cache.items():
                if cs <= start_date and ce >= end_date and (
                    now - ts) < _CACHE_TTL:
                    LOGGER.debug(
                        'Calendar cache superset hit: %s–%s within %s–%s',
                        start_date, end_date, cs, ce
                    )
                    # Filter superset cache data down to the requested window.
                    cached_issues = [
                        dict(i) for i in cached_range_issues
                        if start_date <= (
                            i.get('effective_date')
                            or i.get('store_date')
                            or ''
                        ) <= end_date
                    ]
                    break

    if cached_issues is None:
        LOGGER.info('Calendar cache miss for %s to %s', start_date, end_date)
        cached_issues = fetch_calendar_issues(start_date, end_date)

        # Store in cache (evict oldest if over limit)
        if len(_calendar_cache) >= _CACHE_MAX_ENTRIES:
            oldest_key = min(
                _calendar_cache,
                key=lambda k: _calendar_cache[k][0])
            del _calendar_cache[oldest_key]
        _calendar_cache[cache_key] = (now, cached_issues)

    # Create a working copy for this response
    issues = [dict(i) for i in cached_issues]

    # Filter by publisher name if requested
    if publisher_names:
        name_set = set(publisher_names)
        issues = [
            issue for issue in issues
            if issue.get("publisher_name") in name_set
            or issue.get("parent_publisher_name") in name_set
        ]

    # Always refresh library status (cheap local DB query)
    _enrich_with_library_status(issues)

    return issues
