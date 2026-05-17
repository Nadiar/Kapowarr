"""
Unit tests for extracted helper functions from refresh_and_scan().

Tests: update_volume_metadata, upsert_issues, delete_orphaned_issues,
       refresh_special_versions
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

    CREATE TABLE issues_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        issue_id INTEGER,
        file_path TEXT,
        FOREIGN KEY (issue_id) REFERENCES issues(id)
    );
"""


def _make_cursor() -> KapowarrCursor:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    cursor = KapowarrCursor(conn)
    cursor.row_factory = sqlite3.Row
    cursor.executescript(_SCHEMA)
    return cursor


def _seed_volume(cursor, vid=1, cv_id=100, title='Vol 1'):
    cursor.execute(
        "INSERT INTO volumes (id, comicvine_id, title) VALUES (?,?,?)",
        (vid, cv_id, title)
    )
    cursor.execute(
        "INSERT INTO volumes_covers (volume_id, cover) VALUES (?,?)",
        (vid, None)
    )
    cursor.connection.commit()


def _seed_issue(cursor, iid, vid, cv_id, number='1'):
    cursor.execute(
        "INSERT INTO issues "
        "(id, volume_id, comicvine_id, issue_number, "
        "calculated_issue_number, title, date, description, monitored)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (iid, vid, cv_id, number, 1.0, 'Title', '2025-01-01', '', 1)
    )
    cursor.connection.commit()


class TestUpdateVolumeMetadata(unittest.TestCase):

    def test_updates_volume_fields(self):
        cursor = _make_cursor()
        _seed_volume(cursor, vid=1, cv_id=100, title='Old Title')

        cv_to_id_fetch = {100: (1, 0)}
        volume_datas = [{
            'comicvine_id': 100,
            'title': 'New Title',
            'aliases': ['Alt Name'],
            'year': 2025,
            'publisher': 'Marvel',
            'volume_number': 1,
            'description': 'Desc',
            'site_url': 'http://cv/100',
            'cover': 'http://img/cover.jpg',
            'issue_count': 5,
        }]

        with patch(
            'backend.implementations.volumes.get_db',
            return_value=cursor
        ), patch(
            'backend.implementations.volumes.commit',
            side_effect=lambda: cursor.connection.commit()
        ):
            from backend.implementations.volumes import update_volume_metadata
            update_volume_metadata(cv_to_id_fetch, volume_datas)

        row = cursor.execute(
            "SELECT title, publisher, alt_title FROM volumes WHERE id=1"
        ).fetchonedict()
        self.assertEqual(row['title'], 'New Title')
        self.assertEqual(row['publisher'], 'Marvel')
        self.assertEqual(row['alt_title'], 'Alt Name')

        cover_row = cursor.execute(
            "SELECT cover FROM volumes_covers WHERE volume_id=1"
        ).fetchonedict()
        self.assertEqual(cover_row['cover'], 'http://img/cover.jpg')


class TestUpsertIssues(unittest.TestCase):

    def test_inserts_new_issue(self):
        cursor = _make_cursor()
        _seed_volume(cursor, vid=1, cv_id=100)

        cv_to_id_fetch = {100: (1, 0)}
        issue_datas = [{
            'comicvine_id': 999,
            'volume_id': 100,
            'issue_number': '1',
            'calculated_issue_number': 1.0,
            'title': 'First Issue',
            'date': '2025-01-01',
            'description': 'Desc',
        }]

        with patch(
            'backend.implementations.volumes.get_db',
            return_value=cursor
        ), patch(
            'backend.implementations.volumes.commit',
            side_effect=lambda: cursor.connection.commit()
        ):
            from backend.implementations.volumes import upsert_issues
            upsert_issues(cv_to_id_fetch, issue_datas)

        row = cursor.execute(
            "SELECT * FROM issues WHERE comicvine_id=999"
        ).fetchonedict()
        self.assertIsNotNone(row)
        self.assertEqual(row['volume_id'], 1)
        self.assertEqual(row['title'], 'First Issue')

    def test_updates_existing_issue(self):
        cursor = _make_cursor()
        _seed_volume(cursor, vid=1, cv_id=100)
        _seed_issue(cursor, iid=10, vid=1, cv_id=999, number='1')

        cv_to_id_fetch = {100: (1, 0)}
        issue_datas = [{
            'comicvine_id': 999,
            'volume_id': 100,
            'issue_number': '1',
            'calculated_issue_number': 1.0,
            'title': 'Updated Title',
            'date': '2025-02-01',
            'description': 'New desc',
        }]

        with patch(
            'backend.implementations.volumes.get_db',
            return_value=cursor
        ), patch(
            'backend.implementations.volumes.commit',
            side_effect=lambda: cursor.connection.commit()
        ):
            from backend.implementations.volumes import upsert_issues
            upsert_issues(cv_to_id_fetch, issue_datas)

        row = cursor.execute(
            "SELECT title FROM issues WHERE comicvine_id=999"
        ).fetchonedict()
        self.assertEqual(row['title'], 'Updated Title')


