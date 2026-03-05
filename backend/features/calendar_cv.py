# -*- coding: utf-8 -*-

"""
ComicVine API integration for the calendar feature.

Fetches issues by store_date range and enriches them with
publisher information from the CV API.
"""

from asyncio import run
from typing import Any, Dict, List

from backend.base.custom_exceptions import InvalidComicVineApiKey
from backend.base.logging import LOGGER
from backend.features.calendar import CalendarIssue
from backend.implementations.comicvine import ComicVine


def _pick_effective_date(
    store_date: str | None,
    cover_date: str | None
) -> str:
    """Return store_date, falling back to cover_date, then ``'Unknown'``.

    store_date is the actual shelf date and takes priority.
    cover_date is used only when store_date is absent.
    """
    return store_date or cover_date or 'Unknown'


def _enrich_with_publishers(
    issues: List[Dict[str, Any]]
) -> List[CalendarIssue]:
    """Enrich raw issue dicts with publisher info from the CV API.

    Filters out stub entries that have no description and no creators
    (likely placeholder or erroneous CV entries).

    Args:
        issues: List of raw CV API issue result dicts.

    Returns:
        List of CalendarIssue TypedDicts with publisher info filled in.
    """
    # NOTE: Stub filter removed — CVProxy may return issues without
    # description/person_credits from local cache; filtering here
    # Collect unique volume IDs
    volume_ids = {
        int(issue['volume']['id'])
        for issue in issues
        if issue.get('volume')
    }

    volume_publisher_map: Dict[int, Dict[str, Any]] = {}

    # Fetch all volumes from CV API
    if volume_ids:
        _fetch_missing_volumes(list(volume_ids), volume_publisher_map)

    # Build CalendarIssue list
    result: List[CalendarIssue] = []
    for issue in issues:
        vol = issue.get('volume') or {}
        vol_id = int(vol.get('id', 0))
        vol_info = volume_publisher_map.get(vol_id, {})

        image = issue.get('image') or {}
        image_url = (
            image.get('small_url')
            or image.get('medium_url')
            or image.get('original_url')
            or ''
        )

        calendar_issue: CalendarIssue = {
            'comicvine_id': int(issue['id']),
            'volume_id': vol_id,
            'volume_name': vol_info.get('volume_name', vol.get('name', '')),
            'issue_number': (issue.get('issue_number') or '').strip(),
            'title': issue.get('name') or None,
            'store_date': issue.get('store_date') or None,
            'cover_date': issue.get('cover_date') or None,
            'effective_date': _pick_effective_date(
                issue.get('store_date'),
                issue.get('cover_date')
            ),
            'image_url': image_url,
            'publisher_name': vol_info.get('publisher_name'),
            'publisher_id': vol_info.get('publisher_id'),
            'site_url': issue.get('site_detail_url') or '',
            # Library status defaults (enriched later)
            'in_library': False,
            'monitored': False,
            'volume_monitored': False,
            'volume_id_local': None,
            'has_files': False
        }
        result.append(calendar_issue)

    return result


def _fetch_missing_volumes(
    volume_ids: List[int],
    volume_publisher_map: Dict[int, Dict[str, Any]]
) -> None:
    """Fetch volumes from the CV API and update the publisher map.

    Args:
        volume_ids: CV volume IDs to fetch.
        volume_publisher_map: Dict to update with fetched publisher info.
    """
    try:
        cv = ComicVine()
    except InvalidComicVineApiKey:
        LOGGER.warning(
            'No CV API key set; cannot enrich calendar with publishers'
        )
        return

    try:
        raw_vols = run(cv.fetch_volumes_for_enrichment(volume_ids))
        for vol in raw_vols:
            vid = int(vol['id'])
            pub = vol.get('publisher') or {}
            volume_publisher_map[vid] = {
                'volume_name': vol.get('name', ''),
                'publisher_id': int(pub['id']) if pub.get('id') else None,
                'publisher_name': pub.get('name')
            }
    except Exception as e:
        LOGGER.warning('Failed to fetch missing volumes for calendar: %s', e)


def fetch_calendar_issues(
    start_date: str,
    end_date: str
) -> List[CalendarIssue]:
    """Fetch all issues in the given date range from ComicVine.

    Queries by ``store_date`` only (the actual shelf date).

    Args:
        start_date: Start date in YYYY-MM-DD format (inclusive).
        end_date: End date in YYYY-MM-DD format (inclusive).

    Returns:
        List of CalendarIssue dicts with an ``effective_date`` field
        set to store_date (falling back to cover_date if absent).
    """
    try:
        cv = ComicVine()
    except InvalidComicVineApiKey:
        LOGGER.warning('No CV API key set; cannot fetch calendar issues')
        return []

    try:
        all_raw_issues = run(
            cv.fetch_issues_by_date_range(start_date, end_date)
        )
    except Exception as e:
        LOGGER.error('Calendar fetch failed: %s', e)
        return []

    # Enrich with publisher info
    return _enrich_with_publishers(all_raw_issues)
