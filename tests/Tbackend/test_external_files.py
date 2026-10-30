"""Tests for files owned by an external manager (`external_files.py`)."""

import sqlite3
import sys
import tempfile
import unittest
from os.path import join
from unittest.mock import MagicMock, patch

if 'grp' not in sys.modules:
    sys.modules['grp'] = MagicMock()

from backend.base.custom_exceptions import FileNotFound, InvalidKeyValue
from backend.features import external_files
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
    CREATE TABLE external_files(
        file_id INTEGER PRIMARY KEY,
        FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
    );
"""


def _make_cursor() -> KapowarrCursor:
    conn = sqlite3.connect(':memory:', check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON;')
    cursor = KapowarrCursor(conn)
    cursor.row_factory = sqlite3.Row
    cursor.executescript(SCHEMA)
    return cursor


class ExternalFilesTestCase(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cursor = _make_cursor()
        patcher = patch.object(
            external_files, 'get_db', return_value=self.cursor
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_file(self, name: str, content: bytes = b'cbz-bytes') -> str:
        path = join(self.tmp.name, name)
        with open(path, 'wb') as f:
            f.write(content)
        return path

    def add_file_row(
        self, filepath: str, volume_id: int = 1, issue_id: int = 10
    ) -> int:
        self.cursor.execute(
            "INSERT INTO files(filepath, size) VALUES (?, 1);", (filepath,)
        )
        file_id = self.cursor.lastrowid
        self.cursor.execute(
            "INSERT OR IGNORE INTO issues(id, volume_id) VALUES (?, ?);",
            (issue_id, volume_id)
        )
        self.cursor.execute(
            "INSERT INTO issues_files(file_id, issue_id) VALUES (?, ?);",
            (file_id, issue_id)
        )
        return file_id

    def row(self, file_id: int):
        return self.cursor.execute(
            "SELECT filepath, size FROM files WHERE id = ?;", (file_id,)
        ).fetchone()


class TestRepointFile(ExternalFilesTestCase):

    def test_updates_path_and_size_and_marks_external(self) -> None:
        file_id = self.add_file_row('/kapowarr/vol/Batman 001.cbz')
        new_path = self.make_file('Batman 001 (2020).cbz', b'x' * 42)

        external_files.repoint_file(file_id, new_path)

        row = self.row(file_id)
        self.assertEqual(row['filepath'], new_path)
        self.assertEqual(row['size'], 42)
        self.assertTrue(external_files.is_external(file_id))

    def test_keeps_issue_binding(self) -> None:
        file_id = self.add_file_row('/kapowarr/vol/Batman 001.cbz')
        external_files.repoint_file(file_id, self.make_file('b.cbz'))

        bound = self.cursor.execute(
            "SELECT issue_id FROM issues_files WHERE file_id = ?;", (file_id,)
        ).fetchall()
        self.assertEqual([r['issue_id'] for r in bound], [10])

    def test_repointing_again_follows_a_second_move(self) -> None:
        file_id = self.add_file_row('/kapowarr/vol/Batman 001.cbz')
        external_files.repoint_file(file_id, self.make_file('first.cbz'))
        second = self.make_file('second.cbz')

        external_files.repoint_file(file_id, second)

        self.assertEqual(self.row(file_id)['filepath'], second)
        self.assertEqual(
            self.cursor.execute(
                "SELECT COUNT(*) FROM external_files;"
            ).fetchone()[0],
            1
        )

    def test_unknown_file_id_raises_file_not_found(self) -> None:
        with self.assertRaises(FileNotFound):
            external_files.repoint_file(999, self.make_file('a.cbz'))

    def test_missing_target_raises_and_changes_nothing(self) -> None:
        file_id = self.add_file_row('/kapowarr/vol/Batman 001.cbz')

        with self.assertRaises(FileNotFound):
            external_files.repoint_file(
                file_id, join(self.tmp.name, 'does-not-exist.cbz')
            )

        self.assertEqual(
            self.row(file_id)['filepath'], '/kapowarr/vol/Batman 001.cbz'
        )
        self.assertFalse(external_files.is_external(file_id))

    def test_relative_path_rejected(self) -> None:
        file_id = self.add_file_row('/kapowarr/vol/Batman 001.cbz')
        with self.assertRaises(InvalidKeyValue):
            external_files.repoint_file(file_id, 'relative/path.cbz')

    def test_path_used_by_another_file_rejected(self) -> None:
        taken = self.make_file('taken.cbz')
        self.add_file_row(taken, issue_id=11)
        file_id = self.add_file_row('/kapowarr/vol/Batman 001.cbz')

        with self.assertRaises(InvalidKeyValue):
            external_files.repoint_file(file_id, taken)


class TestQueries(ExternalFilesTestCase):

    def test_is_external_false_for_ordinary_file(self) -> None:
        file_id = self.add_file_row('/kapowarr/vol/a.cbz')
        self.assertFalse(external_files.is_external(file_id))

    def test_skip_external_drops_only_external_files(self) -> None:
        ordinary = self.add_file_row('/kapowarr/vol/a.cbz', issue_id=10)
        external = self.add_file_row('/kapowarr/vol/b.cbz', issue_id=11)
        external_files.repoint_file(external, self.make_file('b.cbz'))

        files = [
            {'id': ordinary, 'filepath': '/kapowarr/vol/a.cbz', 'size': 1},
            {'id': external, 'filepath': 'x', 'size': 1},
        ]
        self.assertEqual(
            [f['id'] for f in external_files.skip_external(files)],
            [ordinary]
        )

    def test_skip_external_handles_empty_list(self) -> None:
        self.assertEqual(external_files.skip_external([]), [])

    def test_present_ids_only_for_volume_and_existing_on_disk(self) -> None:
        here = self.add_file_row('/k/a.cbz', volume_id=1, issue_id=10)
        other_volume = self.add_file_row('/k/b.cbz', volume_id=2, issue_id=20)
        gone = self.add_file_row('/k/c.cbz', volume_id=1, issue_id=11)
        for file_id, name in ((here, 'a'), (other_volume, 'b'), (gone, 'c')):
            external_files.repoint_file(
                file_id, self.make_file(f'{name}.cbz')
            )
        self.cursor.execute(
            "UPDATE files SET filepath = ? WHERE id = ?;",
            (join(self.tmp.name, 'vanished.cbz'), gone)
        )

        self.assertEqual(
            external_files.present_external_file_ids(volume_id=1), {here}
        )


class TestForget(ExternalFilesTestCase):

    def test_forget_removes_the_external_mark_only(self) -> None:
        file_id = self.add_file_row('/k/a.cbz')
        new_path = self.make_file('a.cbz')
        external_files.repoint_file(file_id, new_path)

        external_files.forget_external(file_id)

        self.assertFalse(external_files.is_external(file_id))
        self.assertEqual(self.row(file_id)['filepath'], new_path)


if __name__ == '__main__':
    unittest.main()
