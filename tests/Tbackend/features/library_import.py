import unittest
from typing import Dict

from backend.base.definitions import FilenameData
from backend.base.file_extraction import extract_filename_data
from backend.features.library_import import (create_groups,
                                             match_groups_by_comicinfo)


class CreateGroupsTests(unittest.TestCase):
    def test_groups_ignore_year_for_same_volume(self):
        files: Dict[str, FilenameData] = {
            'Iron Man Volume 1 Issue 2': extract_filename_data(
                'Iron Man Volume 1 Issue 2'
            ),
            'Iron Man Volume 1 Issue 3': extract_filename_data(
                'Iron Man Volume 1 Issue 3'
            )
        }

        groups = create_groups(files)

        self.assertEqual(len(groups), 1)
        self.assertEqual(set(groups[1].keys()), set(files.keys()))

    def test_groups_detect_volume_as_issue_matches(self):
        files: Dict[str, FilenameData] = {
            'Iron Man Volume 1 Issue 2': extract_filename_data(
                'Iron Man Volume 1 Issue 2'
            ),
            'Iron Man Volume 1 Issue 3': extract_filename_data(
                'Iron Man Volume 1 Issue 3'
            ),
            'Iron Man Volume 2': extract_filename_data(
                'Iron Man Volume 2'
            ),
            'Iron Man Volume 3': extract_filename_data(
                'Iron Man Volume 3'
            )
        }

        groups = create_groups(files)

        self.assertEqual(len(groups), 2)
        self.assertEqual(set(groups[1].keys()), {
            "Iron Man Volume 1 Issue 2",
            "Iron Man Volume 1 Issue 3"
        })
        self.assertEqual(set(groups[2].keys()), {
            "Iron Man Volume 2",
            "Iron Man Volume 3"
        })

    def test_groups_require_sequential_years_and_issue_numbers(self):
        files: Dict[str, FilenameData] = {
            'Iron Man Volume 1 Issue 2 (2025)': extract_filename_data(
                'Iron Man Volume 1 Issue 2 (2025)'
            ),
            'Iron Man Volume 1 Issue 3 (2026)': extract_filename_data(
                'Iron Man Volume 1 Issue 3 (2026)'
            ),
            'Iron Man Volume 1 Issue 5 (2026)': extract_filename_data(
                'Iron Man Volume 1 Issue 5 (2026)'
            )
        }

        groups = create_groups(files)
        self.assertEqual(len(groups), 1)
        self.assertEqual(set(groups[1].keys()), set(files.keys()))


class MatchGroupsByComicInfoTests(unittest.TestCase):
    """Groups whose ComicInfo.xml issue IDs all point at one volume."""

    @staticmethod
    def Volume(volume_id, title, year, aliases=()):
        return {
            'comicvine_id': volume_id, 'title': title, 'year': year,
            'aliases': list(aliases), 'issue_count': 10,
            'site_url': f'https://comicvine.gamespot.com/v/4050-{volume_id}',
        }

    def setUp(self):
        self.volumes = {
            1: self.Volume(100, 'Batman', 2016),
            2: self.Volume(100, 'Batman', 2016),
            3: self.Volume(200, 'Robin', 1993),
            4: self.Volume(300, 'Ghost Rider', 1973),
            5: self.Volume(400, 'The Dark Knight Returns', 1986, ['DKR']),
        }

    def match(self, groups, issue_ids):
        self.lookups = []

        def lookup(ids):
            self.lookups.append(set(ids))
            return {i: self.volumes[i] for i in ids if i in self.volumes}

        return match_groups_by_comicinfo(groups, issue_ids.get, lookup)

    @staticmethod
    def Group(series, *files):
        return {f: {'series': series} for f in files}

    def test_group_whose_files_agree_gets_that_volume(self):
        result = self.match({1: self.Group('Batman', 'a', 'b')},
                            {'a': 1, 'b': 2})
        self.assertEqual(result, {1: {
            'id': 100, 'title': 'Batman (2016)', 'issue_count': 10,
            'link': 'https://comicvine.gamespot.com/v/4050-100',
        }})

    def test_files_without_an_id_follow_the_others(self):
        result = self.match({1: self.Group('Batman', 'a', 'b')}, {'a': 1})
        self.assertEqual(result[1]['id'], 100)

    def test_group_pointing_at_two_volumes_is_left_for_the_title_search(self):
        self.assertEqual(
            self.match({1: self.Group('Batman', 'a', 'b')}, {'a': 1, 'b': 3}),
            {}
        )

    def test_tag_of_a_differently_named_volume_is_not_trusted(self):
        # e.g. a file mis-tagged by an older tool
        self.assertEqual(
            self.match({1: self.Group('Amazing Spider-Man', 'a')}, {'a': 4}),
            {}
        )

    def test_title_match_ignores_punctuation_and_accepts_aliases(self):
        result = self.match(
            {1: self.Group('Batman', 'a'), 2: self.Group('DKR', 'b')},
            {'a': 1, 'b': 5}
        )
        self.assertEqual((result[1]['id'], result[2]['id']), (100, 400))

    def test_unknown_issue_ids_are_left_for_the_title_search(self):
        self.assertEqual(self.match({1: self.Group('Batman', 'a')},
                                    {'a': 999}), {})

    def test_groups_without_ids_skip_the_lookup(self):
        self.assertEqual(
            self.match({1: self.Group('Batman', 'a'),
                        2: self.Group('Robin', 'b')}, {}),
            {}
        )
        self.assertEqual(self.lookups, [])

    def test_all_ids_are_looked_up_in_one_call(self):
        self.match({1: self.Group('Batman', 'a'), 2: self.Group('Robin', 'b')},
                   {'a': 1, 'b': 3})
        self.assertEqual(self.lookups, [{1, 3}])


