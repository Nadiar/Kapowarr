"""
Unit tests for ComicVine.fetch_issues_since().

Verifies:
- date_last_updated filter is correctly constructed
- Returns IssueMetadata dicts
- Timestamp converted to CV date format
"""

import sys
import unittest
from asyncio import run as async_run
from unittest.mock import MagicMock, patch

# Avoid grp import failure on Windows
if 'grp' not in sys.modules:
    sys.modules['grp'] = MagicMock()


class FakeAsyncSession:
    """Minimal async context manager replacing AsyncSession."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class TestFetchIssuesSince(unittest.TestCase):
    """Test ComicVine.fetch_issues_since() method."""

    def _make_cv(self):
        """Create a ComicVine instance with mocked settings."""
        with patch(
            'backend.implementations.comicvine.Settings'
        ) as mock_settings, patch(
            'backend.implementations.comicvine.Session'
        ):
            s = MagicMock()
            s.date_type.value = 'store_date'
            s.comicvine_api_key = 'fake-key'
            s.comicvine_api_url = 'http://test/api'
            mock_settings.return_value.get_settings.return_value = s
            from backend.implementations.comicvine import ComicVine
            return ComicVine()

    def test_filter_string_includes_date_last_updated(self):
        """Verify the API call includes date_last_updated filter."""
        cv = self._make_cv()

        captured_params = {}

        async def fake_call_api(session, path, params, *a):
            captured_params.update(params)
            return {
                'results': [],
                'number_of_total_results': 0
            }

        with patch.object(
            cv, '_ComicVine__call_api', side_effect=fake_call_api
        ), patch(
            'backend.implementations.comicvine.AsyncSession',
            return_value=FakeAsyncSession()
        ):
            async_run(cv.fetch_issues_since((12345,), 1711234567))

        self.assertIn('filter', captured_params)
        filt = captured_params['filter']
        self.assertIn('volume:', filt)
        self.assertIn('12345', filt)
        self.assertIn('date_last_updated:', filt)

    def test_returns_issue_metadata_dicts(self):
        """Verify results are formatted via __format_issue_output."""
        cv = self._make_cv()

        fake_api_result = {
            'results': [{
                'id': 999,
                'issue_number': '1',
                'name': 'Test Issue',
                'cover_date': '2025-01-01',
                'store_date': '2025-01-01',
                'description': '<p>Desc</p>',
                'volume': {'id': 12345},
                'date_last_updated': '2025-01-15 10:00:00',
            }],
            'number_of_total_results': 1
        }

        async def fake_call_api(session, path, params, *a):
            return fake_api_result

        with patch.object(
            cv, '_ComicVine__call_api', side_effect=fake_call_api
        ), patch(
            'backend.implementations.comicvine.AsyncSession',
            return_value=FakeAsyncSession()
        ):
            result = async_run(cv.fetch_issues_since((12345,), 0))

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['comicvine_id'], 999)
        self.assertEqual(result[0]['volume_id'], 12345)
        self.assertEqual(result[0]['issue_number'], '1')

    def test_timestamp_to_date_conversion(self):
        """Verify the since_timestamp is converted to CV format."""
        cv = self._make_cv()

        captured_params = {}

        async def fake_call_api(session, path, params, *a):
            captured_params.update(params)
            return {
                'results': [],
                'number_of_total_results': 0
            }

        # 2025-03-24 00:00:00 UTC = 1742774400
        with patch.object(
            cv, '_ComicVine__call_api', side_effect=fake_call_api
        ), patch(
            'backend.implementations.comicvine.AsyncSession',
            return_value=FakeAsyncSession()
        ):
            async_run(cv.fetch_issues_since((1,), 1742774400))

        filt = captured_params['filter']
        self.assertIn('2025-03-24', filt)


if __name__ == '__main__':
    unittest.main()
