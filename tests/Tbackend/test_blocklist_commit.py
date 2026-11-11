"""Blocklisting a link commits straight away, so the database write lock
isn't held while the caller goes on to slow network requests."""

import sqlite3
import unittest
from unittest.mock import patch

from backend.base.definitions import BlocklistReason
from backend.implementations.blocklist import add_to_blocklist
from backend.internals.db import KapowarrCursor

SCHEMA = """
    CREATE TABLE blocklist(
        id INTEGER PRIMARY KEY,
        volume_id INTEGER,
        issue_id INTEGER,
        web_link TEXT,
        web_title TEXT,
        web_sub_title TEXT,
        download_link TEXT UNIQUE,
        download_service VARCHAR(30),
        reason INTEGER NOT NULL CHECK (reason > 0),
        added_at INTEGER NOT NULL CHECK (added_at > 0)
    );
"""


class TestAddToBlocklistCommits(unittest.TestCase):
    def test_blocklist_entry_is_committed_and_lock_released(self):
        connection = sqlite3.connect(':memory:')
        cursor = KapowarrCursor(connection)
        cursor.row_factory = sqlite3.Row
        cursor.executescript(SCHEMA)

        with patch(
            'backend.implementations.blocklist.get_db', return_value=cursor
        ), patch('backend.implementations.blocklist.get_blocklist_entry'):
            add_to_blocklist(
                web_link='https://gc/x',
                web_title='Title',
                web_sub_title=None,
                download_link='https://mega/x',
                download_service=None,
                volume_id=1,
                issue_id=None,
                reason=BlocklistReason.LINK_BROKEN
            )

        self.assertFalse(connection.in_transaction)
        self.assertEqual(
            cursor.execute('SELECT COUNT(*) FROM blocklist;').fetchone()[0],
            1
        )


if __name__ == '__main__':
    unittest.main()
