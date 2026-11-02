import sys
import unittest
from asyncio import run
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

if 'grp' not in sys.modules:
    sys.modules['grp'] = MagicMock()

from backend.implementations import comicvine
from backend.implementations.comicvine import ComicVine


def volume(volume_id: int, name: str, year: str, count: int) -> dict:
    return {
        'id': volume_id,
        'name': name,
        'start_year': year,
        'count_of_issues': count,
        'description': None,
        'deck': None,
        'aliases': None,
        'image': {'small_url': 'https://x/cover.jpg'},
        'publisher': None,
        'site_detail_url': f'https://comicvine.gamespot.com/v/4050-{volume_id}',
    }


class FetchVolumeMatchesOfIssues(unittest.TestCase):

    def setUp(self) -> None:
        settings = MagicMock()
        settings.get_settings.return_value = SimpleNamespace(
            date_type=SimpleNamespace(value='store_date'),
            comicvine_api_key='KEY', comicvine_api_url='http://cv/api'
        )
        for name, value in (
            ('Settings', settings), ('Session', MagicMock()),
            ('AsyncSession', MagicMock()),
        ):
            patcher = patch.object(comicvine, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.calls = []

        async def call_api(_self, session, url_path, params={}, default=None):
            self.calls.append((url_path, params))
            ids = [int(i) for i in params['filter'][3:].split('|')]
            if url_path == '/issues':
                return {'results': [
                    {'id': i, 'volume': {'id': 100 if i < 1000 else 200}}
                    for i in ids if i != 404
                ]}
            return {'results': [
                volume(v, 'Batman' if v == 100 else 'Robin', '2016', 150)
                for v in ids
            ]}

        patcher = patch.object(ComicVine, '_ComicVine__call_api', call_api)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_issues_map_to_their_volume_as_an_import_match(self):
        result = run(ComicVine().fetch_volume_matches_of_issues([1, 2, 1001]))

        self.assertEqual(result[1], {
            'id': 100, 'title': 'Batman (2016)', 'issue_count': 150,
            'link': 'https://comicvine.gamespot.com/v/4050-100',
        })
        self.assertEqual(result[2]['id'], 100)
        self.assertEqual(result[1001]['id'], 200)

    def test_unknown_issues_are_left_out(self):
        result = run(ComicVine().fetch_volume_matches_of_issues([1, 404]))
        self.assertEqual(set(result), {1})

    def test_requests_are_batched_by_one_hundred_without_covers(self):
        run(ComicVine().fetch_volume_matches_of_issues(range(1, 251)))

        issue_calls = [p for u, p in self.calls if u == '/issues']
        volume_calls = [p for u, p in self.calls if u == '/volumes']
        self.assertEqual(len(issue_calls), 3)
        self.assertEqual(issue_calls[0]['field_list'], 'id,volume')
        self.assertEqual(len(volume_calls), 1)
        self.assertEqual(volume_calls[0]['filter'], 'id:100')

    def test_no_issues_makes_no_requests(self):
        self.assertEqual(
            run(ComicVine().fetch_volume_matches_of_issues([])), {})
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
