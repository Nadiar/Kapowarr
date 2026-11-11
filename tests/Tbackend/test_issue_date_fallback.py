"""An issue without a store date gets its cover date, so older issues, which
ComicVine rarely has store dates for, still have a date."""

import sys
import unittest
from unittest.mock import MagicMock, patch

if 'grp' not in sys.modules:
    sys.modules['grp'] = MagicMock()


def make_comicvine(date_type: str):
    with patch(
        'backend.implementations.comicvine.Settings'
    ) as settings, patch('backend.implementations.comicvine.Session'):
        values = MagicMock()
        values.date_type.value = date_type
        values.comicvine_api_key = 'fake-key'
        values.comicvine_api_url = 'http://test/api'
        settings.return_value.get_settings.return_value = values
        from backend.implementations.comicvine import ComicVine
        return ComicVine()


def issue(store_date, cover_date) -> dict:
    return {
        'id': 1, 'volume': {'id': 2}, 'issue_number': '1', 'name': None,
        'description': '', 'store_date': store_date, 'cover_date': cover_date
    }


class TestIssueDateFallback(unittest.TestCase):
    def date_of(self, date_type: str, data: dict):
        comicvine = make_comicvine(date_type)
        return comicvine._ComicVine__format_issue_output(data)['date']

    def test_store_date_is_used_when_there_is_one(self):
        self.assertEqual(
            self.date_of('store_date', issue('1997-08-20', '1997-10-01')),
            '1997-08-20'
        )

    def test_cover_date_is_used_when_there_is_no_store_date(self):
        self.assertEqual(
            self.date_of('store_date', issue(None, '1997-10-01')),
            '1997-10-01'
        )

    def test_no_dates_at_all_is_none(self):
        self.assertIsNone(self.date_of('store_date', issue(None, None)))


if __name__ == '__main__':
    unittest.main()
