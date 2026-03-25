"""
Unit tests for the 4 new task classes replacing UpdateAll:
SyncIssues, RefreshMetadata, ScanFiles, SpecialVersionRefresh
"""

import sqlite3
import sys
import unittest
from unittest.mock import MagicMock, patch

from backend.internals.db import KapowarrCursor

# Avoid grp import failure on Windows
if 'grp' not in sys.modules:
    sys.modules['grp'] = MagicMock()

_SCHEMA = """
    CREATE TABLE config (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE volumes (
        id INTEGER PRIMARY KEY,
        comicvine_id INTEGER UNIQUE,
        title TEXT,
        alt_title TEXT,
        year INTEGER,
        publisher TEXT,
        volume_number INTEGER,
        description TEXT,
        site_url TEXT,
        last_cv_fetch REAL DEFAULT 0,
        special_version TEXT DEFAULT 'normal',
        special_version_locked INTEGER DEFAULT 0,
        monitor_new_issues INTEGER DEFAULT 1,
        monitored INTEGER DEFAULT 1
    );
    CREATE TABLE volumes_covers (
        volume_id INTEGER PRIMARY KEY,
        cover TEXT,
        FOREIGN KEY (volume_id) REFERENCES volumes(id)
    );
    CREATE TABLE issues (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        volume_id INTEGER,
        comicvine_id INTEGER UNIQUE,
        issue_number TEXT,
        calculated_issue_number REAL,
        title TEXT,
        date TEXT,
        description TEXT,
        monitored INTEGER DEFAULT 1,
        FOREIGN KEY (volume_id) REFERENCES volumes(id)
    );
    INSERT INTO config (key, value) VALUES ('last_issue_sync', '0');
"""


def _make_cursor():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    cursor = KapowarrCursor(conn)
    cursor.row_factory = sqlite3.Row
    cursor.executescript(_SCHEMA)
    return cursor


def _seed(cursor, vid=1, cv_id=100, monitored=1):
    cursor.execute(
        "INSERT INTO volumes "
        "(id, comicvine_id, title, monitored, last_cv_fetch) "
        "VALUES (?,?,?,?,?)",
        (vid, cv_id, f'Vol {vid}', monitored, 0)
    )
    cursor.execute(
        "INSERT INTO volumes_covers (volume_id, cover) VALUES (?,?)",
        (vid, None)
    )
    cursor.connection.commit()


# =====================
# SyncIssues
# =====================

class TestSyncIssues(unittest.TestCase):

    def test_class_attributes(self):
        from backend.features.tasks import SyncIssues
        self.assertEqual(SyncIssues.action, 'sync_issues')
        self.assertEqual(SyncIssues.priority, 3)

    def test_reads_last_issue_sync_and_calls_fetch(self):
        cursor = _make_cursor()
        _seed(cursor)
        cursor.execute(
            "UPDATE config SET value='1000' WHERE key='last_issue_sync'"
        )
        cursor.connection.commit()

        mock_cv = MagicMock()
        mock_cv.fetch_issues_since = MagicMock(return_value=[])

        from backend.features.tasks import SyncIssues
        task = SyncIssues()
        task.stop = False

        with patch(
            'backend.implementations.volumes.get_db',
            return_value=cursor
        ), patch(
            'backend.features.tasks.get_db',
            return_value=cursor
        ), patch(
            'backend.features.tasks.ComicVine',
            return_value=mock_cv
        ), patch(
            'backend.features.tasks.WebSocket'
        ), patch(
            'backend.features.tasks.run',
            side_effect=lambda coro: coro
        ), patch(
            'backend.implementations.volumes.get_db',
            return_value=cursor
        ), patch(
            'backend.implementations.volumes.commit',
            side_effect=lambda: cursor.connection.commit()
        ):
            task.run()

        # Verify fetch_issues_since was called
        mock_cv.fetch_issues_since.assert_called_once()
        call_args = mock_cv.fetch_issues_since.call_args
        # since should be 1000 - 7200 = -6200 (clamped or not)
        self.assertEqual(call_args[0][1], 1000 - 7200)

    def test_updates_last_issue_sync_on_success(self):
        cursor = _make_cursor()
        _seed(cursor)

        mock_cv = MagicMock()
        mock_cv.fetch_issues_since = MagicMock(return_value=[])

        from backend.features.tasks import SyncIssues
        task = SyncIssues()
        task.stop = False

        with patch(
            'backend.implementations.volumes.get_db',
            return_value=cursor
        ), patch(
            'backend.features.tasks.get_db',
            return_value=cursor
        ), patch(
            'backend.features.tasks.ComicVine',
            return_value=mock_cv
        ), patch(
            'backend.features.tasks.WebSocket'
        ), patch(
            'backend.features.tasks.run',
            side_effect=lambda coro: coro
        ), patch(
            'backend.implementations.volumes.commit',
            side_effect=lambda: cursor.connection.commit()
        ):
            task.run()

        row = cursor.execute(
            "SELECT value FROM config WHERE key='last_issue_sync'"
        ).fetchonedict()
        self.assertNotEqual(row['value'], '0')


