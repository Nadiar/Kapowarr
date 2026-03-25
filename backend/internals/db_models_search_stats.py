# -*- coding: utf-8 -*-

"""
Database helpers for tracking per-volume search outcomes.

Records hit/miss results per volume so SearchAll can deprioritise volumes
that consistently yield no results, reducing pointless search traffic.
"""

import time
from typing import Union

from backend.internals.db import get_db


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


def get_total_volume_count() -> int:
    """Return the count of monitored volumes.

    Used by SearchAll to compute the priority penalty per consecutive miss:
        priority = v.id - (consecutive_misses * total_volume_count)

    Returns:
        int: Number of monitored volumes (0 if none).
    """
    row = get_db().execute(
        "SELECT COUNT(*) FROM volumes WHERE monitored = 1;"
    ).fetchone()
    if row is None:
        return 0
    return int(row[0])
