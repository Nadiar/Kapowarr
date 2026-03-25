"""
Unit tests for volume_search_stats recording and priority scoring.

Uses an in-memory SQLite database with the real schema so we can verify
SQL upsert, increment, and reset logic without a live Kapowarr instance.
"""

import sqlite3
import time
import unittest
from unittest.mock import patch

from backend.internals.db import KapowarrCursor

# ---------------------------------------------------------------------------
# Schema used for in-memory test DB
# ---------------------------------------------------------------------------
_SCHEMA = """
    CREATE TABLE volumes (
        id INTEGER PRIMARY KEY,
        monitored BOOL NOT NULL DEFAULT 1
    );
    CREATE TABLE volume_search_stats (
        volume_id INTEGER PRIMARY KEY,
        last_searched INTEGER NOT NULL DEFAULT 0,
        consecutive_misses INTEGER NOT NULL DEFAULT 0,
        total_searches INTEGER NOT NULL DEFAULT 0,
        total_hits INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY (volume_id) REFERENCES volumes(id) ON DELETE CASCADE
    );
"""


def _make_cursor() -> KapowarrCursor:
    """Return a KapowarrCursor backed by an in-memory SQLite DB.

    Production code (DBConnection.cursor()) explicitly sets cursor.row_factory
    after creating the KapowarrCursor, because sqlite3.Cursor(conn) does NOT
    inherit row_factory from the connection (unlike conn.cursor() which does).
    We must do the same here.
    """
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    cursor = KapowarrCursor(conn)
    cursor.row_factory = sqlite3.Row  # required — must be set explicitly
    cursor.executescript(_SCHEMA)
    return cursor


def _insert_volume(cursor: KapowarrCursor, volume_id: int,
                   monitored: int = 1) -> None:
    cursor.execute(
        "INSERT OR IGNORE INTO volumes(id, monitored) VALUES (?, ?)",
        (volume_id, monitored)
    )
    cursor.connection.commit()


# ---------------------------------------------------------------------------
# Tests: record_search_miss
# ---------------------------------------------------------------------------

class TestRecordSearchMiss(unittest.TestCase):

    def setUp(self) -> None:
        self.cursor = _make_cursor()
        _insert_volume(self.cursor, 1)

    def test_first_miss_creates_row(self) -> None:
        """First miss for a volume should create a stats row with misses=1."""
        with patch(
            'backend.internals.db_models_search_stats.get_db',
            return_value=self.cursor
        ):
            from backend.internals.db_models_search_stats import \
                record_search_miss
            record_search_miss(1)

        row = self.cursor.execute(
            "SELECT consecutive_misses, total_searches "
            "FROM volume_search_stats WHERE volume_id = 1"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row['consecutive_misses'], 1)
        self.assertEqual(row['total_searches'], 1)

    def test_consecutive_miss_increments(self) -> None:
        """Repeated misses should increment consecutive_misses."""
        with patch(
            'backend.internals.db_models_search_stats.get_db',
            return_value=self.cursor
        ):
            from backend.internals.db_models_search_stats import \
                record_search_miss
            record_search_miss(1)
            record_search_miss(1)
            record_search_miss(1)

        row = self.cursor.execute(
            "SELECT consecutive_misses, total_searches "
            "FROM volume_search_stats WHERE volume_id = 1"
        ).fetchone()
        self.assertEqual(row['consecutive_misses'], 3)
        self.assertEqual(row['total_searches'], 3)


# ---------------------------------------------------------------------------
# Tests: record_search_hit
# ---------------------------------------------------------------------------

class TestRecordSearchHit(unittest.TestCase):

    def setUp(self) -> None:
        self.cursor = _make_cursor()
        _insert_volume(self.cursor, 1)

    def test_hit_after_misses_resets_consecutive(self) -> None:
        """A hit after misses should reset consecutive_misses to 0."""
        with patch(
            'backend.internals.db_models_search_stats.get_db',
            return_value=self.cursor
        ):
            from backend.internals.db_models_search_stats import (
                record_search_hit, record_search_miss)
            record_search_miss(1)
            record_search_miss(1)
            record_search_hit(1)

        row = self.cursor.execute(
            "SELECT consecutive_misses, total_searches, total_hits "
            "FROM volume_search_stats WHERE volume_id = 1"
        ).fetchone()
        self.assertEqual(row['consecutive_misses'], 0)
        self.assertEqual(row['total_searches'], 3)
        self.assertEqual(row['total_hits'], 1)

    def test_first_hit_creates_row(self) -> None:
        """First hit for a volume with no prior stats creates a row."""
        with patch(
            'backend.internals.db_models_search_stats.get_db',
            return_value=self.cursor
        ):
            from backend.internals.db_models_search_stats import \
                record_search_hit
            record_search_hit(1)

        row = self.cursor.execute(
            "SELECT consecutive_misses, total_hits "
            "FROM volume_search_stats WHERE volume_id = 1"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row['consecutive_misses'], 0)
        self.assertEqual(row['total_hits'], 1)