class TestDeleteOrphanedIssues(unittest.TestCase):

    def test_deletes_issue_not_in_response(self):
        cursor = _make_cursor()
        _seed_volume(cursor, vid=1, cv_id=100)
        _seed_issue(cursor, iid=10, vid=1, cv_id=999)
        _seed_issue(cursor, iid=11, vid=1, cv_id=888)

        cv_to_id_fetch = {100: (1, 0)}
        # Only 999 is in the response, 888 should be deleted
        # issue_count=1 means all were fetched (matching response)
        volume_datas = [{'comicvine_id': 100, 'issue_count': 1}]
        issue_datas = [
            {'comicvine_id': 999, 'volume_id': 100},
        ]

        deleted_ids = []
        mock_issue_cls = MagicMock()

        def fake_issue(iid):
            inst = MagicMock()
            inst.delete.side_effect = lambda: deleted_ids.append(iid)
            return inst
        mock_issue_cls.side_effect = fake_issue

        with patch(
            'backend.implementations.volumes.get_db',
            return_value=cursor
        ), patch(
            'backend.implementations.volumes.commit',
            side_effect=lambda: cursor.connection.commit()
        ), patch(
            'backend.implementations.volumes.Issue',
            mock_issue_cls
        ):
            from backend.implementations.volumes import delete_orphaned_issues
            delete_orphaned_issues(
                cv_to_id_fetch, volume_datas, issue_datas
            )

        # Issue 11 (cv_id=888) should have been deleted
        self.assertEqual(len(deleted_ids), 1)
        self.assertEqual(deleted_ids[0], 11)


class TestRefreshSpecialVersions(unittest.TestCase):

    def test_updates_unlocked_volumes(self):
        cursor = _make_cursor()
        _seed_volume(cursor, vid=1, cv_id=100)

        cv_to_id_fetch = {100: (1, 0)}
        volume_datas = [{'comicvine_id': 100}]

        with patch(
            'backend.implementations.volumes.get_db',
            return_value=cursor
        ), patch(
            'backend.implementations.volumes.commit',
            side_effect=lambda: cursor.connection.commit()
        ), patch(
            'backend.implementations.volumes.determine_special_version',
            return_value='tpb'
        ):
            from backend.implementations.volumes import \
                refresh_special_versions
            refresh_special_versions(cv_to_id_fetch, volume_datas)

        row = cursor.execute(
            "SELECT special_version FROM volumes WHERE id=1"
        ).fetchonedict()
        self.assertEqual(row['special_version'], 'tpb')

    def test_skips_locked_volumes(self):
        cursor = _make_cursor()
        _seed_volume(cursor, vid=1, cv_id=100)
        cursor.execute(
            "UPDATE volumes SET special_version='hard-cover', "
            "special_version_locked=1 WHERE id=1"
        )
        cursor.connection.commit()

        cv_to_id_fetch = {100: (1, 0)}
        volume_datas = [{'comicvine_id': 100}]

        with patch(
            'backend.implementations.volumes.get_db',
            return_value=cursor
        ), patch(
            'backend.implementations.volumes.commit',
            side_effect=lambda: cursor.connection.commit()
        ), patch(
            'backend.implementations.volumes.determine_special_version',
            return_value='tpb'
        ):
            from backend.implementations.volumes import \
                refresh_special_versions
            refresh_special_versions(cv_to_id_fetch, volume_datas)

        row = cursor.execute(
            "SELECT special_version FROM volumes WHERE id=1"
        ).fetchonedict()
        self.assertEqual(row['special_version'], 'hard-cover')


if __name__ == '__main__':
    unittest.main()
