"""
Tests for the volume_publisher_cache integration in _fetch_missing_volumes.

Uses an in-memory SQLite DB so no live Kapowarr instance is needed.
"""
import sqlite3
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

if 'grp' not in sys.modules:
    sys.modules['grp'] = MagicMock()

from backend.internals.db import KapowarrCursor

_CACHE_SCHEMA = """
    CREATE TABLE volume_publisher_cache (
        comicvine_id   INTEGER PRIMARY KEY,
        volume_name    TEXT    NOT NULL DEFAULT '',
        publisher_name TEXT,
        publisher_id   INTEGER,
        cached_at      REAL    NOT NULL
    );
"""


def _make_cursor() -> KapowarrCursor:
    conn = sqlite3.connect(':memory:', check_same_thread=False)
    conn.row_factory = sqlite3.Row
    cursor = KapowarrCursor(conn)
    cursor.row_factory = sqlite3.Row
    cursor.executescript(_CACHE_SCHEMA)
    return cursor


class TestFetchMissingVolumesCache(unittest.TestCase):
    """_fetch_missing_volumes must use the SQLite cache to skip CVProxy calls."""

    def _make_fake_cv(self, raw_vols):
        """Return a mock ComicVine whose fetch_volumes_for_enrichment returns raw_vols."""
        mock_cv = MagicMock()
        mock_cv.fetch_volumes_for_enrichment = MagicMock(return_value=raw_vols)
        return mock_cv

    def test_cache_miss_calls_cvproxy_and_stores_result(self):
        """On a cold cache, CVProxy is called and the result is stored."""
        cursor = _make_cursor()
        volume_map = {}
        raw_vols = [
            {'id': '101', 'name': 'Alpha', 'publisher': {'id': '10', 'name': 'Marvel'}},
        ]
        mock_cv = self._make_fake_cv(raw_vols)

        with patch('backend.features.calendar_cv.get_db', return_value=cursor), \
             patch('backend.features.calendar_cv.ComicVine', return_value=mock_cv), \
             patch('backend.features.calendar_cv.run',
                   side_effect=lambda coro: raw_vols):
            from backend.features.calendar_cv import _fetch_missing_volumes
            _fetch_missing_volumes([101], volume_map)

        self.assertIn(101, volume_map)
        self.assertEqual(volume_map[101]['volume_name'], 'Alpha')

        row = cursor.execute(
            'SELECT * FROM volume_publisher_cache WHERE comicvine_id = 101;'
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row['volume_name'], 'Alpha')

    def test_cache_hit_skips_cvproxy(self):
        """A fresh cached entry -> CVProxy is NOT called."""
        cursor = _make_cursor()
        now = time.time()
        with cursor:
            cursor.execute(
                'INSERT INTO volume_publisher_cache '
                '(comicvine_id, volume_name, publisher_name, publisher_id, cached_at) '
                'VALUES (?, ?, ?, ?, ?);',
                (201, 'Beta', 'DC Comics', 20, now)
            )

        volume_map = {}
        mock_cv = MagicMock()

        with patch('backend.features.calendar_cv.get_db', return_value=cursor), \
             patch('backend.features.calendar_cv.ComicVine', return_value=mock_cv), \
             patch('backend.features.calendar_cv.run') as mock_run:
            from backend.features.calendar_cv import _fetch_missing_volumes
            _fetch_missing_volumes([201], volume_map)

        mock_run.assert_not_called()
        self.assertIn(201, volume_map)
        self.assertEqual(volume_map[201]['publisher_name'], 'DC Comics')

    def test_expired_cache_entry_triggers_refresh(self):
        """An expired entry (older than TTL) must be re-fetched from CVProxy."""
        from backend.features.calendar_cv import VOLUME_PUBLISHER_CACHE_TTL
        cursor = _make_cursor()
        stale_time = time.time() - VOLUME_PUBLISHER_CACHE_TTL - 1
        with cursor:
            cursor.execute(
                'INSERT INTO volume_publisher_cache '
                '(comicvine_id, volume_name, publisher_name, publisher_id, cached_at) '
                'VALUES (?, ?, ?, ?, ?);',
                (301, 'Gamma', 'Image', 30, stale_time)
            )

        volume_map = {}
        raw_vols = [
            {'id': '301', 'name': 'Gamma Updated',
             'publisher': {'id': '30', 'name': 'Image Updated'}},
        ]
        mock_cv = self._make_fake_cv(raw_vols)

        with patch('backend.features.calendar_cv.get_db', return_value=cursor), \
             patch('backend.features.calendar_cv.ComicVine', return_value=mock_cv), \
             patch('backend.features.calendar_cv.run',
                   side_effect=lambda coro: raw_vols):
            from backend.features.calendar_cv import _fetch_missing_volumes
            _fetch_missing_volumes([301], volume_map)

        self.assertEqual(volume_map[301]['volume_name'], 'Gamma Updated')

    def test_empty_volume_ids_is_noop(self):
        """Empty input must not touch the DB or CVProxy."""
        cursor = _make_cursor()
        volume_map = {}

        with patch('backend.features.calendar_cv.get_db', return_value=cursor), \
             patch('backend.features.calendar_cv.ComicVine') as MockCV, \
             patch('backend.features.calendar_cv.run') as mock_run:
            from backend.features.calendar_cv import _fetch_missing_volumes
            _fetch_missing_volumes([], volume_map)

        MockCV.assert_not_called()
        mock_run.assert_not_called()
        self.assertEqual(volume_map, {})


if __name__ == '__main__':
    unittest.main()