# =====================
# RefreshMetadata
# =====================

class TestRefreshMetadata(unittest.TestCase):

    def test_class_attributes(self):
        from backend.features.tasks import RefreshMetadata
        self.assertEqual(RefreshMetadata.action, 'refresh_metadata')
        self.assertEqual(RefreshMetadata.priority, 3)

    def test_queries_stale_volumes(self):
        cursor = _make_cursor()
        # Volume with last_cv_fetch = 0 (very stale)
        _seed(cursor, vid=1, cv_id=100)

        mock_cv = MagicMock()
        mock_cv.fetch_volumes = MagicMock(return_value=[])

        from backend.features.tasks import RefreshMetadata
        task = RefreshMetadata()
        task.stop = False

        with patch(
            'backend.features.tasks.get_db',
            return_value=cursor
        ), patch(
            'backend.features.tasks.ComicVine',
            return_value=mock_cv
        ), patch(
            'backend.features.tasks.WebSocket'
        ), patch(
            'backend.features.tasks.run',
            side_effect=lambda coro: coro
        ), patch(
            'backend.implementations.volumes.get_db',
            return_value=cursor
        ), patch(
            'backend.implementations.volumes.commit',
            side_effect=lambda: cursor.connection.commit()
        ):
            task.run()

        mock_cv.fetch_volumes.assert_called_once()
        call_args = mock_cv.fetch_volumes.call_args[0][0]
        self.assertIn(100, call_args)


# =====================
# ScanFiles
# =====================

class TestScanFiles(unittest.TestCase):

    def test_class_attributes(self):
        from backend.features.tasks import ScanFiles
        self.assertEqual(ScanFiles.action, 'scan_files')
        self.assertEqual(ScanFiles.priority, 3)

    def test_iterates_volumes(self):
        cursor = _make_cursor()
        _seed(cursor, vid=1, cv_id=100)
        _seed(cursor, vid=2, cv_id=200)

        scanned = []
        mock_scan = MagicMock(side_effect=lambda vid, **kw: scanned.append(vid))

        from backend.features.tasks import ScanFiles
        task = ScanFiles()
        task.stop = False

        with patch(
            'backend.features.tasks.get_db',
            return_value=cursor
        ), patch(
            'backend.features.tasks.WebSocket'
        ), patch(
            'backend.features.tasks.scan_files',
            mock_scan
        ):
            task.run()

        self.assertEqual(sorted(scanned), [1, 2])


# =====================
# SpecialVersionRefresh
# =====================

class TestSpecialVersionRefresh(unittest.TestCase):

    def test_class_attributes(self):
        from backend.features.tasks import SpecialVersionRefresh
        self.assertEqual(
            SpecialVersionRefresh.action,
            'special_version_refresh'
        )
        self.assertEqual(SpecialVersionRefresh.priority, 4)

    def test_processes_unlocked_volumes(self):
        cursor = _make_cursor()
        _seed(cursor, vid=1, cv_id=100)
        # Lock volume 2 — should be skipped
        _seed(cursor, vid=2, cv_id=200)
        cursor.execute(
            "UPDATE volumes SET special_version_locked=1 WHERE id=2"
        )
        cursor.connection.commit()

        from backend.features.tasks import SpecialVersionRefresh
        task = SpecialVersionRefresh()
        task.stop = False

        with patch(
            'backend.features.tasks.get_db',
            return_value=cursor
        ), patch(
            'backend.features.tasks.WebSocket'
        ), patch(
            'backend.features.tasks.determine_special_version',
            return_value='tpb'
        ), patch(
            'backend.features.tasks.commit',
            side_effect=lambda: cursor.connection.commit()
        ):
            task.run()

        r1 = cursor.execute(
            "SELECT special_version FROM volumes WHERE id=1"
        ).fetchonedict()
        r2 = cursor.execute(
            "SELECT special_version FROM volumes WHERE id=2"
        ).fetchonedict()
        self.assertEqual(r1['special_version'], 'tpb')
        # Locked volume should retain original
        self.assertEqual(r2['special_version'], 'normal')


if __name__ == '__main__':
    unittest.main()
