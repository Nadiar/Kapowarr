# -*- coding: utf-8 -*-

"""
Tests for backend.implementations.local_comicvine module.

Uses an in-memory SQLite database that mirrors the local CV schema
so tests run without the real localcv.db file.
"""

import sqlite3
import unittest
from unittest.mock import MagicMock, patch

# We need to mock get_db before importing the module under test
# because search_volumes_local calls get_db for already_added check.


class _NoCloseConnection:
    """Wrapper around sqlite3.Connection that makes close() a no-op.
    The module under test calls conn.close() in its finally block,
    which would destroy our shared in-memory database.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def cursor(self):
        return self._conn.cursor()

    def close(self):
        pass  # no-op: keep the in-memory DB alive for the test

    @property
    def row_factory(self):
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._conn.row_factory = value


def _create_test_db() -> sqlite3.Connection:
    """Create an in-memory SQLite database with the local CV schema
    and sample data."""
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Create tables
    c.execute('''
        CREATE TABLE cv_publisher (
            id INTEGER PRIMARY KEY,
            name TEXT,
            image_url TEXT,
            site_detail_url TEXT
        )
    ''')

    c.execute('''
        CREATE TABLE cv_volume (
            id INTEGER PRIMARY KEY,
            name TEXT,
            aliases TEXT,
            start_year TEXT,
            publisher_id INTEGER,
            count_of_issues INTEGER,
            description TEXT,
            image_url TEXT,
            site_detail_url TEXT,
            deck TEXT
        )
    ''')

    c.execute('''
        CREATE TABLE cv_issue (
            id INTEGER PRIMARY KEY,
            volume_id INTEGER,
            name TEXT,
            issue_number TEXT,
            cover_date TEXT,
            store_date TEXT,
            description TEXT,
            image_url TEXT,
            site_detail_url TEXT
        )
    ''')

    # Create FTS5 virtual table
    c.execute('''
        CREATE VIRTUAL TABLE volume_fts USING fts5(
            name, aliases,
            content=cv_volume,
            content_rowid=id
        )
    ''')

    # Insert sample data
    c.execute('''
        INSERT INTO cv_publisher (id, name) VALUES
        (10, 'DC Comics'),
        (31, 'Marvel'),
        (1868, 'Image Comics')
    ''')

    c.execute('''
        INSERT INTO cv_volume (id, name, aliases, start_year, publisher_id,
                              count_of_issues, description, image_url,
                              site_detail_url, deck) VALUES
        (796, 'Batman', 'The Batman', '1940', 10, 750,
         'The Dark Knight', 'http://img/batman.jpg',
         'https://comicvine.gamespot.com/batman/4050-796/',
         'Volume 2 of the ongoing series'),
        (2135, 'Batman Album', NULL, '1965', 10, 120,
         'Australian Batman reprints', 'http://img/batalbum.jpg',
         'https://comicvine.gamespot.com/batman-album/4050-2135/',
         NULL),
        (42721, 'X-O Manowar', NULL, '1992', 1868, 68,
         'Valiant hero', 'http://img/xo.jpg',
         'https://comicvine.gamespot.com/x-o-manowar/4050-42721/',
         NULL),
        (18216, 'Bone', NULL, '1991', 1868, 55,
         'Jeff Smith epic', 'http://img/bone.jpg',
         'https://comicvine.gamespot.com/bone/4050-18216/',
         NULL),
        (99999, 'Übel Blatt', NULL, '2004', 31, 24,
         '<p>German publication.</p>', 'http://img/ubel.jpg',
         'https://comicvine.gamespot.com/ubel-blatt/4050-99999/',
         NULL)
    ''')

    # Populate FTS index
    c.execute('''
        INSERT INTO volume_fts (rowid, name, aliases)
        SELECT id, name, COALESCE(aliases, '') FROM cv_volume
    ''')

    c.execute('''
        INSERT INTO cv_issue (id, volume_id, name, issue_number) VALUES
        (100001, 796, 'Batman #1', '1'),
        (100002, 796, 'Batman #2', '2'),
        (100003, 796, 'Batman #3', '3'),
        (200001, 42721, 'X-O Manowar #1', '1'),
        (300001, 18216, 'Bone #1', '1')
    ''')

    conn.commit()
    return conn


class TestSearchVolumesLocal(unittest.TestCase):
    """Tests for search_volumes_local()."""

    def setUp(self):
        self.test_conn = _create_test_db()

        # Mock get_db to return a cursor with no already-added volumes
        self.mock_kapowarr_db = MagicMock()
        self.mock_kapowarr_db.execute.return_value = []

    def tearDown(self):
        self.test_conn.close()

    def _patch_and_search(self, query, limit=50):
        """Helper to call search_volumes_local with mocked DB connections."""
        from backend.implementations.local_comicvine import (
            search_volumes_local
        )

        wrapped = _NoCloseConnection(self.test_conn)
        with patch(
            'backend.implementations.local_comicvine._get_local_cv_conn',
            return_value=wrapped
        ), patch(
            'backend.implementations.local_comicvine.get_db',
            return_value=self.mock_kapowarr_db
        ):
            return search_volumes_local(query, limit)

    def test_search_batman_finds_results(self):
        results = self._patch_and_search('batman')
        self.assertIsNotNone(results)
        self.assertTrue(len(results) >= 2)
        titles = {r['title'] for r in results}
        self.assertIn('Batman', titles)
        self.assertIn('Batman Album', titles)

    def test_search_returns_volume_metadata_format(self):
        results = self._patch_and_search('bone')
        self.assertIsNotNone(results)
        self.assertEqual(len(results), 1)
        vol = results[0]
        self.assertEqual(vol['comicvine_id'], 18216)
        self.assertEqual(vol['title'], 'Bone')
        self.assertEqual(vol['year'], 1991)
        self.assertEqual(vol['volume_number'], 1)
        self.assertEqual(vol['issue_count'], 55)
        self.assertEqual(vol['publisher'], 'Image Comics')
        self.assertIn('bone', vol['site_url'])
        self.assertFalse(vol['translated'])
        self.assertIsNone(vol['already_added'])
        self.assertIsNone(vol['issues'])

    def test_search_with_hyphen_works(self):
        results = self._patch_and_search('x-o manowar')
        self.assertIsNotNone(results)
        self.assertTrue(len(results) >= 1)
        self.assertEqual(results[0]['comicvine_id'], 42721)

    def test_search_no_results_returns_empty_list(self):
        results = self._patch_and_search('nonexistentcomic12345')
        self.assertIsNotNone(results)
        self.assertEqual(len(results), 0)

    def test_search_with_limit(self):
        results = self._patch_and_search('batman', limit=1)
        self.assertIsNotNone(results)
        self.assertEqual(len(results), 1)

    def test_search_db_unavailable_returns_none(self):
        from backend.implementations.local_comicvine import (
            search_volumes_local
        )
        with patch(
            'backend.implementations.local_comicvine._get_local_cv_conn',
            return_value=None
        ):
            result = search_volumes_local('batman')
            self.assertIsNone(result)

    def test_search_publisher_resolved(self):
        results = self._patch_and_search('batman')
        self.assertIsNotNone(results)
        for r in results:
            self.assertEqual(r['publisher'], 'DC Comics')

    def test_search_aliases_populated(self):
        results = self._patch_and_search('batman')
        # Main Batman entry has alias "The Batman"
        batman = next(r for r in results if r['comicvine_id'] == 796)
        self.assertIn('The Batman', batman['aliases'])

    def test_search_already_added_marked(self):
        # Simulate volume 796 already added in Kapowarr
        self.mock_kapowarr_db.execute.return_value = [(796, 42)]
        results = self._patch_and_search('batman')
        batman = next(r for r in results if r['comicvine_id'] == 796)
        self.assertEqual(batman['already_added'], 42)

    def test_volume_number_from_deck(self):
        """Volume number should be extracted from deck field."""
        results = self._patch_and_search('batman')
        # Batman has deck "Volume 2 of the ongoing series"
        batman = next(r for r in results if r['comicvine_id'] == 796)
        self.assertEqual(batman['volume_number'], 2)
        # Batman Album has no deck, defaults to 1
        album = next(r for r in results if r['comicvine_id'] == 2135)
        self.assertEqual(album['volume_number'], 1)


class TestResolveIssueIdsLocal(unittest.TestCase):
    """Tests for resolve_issue_ids_local()."""

    def setUp(self):
        self.test_conn = _create_test_db()

    def tearDown(self):
        self.test_conn.close()

    def _patch_and_resolve(self, issue_ids):
        from backend.implementations.local_comicvine import (
            resolve_issue_ids_local
        )
        wrapped = _NoCloseConnection(self.test_conn)
        with patch(
            'backend.implementations.local_comicvine._get_local_cv_conn',
            return_value=wrapped
        ):
            return resolve_issue_ids_local(issue_ids)

    def test_resolve_single_issue(self):
        result = self._patch_and_resolve([100001])
        self.assertIsNotNone(result)
        self.assertEqual(result[100001], 796)

    def test_resolve_multiple_issues(self):
        result = self._patch_and_resolve([100001, 200001, 300001])
        self.assertIsNotNone(result)
        self.assertEqual(result[100001], 796)
        self.assertEqual(result[200001], 42721)
        self.assertEqual(result[300001], 18216)

    def test_resolve_missing_issue_omitted(self):
        result = self._patch_and_resolve([100001, 999999])
        self.assertIsNotNone(result)
        self.assertIn(100001, result)
        self.assertNotIn(999999, result)

    def test_resolve_empty_list(self):
        result = self._patch_and_resolve([])
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 0)

    def test_resolve_db_unavailable_returns_none(self):
        from backend.implementations.local_comicvine import (
            resolve_issue_ids_local
        )
        with patch(
            'backend.implementations.local_comicvine._get_local_cv_conn',
            return_value=None
        ):
            result = resolve_issue_ids_local([100001])
            self.assertIsNone(result)


class TestFetchVolumesLocal(unittest.TestCase):
    """Tests for fetch_volumes_local()."""

    def setUp(self):
        self.test_conn = _create_test_db()

    def tearDown(self):
        self.test_conn.close()

    def _patch_and_fetch(self, cv_ids):
        from backend.implementations.local_comicvine import (
            fetch_volumes_local
        )
        wrapped = _NoCloseConnection(self.test_conn)
        with patch(
            'backend.implementations.local_comicvine._get_local_cv_conn',
            return_value=wrapped
        ):
            return fetch_volumes_local(cv_ids)

    def test_fetch_single_volume(self):
        result = self._patch_and_fetch([796])
        self.assertIsNotNone(result)
        self.assertIn(796, result)
        vol = result[796]
        self.assertEqual(vol['title'], 'Batman')
        self.assertEqual(vol['year'], 1940)
        self.assertEqual(vol['publisher'], 'DC Comics')

    def test_fetch_multiple_volumes(self):
        result = self._patch_and_fetch([796, 42721, 18216])
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[796]['title'], 'Batman')
        self.assertEqual(result[42721]['title'], 'X-O Manowar')
        self.assertEqual(result[18216]['title'], 'Bone')

    def test_fetch_missing_volume_omitted(self):
        result = self._patch_and_fetch([796, 111111])
        self.assertIsNotNone(result)
        self.assertIn(796, result)
        self.assertNotIn(111111, result)

    def test_fetch_db_unavailable_returns_none(self):
        from backend.implementations.local_comicvine import (
            fetch_volumes_local
        )
        with patch(
            'backend.implementations.local_comicvine._get_local_cv_conn',
            return_value=None
        ):
            result = fetch_volumes_local([796])
            self.assertIsNone(result)


class TestLocalFilenamesToCvs(unittest.TestCase):
    """Tests for local_filenames_to_cvs()."""

    def setUp(self):
        self.test_conn = _create_test_db()
        self.mock_kapowarr_db = MagicMock()
        self.mock_kapowarr_db.execute.return_value = []

    def tearDown(self):
        self.test_conn.close()

    def _make_file_group(self, series, year=None, issue=1.0):
        """Create a minimal FilenameData group."""
        return {
            f'/comics/{series}/{series} #{int(issue)}.cbz': {
                'series': series,
                'year': year,
                'volume_number': 1,
                'issue_number': issue,
                'special_version': None,
                'annual': False
            }
        }

    def _patch_and_match(self, file_groups, only_english=True):
        from backend.implementations.local_comicvine import (
            local_filenames_to_cvs
        )
        wrapped = _NoCloseConnection(self.test_conn)
        with patch(
            'backend.implementations.local_comicvine._get_local_cv_conn',
            return_value=wrapped
        ), patch(
            'backend.implementations.local_comicvine.get_db',
            return_value=self.mock_kapowarr_db
        ):
            return local_filenames_to_cvs(file_groups, only_english)

    def test_match_single_group(self):
        groups = {1: self._make_file_group('Bone', 1991)}
        result = self._patch_and_match(groups)
        self.assertIn(1, result)
        self.assertEqual(result[1]['id'], 18216)
        self.assertIn('Bone', result[1]['title'])

    def test_unmatched_group_has_none_id(self):
        groups = {1: self._make_file_group('TotallyFakeComic99')}
        result = self._patch_and_match(groups)
        self.assertIn(1, result)
        self.assertIsNone(result[1]['id'])


class TestLocalResolveGroupsByIssueIds(unittest.TestCase):
    """Tests for local_resolve_groups_by_issue_ids()."""

    def setUp(self):
        self.test_conn = _create_test_db()

    def tearDown(self):
        self.test_conn.close()

    def _patch_and_resolve(self, groups_to_ids, only_english=True):
        from backend.implementations.local_comicvine import (
            local_resolve_groups_by_issue_ids
        )
        wrapped = _NoCloseConnection(self.test_conn)
        with patch(
            'backend.implementations.local_comicvine._get_local_cv_conn',
            return_value=wrapped
        ):
            return local_resolve_groups_by_issue_ids(
                groups_to_ids, only_english
            )

    def test_resolve_single_group(self):
        result = self._patch_and_resolve({1: 100001})
        self.assertIn(1, result)
        self.assertEqual(result[1]['id'], 796)
        self.assertIn('Batman', result[1]['title'])

    def test_resolve_multiple_groups(self):
        result = self._patch_and_resolve({1: 100001, 2: 200001})
        self.assertEqual(result[1]['id'], 796)
        self.assertEqual(result[2]['id'], 42721)

    def test_resolve_missing_issue_returns_no_match(self):
        result = self._patch_and_resolve({1: 999999})
        self.assertIn(1, result)
        self.assertIsNone(result[1]['id'])

    def test_resolve_db_unavailable_returns_no_matches(self):
        from backend.implementations.local_comicvine import (
            local_resolve_groups_by_issue_ids
        )
        with patch(
            'backend.implementations.local_comicvine._get_local_cv_conn',
            return_value=None
        ):
            result = local_resolve_groups_by_issue_ids({1: 100001}, True)
            self.assertIn(1, result)
            self.assertIsNone(result[1]['id'])


class TestIsLocalCvAvailable(unittest.TestCase):
    """Tests for is_local_cv_available()."""

    def test_available_when_file_exists(self):
        from backend.implementations.local_comicvine import (
            is_local_cv_available
        )
        with patch(
            'backend.implementations.local_comicvine.isfile',
            return_value=True
        ):
            self.assertTrue(is_local_cv_available())

    def test_unavailable_when_file_missing(self):
        from backend.implementations.local_comicvine import (
            is_local_cv_available
        )
        with patch(
            'backend.implementations.local_comicvine.isfile',
            return_value=False
        ):
            self.assertFalse(is_local_cv_available())


if __name__ == '__main__':
    unittest.main()