# ---------------------------------------------------------------------------
# Tests: reset_misses
# ---------------------------------------------------------------------------

class TestResetMisses(unittest.TestCase):

    def setUp(self) -> None:
        self.cursor = _make_cursor()
        _insert_volume(self.cursor, 1)
        _insert_volume(self.cursor, 999)  # volume with no stats

    def test_reset_clears_consecutive_misses(self) -> None:
        """reset_misses should set consecutive_misses to 0."""
        # Seed some misses
        self.cursor.execute(
            "INSERT INTO volume_search_stats"
            "(volume_id, consecutive_misses, total_searches) VALUES (1, 5, 5)"
        )
        self.cursor.connection.commit()

        with patch(
            'backend.internals.db_models_search_stats.get_db',
            return_value=self.cursor
        ):
            from backend.internals.db_models_search_stats import reset_misses
            reset_misses(1)

        row = self.cursor.execute(
            "SELECT consecutive_misses "
            "FROM volume_search_stats WHERE volume_id = 1"
        ).fetchone()
        self.assertEqual(row['consecutive_misses'], 0)

    def test_reset_noop_for_volume_with_no_stats(self) -> None:
        """reset_misses on a volume with no stats row should not crash."""
        with patch(
            'backend.internals.db_models_search_stats.get_db',
            return_value=self.cursor
        ):
            from backend.internals.db_models_search_stats import reset_misses

            # Should not raise
            reset_misses(999)


# ---------------------------------------------------------------------------
# Tests: get_total_volume_count
# ---------------------------------------------------------------------------

class TestGetTotalVolumeCount(unittest.TestCase):

    def setUp(self) -> None:
        self.cursor = _make_cursor()

    def test_counts_only_monitored_volumes(self) -> None:
        """Only monitored volumes should be counted."""
        _insert_volume(self.cursor, 1, monitored=1)
        _insert_volume(self.cursor, 2, monitored=1)
        _insert_volume(self.cursor, 3, monitored=0)  # not monitored

        with patch(
            'backend.internals.db_models_search_stats.get_db',
            return_value=self.cursor
        ):
            from backend.internals.db_models_search_stats import \
                get_total_volume_count
            count = get_total_volume_count()

        self.assertEqual(count, 2)

    def test_zero_when_no_volumes(self) -> None:
        """Returns 0 when there are no monitored volumes."""
        with patch(
            'backend.internals.db_models_search_stats.get_db',
            return_value=self.cursor
        ):
            from backend.internals.db_models_search_stats import \
                get_total_volume_count
            count = get_total_volume_count()

        self.assertEqual(count, 0)


# ---------------------------------------------------------------------------
# Tests: cascade delete
# ---------------------------------------------------------------------------

class TestCascadeDelete(unittest.TestCase):

    def setUp(self) -> None:
        self.cursor = _make_cursor()

    def test_stats_deleted_when_volume_deleted(self) -> None:
        """Deleting a volume should cascade-delete its stats row."""
        _insert_volume(self.cursor, 10)
        self.cursor.execute(
            "INSERT INTO volume_search_stats"
            "(volume_id, consecutive_misses) VALUES (10, 3)"
        )
        self.cursor.connection.commit()

        # Verify stats row exists
        row = self.cursor.execute(
            "SELECT volume_id FROM volume_search_stats WHERE volume_id = 10"
        ).fetchone()
        self.assertIsNotNone(row)

        # Delete the volume
        self.cursor.execute("DELETE FROM volumes WHERE id = 10")
        self.cursor.connection.commit()

        # Stats row should be gone
        row = self.cursor.execute(
            "SELECT volume_id FROM volume_search_stats WHERE volume_id = 10"
        ).fetchone()
        self.assertIsNone(row)


if __name__ == '__main__':
    unittest.main()
