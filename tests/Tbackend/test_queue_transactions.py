"""Adding downloads to the queue: a download with a problem is dropped on its
own, one that only hit a locked database is retried, and a failure never
leaves half-written rows or an open transaction behind."""

import sqlite3
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

if 'grp' not in sys.modules:
    sys.modules['grp'] = MagicMock()

from backend.base.custom_exceptions import EnqueuingDownloadFailure
from backend.base.definitions import EnqueuingDownloadFailureReason
from backend.features.download_queue import DownloadHandler
from backend.internals.db import KapowarrCursor

VOLUME_ID = 7


def entry(link: str) -> tuple:
    return (link, 1, {}, VOLUME_ID, None, False)


def locked() -> sqlite3.OperationalError:
    return sqlite3.OperationalError('database is locked')


def make_cursor(schema: str) -> KapowarrCursor:
    connection = sqlite3.connect(':memory:')
    connection.execute('PRAGMA foreign_keys = ON;')
    cursor = KapowarrCursor(connection)
    cursor.row_factory = sqlite3.Row
    cursor.executescript(schema)
    return cursor


class TestAddMultiple(unittest.TestCase):
    def setUp(self):
        patch('backend.features.download_queue.sleep').start()
        patch('backend.features.download_queue.commit').start()
        self.get_db = patch('backend.features.download_queue.get_db').start()
        self.addCleanup(patch.stopall)
        self.handler = MagicMock()

    def add_multiple(self, entries: list, outcomes: dict) -> list:
        """Run add_multiple, where `outcomes` maps a link to the list of
        exceptions (or None for success) its attempts end with."""
        def add(link, *args):
            result = outcomes[link].pop(0)
            if result is not None:
                raise result
            return []

        self.handler.add.side_effect = add
        return DownloadHandler.add_multiple(self.handler, entries)

    def test_problem_download_is_dropped_and_locked_one_retried(self):
        broken = EnqueuingDownloadFailure(
            EnqueuingDownloadFailureReason.NO_WORKING_LINKS
        )
        added = self.add_multiple(
            [entry('locked'), entry('broken'),
             entry('error'), entry('fine')],
            {
                'locked': [locked(), None],
                'broken': [broken],
                'error': [RuntimeError('unexpected')],
                'fine': [None]
            }
        )

        self.assertEqual(added, [entry('fine'), entry('locked')])
        self.assertEqual(
            self.handler.add.call_args_list,
            [call(*entry('locked')), call(*entry('broken')),
             call(*entry('error')), call(*entry('fine')),
             call(*entry('locked'))]
        )

    def test_failed_download_is_rolled_back(self):
        self.add_multiple(
            [entry('error')], {'error': [RuntimeError('unexpected')]}
        )

        self.get_db.return_value.connection.rollback.assert_called()

    def test_download_that_stays_locked_is_given_up(self):
        added = self.add_multiple(
            [entry('locked'), entry('fine')],
            {
                'locked': [locked(), locked(), locked()],
                'fine': [None]
            }
        )

        self.assertEqual(added, [entry('fine')])
        self.assertEqual(self.handler.add.call_count, 4)


QUEUE_SCHEMA = """
    CREATE TABLE download_queue(
        id INTEGER PRIMARY KEY,
        volume_id INTEGER NOT NULL,
        client_type VARCHAR(255) NOT NULL,
        external_client_id INTEGER,
        download_link TEXT NOT NULL,
        covered_issues VARCHAR(255),
        force_original_name BOOL,
        source_type VARCHAR(25) NOT NULL,
        source_name VARCHAR(255) NOT NULL,
        web_link TEXT,
        web_title TEXT,
        web_sub_title TEXT
    );
"""


def fake_download(volume_id):
    return SimpleNamespace(
        id=None,
        volume_id=volume_id,
        identifier=SimpleNamespace(value='direct'),
        download_link='https://dl/x',
        covered_issues=1.0,
        download_service=SimpleNamespace(value='mega'),
        source_name='GetComics',
        web_link='https://gc/x',
        web_title='Title',
        web_sub_title=None,
        download_thread=None
    )


class TestQueueRowsAreAtomic(unittest.TestCase):
    def setUp(self):
        self.cursor = make_cursor(QUEUE_SCHEMA)
        patch(
            'backend.features.download_queue.get_db',
            return_value=self.cursor
        ).start()
        self.server = patch('backend.features.download_queue.Server').start()
        self.websocket = patch(
            'backend.features.download_queue.WebSocket').start()
        self.addCleanup(patch.stopall)

        self.handler = DownloadHandler.__new__(DownloadHandler)
        self.prepare = getattr(
            self.handler, '_DownloadHandler__prepare_downloads_for_queue'
        )

    def test_failing_row_leaves_no_rows_threads_or_events(self):
        downloads = [fake_download(VOLUME_ID), fake_download(None)]

        with self.assertRaises(sqlite3.IntegrityError):
            self.prepare(downloads, False)

        self.assertEqual(
            self.cursor.execute(
                'SELECT COUNT(*) FROM download_queue;').fetchone()[0],
            0
        )
        self.assertFalse(self.cursor.connection.in_transaction)
        self.server.return_value.get_db_thread.assert_not_called()
        self.websocket.return_value.emit.assert_not_called()

    def test_rows_are_committed_before_the_download_starts(self):
        def check_committed(*args, **kwargs):
            self.assertFalse(self.cursor.connection.in_transaction)
            return MagicMock()

        self.server.return_value.get_db_thread.side_effect = check_committed

        downloads = self.prepare([fake_download(VOLUME_ID)], False)

        self.assertIsNotNone(downloads[0].id)
        self.server.return_value.get_db_thread.assert_called_once()


class TestTransactionCommitFailure(unittest.TestCase):
    def test_failed_commit_rolls_back_and_releases_the_transaction(self):
        cursor = make_cursor("""
            CREATE TABLE parents(id INTEGER PRIMARY KEY);
            CREATE TABLE children(
                parent_id INTEGER REFERENCES parents(id)
                    DEFERRABLE INITIALLY DEFERRED
            );
        """)

        with self.assertRaises(sqlite3.IntegrityError):
            with cursor:
                cursor.execute('INSERT INTO children VALUES (1);')

        self.assertFalse(cursor.connection.in_transaction)
        self.assertEqual(cursor.connection.isolation_level, 'DEFERRED')
        self.assertEqual(
            cursor.execute('SELECT COUNT(*) FROM children;').fetchone()[0],
            0
        )


if __name__ == '__main__':
    unittest.main()