class ProposeLibraryImportTests(unittest.TestCase):
    """Tagged groups are matched by their issue, the rest by title."""

    TAGGED = {'comicvine_id': 100, 'title': 'Batman', 'year': 2016,
              'aliases': [], 'issue_count': 150,
              'site_url': 'https://comicvine.gamespot.com/batman/4050-100/'}
    BY_TITLE = {'id': 200, 'title': 'Robin (1993)', 'issue_count': 183,
                'link': 'https://comicvine.gamespot.com/robin/4050-200/'}

    def propose(self, tagged_files):
        import os
        from unittest.mock import MagicMock, patch

        from backend.features import library_import

        root = os.path.abspath('/comics')
        files = [
            os.path.join(root, 'Batman (2016)', 'Batman 001.cbz'),
            os.path.join(root, 'Robin (1993)', 'Robin 001.cbz'),
        ]
        self.title_searched = []

        async def filenames_to_cvs(groups, only_english):
            self.title_searched.extend(
                os.path.basename(f) for g in groups.values() for f in g
            )
            return {n: self.BY_TITLE for n in groups}

        async def fetch_volumes_of_issues(issue_ids):
            return {i: self.TAGGED for i in issue_ids}

        comicvine = MagicMock()
        comicvine.filenames_to_cvs = filenames_to_cvs
        comicvine.fetch_volumes_of_issues = fetch_volumes_of_issues
        root_folders = MagicMock()
        root_folders.return_value.get_folder_list.return_value = [root]

        with patch.object(library_import, 'RootFolders', root_folders), \
                patch.object(library_import, 'list_files',
                             return_value=files), \
                patch.object(library_import.FilesDB, 'fetch',
                             return_value=[]), \
                patch.object(library_import, 'ComicVine',
                             return_value=comicvine), \
                patch.object(library_import, 'read_comicvine_issue_id',
                             side_effect=lambda f: (
                                 7 if os.path.basename(f) in tagged_files
                                 else None
                             )):
            result = library_import.propose_library_import()

        return {os.path.basename(r['filepath']): r['cv']['id'] for r in result}

    def test_tagged_group_skips_the_title_search(self):
        self.assertEqual(
            self.propose({'Batman 001.cbz'}),
            {'Batman 001.cbz': 100, 'Robin 001.cbz': 200}
        )
        self.assertEqual(self.title_searched, ['Robin 001.cbz'])

    def test_untagged_groups_are_all_searched_by_title(self):
        self.assertEqual(
            self.propose(set()),
            {'Batman 001.cbz': 200, 'Robin 001.cbz': 200}
        )
        self.assertEqual(
            sorted(self.title_searched), ['Batman 001.cbz', 'Robin 001.cbz']
        )
