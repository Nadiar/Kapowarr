"""Tests for the fork-only schema reconciler (`fork_schema.py`)."""

import sqlite3
import sys
import unittest
from unittest.mock import MagicMock, patch

if 'grp' not in sys.modules:
    sys.modules['grp'] = MagicMock()

from backend.internals import fork_schema
from backend.internals.db import KapowarrCursor

UPSTREAM_BASE = """
    CREATE TABLE config(key VARCHAR(100) PRIMARY KEY, value BLOB);
    CREATE TABLE volumes(id INTEGER PRIMARY KEY);
    CREATE TABLE task_history(
        task_name NOT NULL,
        display_title NOT NULL,
        run_at INTEGER NOT NULL
    );
    CREATE TABLE external_download_clients(
        id INTEGER PRIMARY KEY, enabled BOOL
    );
"""

PRE_MERGE_FORK_BASE = """
    CREATE TABLE config(key VARCHAR(100) PRIMARY KEY, value BLOB);
    CREATE TABLE notifications(id INTEGER PRIMARY KEY);
    CREATE TABLE external_download_clients(id INTEGER PRIMARY KEY);
"""


def _make_cursor(schema: str) -> KapowarrCursor:
    conn = sqlite3.connect(':memory:', check_same_thread=False)
    conn.row_factory = sqlite3.Row
    cursor = KapowarrCursor(conn)
    cursor.row_factory = sqlite3.Row
    cursor.executescript(schema)
    return cursor


def _columns(cursor: KapowarrCursor, table: str) -> set:
    return {
        r['name']
        for r in cursor.execute(f'PRAGMA table_info({table});').fetchall()
    }


def _config(cursor: KapowarrCursor) -> dict:
    return {
        r['key']: r['value']
        for r in cursor.execute('SELECT key, value FROM config;').fetchall()
    }


class TestReconcileForkSchema(unittest.TestCase):

    def _reconcile(self, cursor: KapowarrCursor) -> None:
        with patch.object(fork_schema, 'get_db', return_value=cursor):
            fork_schema.reconcile_fork_schema()

    def test_creates_fork_tables_on_upstream_db(self) -> None:
        cursor = _make_cursor(UPSTREAM_BASE)
        self._reconcile(cursor)
        tables = {
            r[0] for r in cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table';"
            ).fetchall()
        }
        self.assertTrue({
            'notifications', 'volume_search_stats', 'volume_publisher_cache'
        } <= tables)

    def test_volume_publisher_cache_columns(self) -> None:
        cursor = _make_cursor(UPSTREAM_BASE)
        self._reconcile(cursor)
        self.assertEqual(
            _columns(cursor, 'volume_publisher_cache'),
            {'comicvine_id', 'volume_name', 'publisher_name',
             'publisher_id', 'cached_at'}
        )

    def test_adds_missing_column_to_upstream_table(self) -> None:
        cursor = _make_cursor(UPSTREAM_BASE)
        self._reconcile(cursor)
        self.assertIn('duration_seconds', _columns(cursor, 'task_history'))

    def test_seeds_config_defaults(self) -> None:
        cursor = _make_cursor(UPSTREAM_BASE)
        self._reconcile(cursor)
        config = _config(cursor)
        self.assertEqual(config['last_issue_sync'], '0')
        self.assertEqual(
            config['comicvine_api_url'], 'https://comicvine.gamespot.com/api'
        )

    def test_keeps_existing_config_values(self) -> None:
        cursor = _make_cursor(UPSTREAM_BASE)
        cursor.execute(
            "INSERT INTO config VALUES ('comicvine_api_url', 'http://cv/api');"
        )
        cursor.execute(
            "INSERT INTO config VALUES ('last_issue_sync', '1700000000');"
        )
        self._reconcile(cursor)
        config = _config(cursor)
        self.assertEqual(config['comicvine_api_url'], 'http://cv/api')
        self.assertEqual(config['last_issue_sync'], '1700000000')

    def test_idempotent_and_keeps_data(self) -> None:
        cursor = _make_cursor(UPSTREAM_BASE)
        self._reconcile(cursor)
        cursor.execute(
            "INSERT INTO notifications(name, provider_type) "
            "VALUES ('n', 'webhook');"
        )
        self._reconcile(cursor)
        self.assertEqual(
            cursor.execute('SELECT COUNT(*) FROM notifications;').fetchone()[0],
            1
        )

    def test_existing_column_not_duplicated(self) -> None:
        cursor = _make_cursor(UPSTREAM_BASE)
        cursor.execute(
            'ALTER TABLE task_history ADD COLUMN duration_seconds INTEGER;'
        )
        self._reconcile(cursor)
        names = [
            r['name'] for r in cursor.execute(
                'PRAGMA table_info(task_history);'
            ).fetchall()
        ]
        self.assertEqual(names.count('duration_seconds'), 1)


class TestPreMergeForkDetection(unittest.TestCase):
    """Fork databases that ran the original (colliding) migrations 45-50 must
    be detected so upstream's migrations 45-51 still get applied."""

    def _detect(self, schema: str, version: int) -> bool:
        with patch.object(
            fork_schema, 'get_db', return_value=_make_cursor(schema)
        ):
            return fork_schema._is_pre_merge_fork_db(version)

    def test_fork_db_detected(self) -> None:
        self.assertTrue(self._detect(PRE_MERGE_FORK_BASE, 51))
        self.assertTrue(self._detect(PRE_MERGE_FORK_BASE, 46))

    def test_upstream_db_not_detected(self) -> None:
        schema = UPSTREAM_BASE + (
            'CREATE TABLE notifications(id INTEGER PRIMARY KEY);'
        )
        self.assertFalse(self._detect(schema, 51))

    def test_out_of_range_versions_not_detected(self) -> None:
        self.assertFalse(self._detect(PRE_MERGE_FORK_BASE, 45))
        self.assertFalse(self._detect(PRE_MERGE_FORK_BASE, 52))


class TestRewindPreMergeForkDb(unittest.TestCase):

    def _rewind(self, schema: str, version: int):
        cursor = _make_cursor(schema)
        cursor.execute(
            "INSERT INTO config VALUES ('database_version', ?);", (version,)
        )
        settings = MagicMock()
        settings.sv.database_version = version
        with patch.object(fork_schema, 'get_db', return_value=cursor), patch(
            'backend.internals.settings.Settings', return_value=settings
        ):
            fork_schema.rewind_pre_merge_fork_db()
        return cursor, settings

    def test_rewinds_fork_db_without_settings_validation(self) -> None:
        cursor, settings = self._rewind(PRE_MERGE_FORK_BASE, 51)
        self.assertEqual(_config(cursor)['database_version'], 45)
        settings.clear_cache.assert_called_once()
        settings.update.assert_not_called()

    def test_leaves_upstream_db_alone(self) -> None:
        cursor, settings = self._rewind(UPSTREAM_BASE, 51)
        self.assertEqual(_config(cursor)['database_version'], 51)
        settings.clear_cache.assert_not_called()


if __name__ == '__main__':
    unittest.main()
