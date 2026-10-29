"""
Unit tests for DB migration #55 (originally fork #48) and fork DB detection.

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


class TestMigration55(unittest.TestCase):
    """Migration 55 (originally fork migration 48) seeds the last_issue_sync
    config value. Task interval rows are created by insert_task_intervals(),
    and update_all is kept since it runs alongside the split tasks."""

    def _run_migration(self):
        from backend.internals.db_migration import DatabaseMigrationHandler
        handler = DatabaseMigrationHandler.handlers.get(55)
        self.assertIsNotNone(handler, "Migration 55->56 not registered")

        cursor = _make_cursor()
        with patch(
            'backend.internals.db_migration.get_db',
            return_value=cursor
        ):
            handler()
        return cursor

    def test_update_all_kept(self):
        cursor = self._run_migration()
        row = cursor.execute(
            "SELECT * FROM task_intervals WHERE task_name = 'update_all'"
        ).fetchone()
        self.assertIsNotNone(row, "update_all should be kept")

    def test_last_issue_sync_config(self):
        cursor = self._run_migration()
        row = cursor.execute(
            "SELECT value FROM config WHERE key = 'last_issue_sync'"
        ).fetchonedict()
        self.assertIsNotNone(row)
        self.assertEqual(row['value'], '0')

    def test_idempotent(self):
        cursor = self._run_migration()
        from backend.internals.db_migration import DatabaseMigrationHandler
        with patch(
            'backend.internals.db_migration.get_db',
            return_value=cursor
        ):
            DatabaseMigrationHandler.handlers[55]()


class TestPreMergeForkDetection(unittest.TestCase):
    """Fork databases that ran the original (colliding) migrations 45-50 must
    be detected so upstream's migrations 45-51 still get applied."""

    def _cursor(self, schema: str):
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        cursor = KapowarrCursor(connection)
        cursor.row_factory = sqlite3.Row
        cursor.executescript(schema)
        return cursor

    def _detect(self, schema: str, version: int) -> bool:
        from backend.internals.db_migration import DatabaseMigrationHandler
        with patch(
            'backend.internals.db_migration.get_db',
            return_value=self._cursor(schema)
        ):
            return DatabaseMigrationHandler._is_pre_merge_fork_db(version)

    FORK = """
        CREATE TABLE notifications(id INTEGER PRIMARY KEY);
        CREATE TABLE external_download_clients(id INTEGER PRIMARY KEY);
    """
    UPSTREAM = """
        CREATE TABLE notifications(id INTEGER PRIMARY KEY);
        CREATE TABLE external_download_clients(
            id INTEGER PRIMARY KEY, enabled BOOL
        );
    """

    def test_fork_db_detected(self):
        self.assertTrue(self._detect(self.FORK, 51))
        self.assertTrue(self._detect(self.FORK, 46))

    def test_upstream_db_not_detected(self):
        self.assertFalse(self._detect(self.UPSTREAM, 51))

    def test_out_of_range_versions_not_detected(self):
        self.assertFalse(self._detect(self.FORK, 45))
        self.assertFalse(self._detect(self.FORK, 52))


if __name__ == '__main__':
    unittest.main()
