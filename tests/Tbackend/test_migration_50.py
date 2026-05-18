"""Smoke test for DB migration #50: volume_publisher_cache table."""
import sqlite3
import sys
import unittest
from unittest.mock import MagicMock, patch

if 'grp' not in sys.modules:
    sys.modules['grp'] = MagicMock()

from backend.internals.db import KapowarrCursor


def _make_cursor(schema: str = '') -> KapowarrCursor:
    conn = sqlite3.connect(':memory:', check_same_thread=False)
    conn.row_factory = sqlite3.Row
    cursor = KapowarrCursor(conn)
    cursor.row_factory = sqlite3.Row
    if schema:
        cursor.executescript(schema)
    return cursor


class TestMigration50(unittest.TestCase):
    """Migration #50 must create volume_publisher_cache on an empty DB."""

    def _run_migration(self, schema: str = '') -> KapowarrCursor:
        from backend.internals.db_migration import DatabaseMigrationHandler
        handler = DatabaseMigrationHandler.handlers.get(50)
        self.assertIsNotNone(handler, 'Migration 50->51 not registered')
        cursor = _make_cursor(schema)
        with patch('backend.internals.db_migration.get_db', return_value=cursor):
            handler()
        return cursor

    def test_creates_volume_publisher_cache_table(self):
        cursor = self._run_migration()
        tables = {
            row[0]
            for row in cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table';"
            ).fetchall()
        }
        self.assertIn('volume_publisher_cache', tables)

    def test_table_has_expected_columns(self):
        cursor = self._run_migration()
        cols = {
            row['name']
            for row in cursor.execute(
                'PRAGMA table_info(volume_publisher_cache);'
            ).fetchall()
        }
        self.assertEqual(
            cols,
            {'comicvine_id', 'volume_name', 'publisher_name',
             'publisher_id', 'cached_at'}
        )

    def test_idempotent_on_existing_table(self):
        """Running migration twice must not raise."""
        existing = """
            CREATE TABLE volume_publisher_cache (
                comicvine_id   INTEGER PRIMARY KEY,
                volume_name    TEXT    NOT NULL DEFAULT '',
                publisher_name TEXT,
                publisher_id   INTEGER,
                cached_at      REAL    NOT NULL
            );
        """
        cursor = self._run_migration(schema=existing)
        tables = {
            row[0]
            for row in cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table';"
            ).fetchall()
        }
        self.assertIn('volume_publisher_cache', tables)


if __name__ == '__main__':
    unittest.main()
