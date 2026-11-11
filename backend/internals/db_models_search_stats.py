# -*- coding: utf-8 -*-

"""
Database helpers for tracking search outcomes per volume and per issue.

Records hit/miss results per volume so SearchAll can deprioritise volumes
that consistently yield no results, and logs when each issue was last
searched and what came of it.
"""

import time
from enum import Enum
from typing import List, Mapping, Tuple

from backend.internals.db import get_db


class IssueSearchOutcome(Enum):
    QUEUED = 'queued'
    "A download for the issue was queued"

    FOUND = 'found'
    "The issue was found, but no download for it could be queued"

    NOT_FOUND = 'not_found'
    "The search worked, but found nothing for the issue"

    ERROR = 'error'
    "An indexer couldn't be searched, so the issue may exist"


def record_search_hit(volume_id: int) -> None:
    """Record a successful search (found downloadable results).

    Resets consecutive_misses to 0, increments total_searches and total_hits.
    Uses INSERT … ON CONFLICT upsert so the first call creates the row.

    Args:
        volume_id (int): ID of the volume that was searched.
    """
    ts = int(time.time())
    cursor = get_db()
    with cursor:
        cursor.execute(
            """
            INSERT INTO volume_search_stats(
                volume_id, last_searched, consecutive_misses,
                total_searches, total_hits
            ) VALUES (:vid, :ts, 0, 1, 1)
            ON CONFLICT(volume_id) DO UPDATE SET
                last_searched      = :ts,
                consecutive_misses = 0,
                total_searches     = total_searches + 1,
                total_hits         = total_hits + 1;
            """,
            {'vid': volume_id, 'ts': ts}
        )


def record_search_miss(volume_id: int) -> None:
    """Record a failed search (no results found).

    Increments consecutive_misses and total_searches.
    Uses INSERT … ON CONFLICT upsert so the first call creates the row.

    Args:
        volume_id (int): ID of the volume that was searched.
    """
    ts = int(time.time())
    cursor = get_db()
    with cursor:
        cursor.execute(
            """
            INSERT INTO volume_search_stats(
                volume_id, last_searched, consecutive_misses,
                total_searches, total_hits
            ) VALUES (:vid, :ts, 1, 1, 0)
            ON CONFLICT(volume_id) DO UPDATE SET
                last_searched      = :ts,
                consecutive_misses = consecutive_misses + 1,
                total_searches     = total_searches + 1;
            """,
            {'vid': volume_id, 'ts': ts}
        )


def reset_misses(volume_id: int) -> None:
    """Reset consecutive_misses to 0 for a volume.

    Called when new issues are added via refresh_and_scan so the volume
    floats back up in SearchAll priority. No-op if no stats row exists yet.

    Args:
        volume_id (int): ID of the volume whose miss counter should clear.
    """
    cursor = get_db()
    with cursor:
        cursor.execute(
            "UPDATE volume_search_stats "
            "SET consecutive_misses = 0 "
            "WHERE volume_id = ?;",
            (volume_id,)
        )


def record_issue_searches(
    outcomes: Mapping[int, IssueSearchOutcome]
) -> None:
    """Log the outcome of searching for each issue.

    Only a search that worked and found nothing counts as a miss. A queued
    download resets the misses, and a failed search leaves them as they are.

    Args:
        outcomes (Mapping[int, IssueSearchOutcome]): Issue ID to its outcome.
    """
    if not outcomes:
        return

    ts = int(time.time())
    cursor = get_db()
    with cursor:
        cursor.executemany(
            """
            INSERT INTO issue_search_log(
                issue_id, last_searched, last_outcome,
                consecutive_misses, times_searched, last_queued
            ) VALUES (
                :issue_id, :ts, :outcome,
                :outcome = 'not_found', 1,
                CASE WHEN :outcome = 'queued' THEN :ts ELSE 0 END
            )
            ON CONFLICT(issue_id) DO UPDATE SET
                last_searched = :ts,
                last_outcome = :outcome,
                consecutive_misses = CASE :outcome
                    WHEN 'not_found' THEN consecutive_misses + 1
                    WHEN 'queued' THEN 0
                    ELSE consecutive_misses
                END,
                times_searched = times_searched + 1,
                last_queued = CASE
                    WHEN :outcome = 'queued' THEN :ts
                    ELSE last_queued
                END;
            """,
            (
                {'issue_id': issue_id, 'ts': ts, 'outcome': outcome.value}
                for issue_id, outcome in outcomes.items()
            )
        )
    return


def get_search_all_volumes() -> List[Tuple[int, str]]:
    """Get the monitored volumes with missing issues, in the order SearchAll
    should search them: the newest already released missing issue first.
    Volumes whose missing issues are all unreleased or undated go last. Ties
    go to fewer consecutive misses, then the most recently added volume.

    Returns:
        List[Tuple[int, str]]: The ID and title of each volume.
    """
    rows = get_db().execute(
        """
        SELECT v.id, v.title
        FROM volumes v
        INNER JOIN issues i ON i.volume_id = v.id
        LEFT JOIN issues_files if_ ON if_.issue_id = i.id
        LEFT JOIN volume_search_stats vss ON vss.volume_id = v.id
        WHERE v.monitored = 1
            AND i.monitored = 1
            AND if_.issue_id IS NULL
        GROUP BY v.id
        ORDER BY
            MAX(CASE WHEN i.date <= DATE('now') THEN i.date END) IS NULL,
            MAX(CASE WHEN i.date <= DATE('now') THEN i.date END) DESC,
            COALESCE(vss.consecutive_misses, 0),
            v.id DESC;
        """
    ).fetchall()
    return [(r[0], r[1]) for r in rows]
