"""
Unit tests for DB migration #48: split update_all into 4 focused tasks.

Verifies:
- update_all row removed from task_intervals
- 4 new rows added (sync_issues, refresh_metadata, scan_files,
  special_version_refresh)
- last_issue_sync config key created
"""

import sqlite3
import unittest
from unittest.mock import patch

from backend.internals.db import KapowarrCursor

# Minimal schema matching what migration 48 starts from
_SCHEMA_PRE = """
    CREATE TABLE config (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE task_intervals (
        task_name TEXT PRIMARY KEY,
        interval INTEGER NOT NULL,
        next_run INTEGER NOT NULL DEFAULT 0
    );

    INSERT INTO task_intervals (task_name, interval, next_run)
        VALUES ('update_all', 86400, 0);
"""


def _make_cursor() -> KapowarrCursor:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    cursor = KapowarrCursor(conn)
    cursor.row_factory = sqlite3.Row
    cursor.executescript(_SCHEMA_PRE)
    return cursor


class TestMigration48(unittest.TestCase):
    """Migration 48 replaces update_all with 4 focused task intervals."""

    def _run_migration(self):
        from backend.internals.db_migration import DatabaseMigrationHandler
        handler = DatabaseMigrationHandler.handlers.get(48)
        self.assertIsNotNone(handler, "Migration 48->49 not registered")

        cursor = _make_cursor()
        with patch(
            'backend.internals.db_migration.get_db',
            return_value=cursor
        ):
            handler()
        return cursor

    def test_update_all_removed(self):
        cursor = self._run_migration()
        row = cursor.execute(
            "SELECT * FROM task_intervals WHERE task_name = 'update_all'"
        ).fetchone()
        self.assertIsNone(row, "update_all should be removed")

    def test_four_new_intervals_created(self):
        cursor = self._run_migration()
        rows = cursor.execute(
            "SELECT task_name, interval FROM task_intervals "
            "ORDER BY task_name"
        ).fetchall()
        names = {r['task_name'] for r in rows}
        self.assertEqual(names, {
            'sync_issues',
            'refresh_metadata',
            'scan_files',
            'special_version_refresh',
        })

    def test_sync_issues_daily(self):
        cursor = self._run_migration()
        row = cursor.execute(
            "SELECT interval FROM task_intervals "
            "WHERE task_name = 'sync_issues'"
        ).fetchonedict()
        self.assertEqual(row['interval'], 86400)

    def test_refresh_metadata_weekly(self):
        cursor = self._run_migration()
        row = cursor.execute(
            "SELECT interval FROM task_intervals "
            "WHERE task_name = 'refresh_metadata'"
        ).fetchonedict()
        self.assertEqual(row['interval'], 604800)

    def test_last_issue_sync_config(self):
        cursor = self._run_migration()
        row = cursor.execute(
            "SELECT value FROM config WHERE key = 'last_issue_sync'"
        ).fetchonedict()
        self.assertIsNotNone(row)
        self.assertEqual(row['value'], '0')


if __name__ == '__main__':
    unittest.main()
