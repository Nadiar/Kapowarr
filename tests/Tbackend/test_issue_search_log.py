"""Per-issue search log and the order in which SearchAll visits volumes."""

import sqlite3
import unittest
from unittest.mock import patch

from backend.internals.db import KapowarrCursor
from backend.internals.db_models_search_stats import (IssueSearchOutcome,
                                                      get_search_all_volumes,
                                                      record_issue_searches)
from backend.internals.fork_schema import FORK_TABLES

_BASE_SCHEMA = """
    CREATE TABLE volumes(
        id INTEGER PRIMARY KEY,
        title TEXT NOT NULL DEFAULT '',
        monitored BOOL NOT NULL DEFAULT 1
    );
    CREATE TABLE issues(
        id INTEGER PRIMARY KEY,
        volume_id INTEGER NOT NULL,
        monitored BOOL NOT NULL DEFAULT 1,
        date VARCHAR(10)
    );
    CREATE TABLE issues_files(
        file_id INTEGER NOT NULL,
        issue_id INTEGER NOT NULL
    );
    CREATE TABLE files(id INTEGER PRIMARY KEY);
"""


def make_cursor() -> KapowarrCursor:
    connection = sqlite3.connect(':memory:')
    connection.row_factory = sqlite3.Row
    cursor = KapowarrCursor(connection)
    cursor.row_factory = sqlite3.Row
    cursor.executescript(_BASE_SCHEMA + FORK_TABLES)
    return cursor


class TestRecordIssueSearches(unittest.TestCase):
    def setUp(self):
        self.cursor = make_cursor()
        patcher = patch(
            'backend.internals.db_models_search_stats.get_db',
            return_value=self.cursor
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def log_row(self, issue_id: int) -> dict:
        return dict(self.cursor.execute(
            "SELECT * FROM issue_search_log WHERE issue_id = ?;",
            (issue_id,)
        ).fetchone())

    def test_not_found_counts_consecutive_misses(self):
        record_issue_searches({1: IssueSearchOutcome.NOT_FOUND})
        record_issue_searches({1: IssueSearchOutcome.NOT_FOUND})

        row = self.log_row(1)
        self.assertEqual(row['last_outcome'], 'not_found')
        self.assertEqual(row['consecutive_misses'], 2)
        self.assertEqual(row['times_searched'], 2)
        self.assertGreater(row['last_searched'], 0)

    def test_queued_resets_misses_and_notes_when(self):
        record_issue_searches({1: IssueSearchOutcome.NOT_FOUND})
        record_issue_searches({1: IssueSearchOutcome.QUEUED})

        row = self.log_row(1)
        self.assertEqual(row['last_outcome'], 'queued')
        self.assertEqual(row['consecutive_misses'], 0)
        self.assertGreater(row['last_queued'], 0)

    def test_error_is_logged_without_counting_a_miss(self):
        record_issue_searches({1: IssueSearchOutcome.NOT_FOUND})
        record_issue_searches({1: IssueSearchOutcome.ERROR})

        row = self.log_row(1)
        self.assertEqual(row['last_outcome'], 'error')
        self.assertEqual(row['consecutive_misses'], 1)
        self.assertEqual(row['times_searched'], 2)
        self.assertEqual(row['last_queued'], 0)


class TestSearchAllOrder(unittest.TestCase):
    def setUp(self):
        self.cursor = make_cursor()
        patcher = patch(
            'backend.internals.db_models_search_stats.get_db',
            return_value=self.cursor
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def add_volume(
        self,
        volume_id: int,
        open_dates: list,
        monitored: bool = True,
        misses: int = 0
    ) -> None:
        self.cursor.execute(
            "INSERT INTO volumes(id, title, monitored) VALUES (?, ?, ?);",
            (volume_id, f'Volume {volume_id}', monitored)
        )
        for issue_date in open_dates:
            self.cursor.execute(
                "INSERT INTO issues(volume_id, date) VALUES (?, ?);",
                (volume_id, issue_date)
            )
        self.cursor.execute(
            "INSERT INTO volume_search_stats(volume_id, consecutive_misses) "
            "VALUES (?, ?);",
            (volume_id, misses)
        )

    def test_newest_released_missing_issue_comes_first(self):
        self.add_volume(1, ['2020-01-01'])
        self.add_volume(2, ['2020-01-01', '2026-09-01'])
        self.add_volume(3, ['2099-01-01'])
        self.add_volume(4, ['2026-09-01'], misses=5)
        self.add_volume(5, [None])
        self.add_volume(6, ['2026-10-01'], monitored=False)

        order = [v[0] for v in get_search_all_volumes()]

        self.assertEqual(order, [2, 4, 1, 5, 3])

    def test_volume_without_missing_issues_is_skipped(self):
        self.add_volume(1, ['2026-01-01'])
        issue_id = self.cursor.execute(
            "SELECT id FROM issues WHERE volume_id = 1;"
        ).fetchone()[0]
        self.cursor.execute(
            "INSERT INTO issues_files(file_id, issue_id) VALUES (1, ?);",
            (issue_id,)
        )

        self.assertEqual(get_search_all_volumes(), [])


if __name__ == '__main__':
    unittest.main()
