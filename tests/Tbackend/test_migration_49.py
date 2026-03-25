"""Regression tests for DB migration #49 and settings validation."""

import sqlite3
import sys
import unittest
from unittest.mock import patch

from backend.base.definitions import ProxyType
from backend.internals.db import KapowarrCursor

if 'grp' not in sys.modules:
    from unittest.mock import MagicMock
    sys.modules['grp'] = MagicMock()


_TASK_HISTORY_SCHEMA_PRE = """
    CREATE TABLE task_history (
        task_name TEXT NOT NULL,
        display_title TEXT NOT NULL,
        run_at INTEGER NOT NULL
    );
"""


_TASK_HISTORY_SCHEMA_POST = """
    CREATE TABLE task_history (
        task_name TEXT NOT NULL,
        display_title TEXT NOT NULL,
        run_at INTEGER NOT NULL,
        duration_seconds INTEGER
    );
"""


def _make_cursor(schema: str) -> KapowarrCursor:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    cursor = KapowarrCursor(conn)
    cursor.row_factory = sqlite3.Row
    cursor.executescript(schema)
    return cursor


class TestMigration49(unittest.TestCase):
    """Migration 49 should safely add task duration support."""

    def _run_migration(self, schema: str) -> KapowarrCursor:
        from backend.internals.db_migration import DatabaseMigrationHandler

        handler = DatabaseMigrationHandler.handlers.get(49)
        self.assertIsNotNone(handler, "Migration 49->50 not registered")

        cursor = _make_cursor(schema)
        with patch('backend.internals.db_migration.get_db', return_value=cursor):
            handler()
        return cursor

    def test_adds_duration_column_when_missing(self) -> None:
        cursor = self._run_migration(_TASK_HISTORY_SCHEMA_PRE)
        columns = cursor.execute(
            "PRAGMA table_info(task_history);"
        ).fetchall()
        column_names = {column['name'] for column in columns}
        self.assertIn('duration_seconds', column_names)

    def test_skips_when_duration_column_already_exists(self) -> None:
        cursor = self._run_migration(_TASK_HISTORY_SCHEMA_POST)
        columns = cursor.execute(
            "PRAGMA table_info(task_history);"
        ).fetchall()
        duration_columns = [
            column['name'] for column in columns
            if column['name'] == 'duration_seconds'
        ]
        self.assertEqual(duration_columns, ['duration_seconds'])


class TestSettingsValidation(unittest.TestCase):
    """Settings validation should keep enum-typed proxy settings intact."""

    def test_database_version_update_preserves_proxy_enum(self) -> None:
        from backend.internals.settings import Settings, SettingsValues

        settings = object.__new__(Settings)
        settings.get_settings = lambda: SettingsValues(
            proxy_type=ProxyType.HTTP,
            proxy_host='proxy.internal',
            proxy_port=8080,
            proxy_username='',
            proxy_password=''
        )

        with patch(
            'backend.internals.settings.build_proxy_url',
            return_value='http://proxy.internal:8080'
        ) as build_proxy_url, patch(
            'backend.internals.settings.test_proxy_url'
        ):
            settings._Settings__validate_settings({'database_version': 49})

        self.assertIs(build_proxy_url.call_args.args[0], ProxyType.HTTP)


class TestDbSchema(unittest.TestCase):
    """Fresh database schema should match the latest task history shape."""

    def test_task_history_schema_includes_duration_column(self) -> None:
        from backend.internals.db import DB_SCHEMA

        cursor = _make_cursor(DB_SCHEMA)
        columns = cursor.execute(
            "PRAGMA table_info(task_history);"
        ).fetchall()
        column_names = {column['name'] for column in columns}
        self.assertIn('duration_seconds', column_names)


if __name__ == '__main__':
    unittest.main()
