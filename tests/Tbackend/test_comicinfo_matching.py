"""A rescan links a file by its ComicInfo.xml when its name matches no issue."""

import os
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

if 'grp' not in sys.modules:
    sys.modules['grp'] = MagicMock()

from backend.base.definitions import SpecialVersion
from backend.implementations import file_matching
from backend.internals.db import KapowarrCursor

SCHEMA = """
    CREATE TABLE files(
        id INTEGER PRIMARY KEY,
        filepath TEXT UNIQUE NOT NULL,
        size INTEGER
    );
    CREATE TABLE issues(id INTEGER PRIMARY KEY, volume_id INTEGER NOT NULL);
    CREATE TABLE issues_files(
        file_id INTEGER NOT NULL,
        issue_id INTEGER NOT NULL,
        forced BOOL NOT NULL DEFAULT 0
    );
    CREATE TABLE volume_files(
        file_id INTEGER PRIMARY KEY,
        volume_id INTEGER NOT NULL,
        file_type TEXT NOT NULL,
        forced BOOL NOT NULL DEFAULT 0
    );
    INSERT INTO issues(id, volume_id) VALUES (10, 1), (11, 1);
"""

# Issue 10 is #1 (ComicVine 5001), issue 11 is #1000 (ComicVine 5002)
ISSUES = [
    SimpleNamespace(id=10, comicvine_id=5001, calculated_issue_number=1.0,
                    date='2022-01-01'),
    SimpleNamespace(id=11, comicvine_id=5002, calculated_issue_number=1000.0,
                    date='2022-08-31'),
]


def comicinfo(issue_id: int) -> str:
    return (
        '<ComicInfo><Web>https://comicvine.gamespot.com/x/'
        f'4000-{issue_id}/</Web></ComicInfo>'
    )


class ScanFilesComicInfoTests(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        conn = sqlite3.connect(':memory:', check_same_thread=False)
        self.cursor = KapowarrCursor(conn)
        self.cursor.row_factory = sqlite3.Row
        self.cursor.executescript(SCHEMA)

        volume = MagicMock()
        volume.get_data.return_value = SimpleNamespace(
            folder=self.tmp.name, special_version=SpecialVersion.NORMAL,
            root_folder=1
        )
        volume.get_issues.return_value = list(ISSUES)
        volume.get_all_files.return_value = []
        volume.get_general_files.return_value = []

        settings = MagicMock()
        settings.get_settings.return_value = SimpleNamespace(
            create_empty_volume_folders=False, unmonitor_deleted_issues=False,
            delete_empty_folders=False
        )

        self.passes_filter = {}
        self.reader = MagicMock(wraps=file_matching.read_comicvine_issue_id)
        for target, value in (
            ('backend.implementations.volumes.Volume',
             MagicMock(return_value=volume)),
            ('backend.implementations.file_matching.get_db',
             MagicMock(return_value=self.cursor)),
            ('backend.internals.db_models.get_db',
             MagicMock(return_value=self.cursor)),
            ('backend.implementations.file_matching.Settings',
             MagicMock(return_value=settings)),
            ('backend.implementations.file_matching.present_external_file_ids',
             MagicMock(return_value=set())),
            ('backend.implementations.file_matching.refine_special_version',
             MagicMock(side_effect=lambda volume, data: data)),
            ('backend.implementations.file_matching.file_importing_filter',
             MagicMock(side_effect=lambda data, *_: self.passes_filter.get(
                 os.path.basename(data['series']), True))),
            ('backend.implementations.file_matching.read_comicvine_issue_id',
             self.reader),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_cbz(self, name: str, xml=None) -> str:
        path = os.path.join(self.tmp.name, name)
        with zipfile.ZipFile(path, 'w') as z:
            z.writestr('001.jpg', b'page')
            if xml is not None:
                z.writestr('ComicInfo.xml', xml)
        return path

    def bindings(self):
        return {
            (os.path.basename(r[0]), r[1])
            for r in self.cursor.execute(
                "SELECT f.filepath, if.issue_id FROM issues_files if "
                "JOIN files f ON f.id = if.file_id;"
            )
        }

    def scan(self) -> None:
        file_matching.scan_files(1, del_unmatched_files=False)

    def test_file_whose_number_matches_no_issue_is_linked_by_comicinfo(self):
        self.make_cbz('Amazing Fantasy 0005.cbz', comicinfo(5002))
        self.scan()
        self.assertEqual(self.bindings(), {('Amazing Fantasy 0005.cbz', 11)})

    def test_file_rejected_by_name_is_linked_by_comicinfo(self):
        self.passes_filter['Something Else'] = False
        self.make_cbz('Something Else 001.cbz', comicinfo(5001))
        self.scan()
        self.assertEqual(self.bindings(), {('Something Else 001.cbz', 10)})

    def test_file_without_a_number_is_linked_by_comicinfo(self):
        self.make_cbz('Amazing Fantasy.cbz', comicinfo(5002))
        self.scan()
        self.assertEqual(self.bindings(), {('Amazing Fantasy.cbz', 11)})

    def test_comicinfo_of_another_volumes_issue_links_nothing(self):
        self.make_cbz('Amazing Fantasy 0005.cbz', comicinfo(9999))
        self.scan()
        self.assertEqual(self.bindings(), set())

    def test_file_matched_by_name_is_not_opened(self):
        path = self.make_cbz('Amazing Fantasy 001.cbz', comicinfo(5002))
        self.scan()
        self.assertEqual(self.bindings(), {('Amazing Fantasy 001.cbz', 10)})
        self.assertNotIn(path, [c.args[0] for c in self.reader.call_args_list])


if __name__ == '__main__':
    unittest.main()
