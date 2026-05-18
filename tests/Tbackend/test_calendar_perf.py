"""
Tests that fetch_volumes_for_enrichment uses gather() for parallel batches.
"""
import asyncio
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

if 'grp' not in sys.modules:
    sys.modules['grp'] = MagicMock()

from backend.implementations.comicvine import ComicVine


def _make_cv() -> ComicVine:
    """Build a ComicVine instance without reading settings."""
    cv = ComicVine.__new__(ComicVine)
    cv.api_url = 'http://fake-cv/api'
    cv._params = {'format': 'json', 'api_key': 'testkey'}
    return cv


class TestFetchVolumesParallel(unittest.TestCase):
    """fetch_volumes_for_enrichment must dispatch all batches via gather()."""

    def _fake_call_api(self, fake_results):
        """Return an async side_effect that returns results for requested IDs."""
        async def side_effect(session, url, params, default=None):
            ids_raw = params['filter'].split('id:')[1]
            ids = {int(x) for x in ids_raw.split('|')}
            return {'results': [r for r in fake_results if int(r['id']) in ids]}
        return side_effect

    def test_gather_called_for_multiple_batches(self):
        """With 150 IDs (2 batches of 100/50), gather() must be called once."""
        cv = _make_cv()
        volume_ids = list(range(1, 151))
        fake_vols = [{'id': str(i), 'name': f'Vol{i}', 'publisher': {}}
                     for i in volume_ids]

        mock_session = AsyncMock()
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch('backend.implementations.comicvine.AsyncSession',
                   return_value=mock_ctx), \
             patch.object(type(cv), '_ComicVine__call_api',
                          side_effect=self._fake_call_api(fake_vols)), \
             patch('backend.implementations.comicvine.gather',
                   wraps=asyncio.gather) as mock_gather:
            result = asyncio.run(cv.fetch_volumes_for_enrichment(volume_ids))

        mock_gather.assert_called_once()
        self.assertEqual(len(result), 150)

    def test_empty_volume_ids_returns_empty(self):
        """Empty input should return [] without calling the API."""
        cv = _make_cv()
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch('backend.implementations.comicvine.AsyncSession',
                   return_value=mock_ctx), \
             patch('backend.implementations.comicvine.gather',
                   wraps=asyncio.gather) as mock_gather:
            result = asyncio.run(cv.fetch_volumes_for_enrichment([]))

        mock_gather.assert_not_called()
        self.assertEqual(result, [])

    def test_single_batch_returns_all_results(self):
        """50 IDs (one batch) → all 50 returned."""
        cv = _make_cv()
        volume_ids = list(range(1, 51))
        fake_vols = [{'id': str(i), 'name': f'Vol{i}', 'publisher': {}}
                     for i in volume_ids]

        mock_session = AsyncMock()
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch('backend.implementations.comicvine.AsyncSession',
                   return_value=mock_ctx), \
             patch.object(type(cv), '_ComicVine__call_api',
                          side_effect=self._fake_call_api(fake_vols)):
            result = asyncio.run(cv.fetch_volumes_for_enrichment(volume_ids))

        self.assertEqual(len(result), 50)


    def test_failed_batch_returns_partial_results(self):
        """A batch that returns empty default does not discard other batches."""
        cv = _make_cv()
        # 200 IDs → 2 batches; batch 1 (IDs 1–100) returns results,
        # batch 2 (IDs 101–200) returns empty (simulating default fallback)
        volume_ids = list(range(1, 201))
        batch1_vols = [{'id': str(i), 'name': f'Vol{i}', 'publisher': {}}
                       for i in range(1, 101)]

        call_count = 0

        async def side_effect(session, url, params, default=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {'results': batch1_vols}
            return {'results': []}  # second batch: empty (error fallback)

        mock_session = AsyncMock()
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch('backend.implementations.comicvine.AsyncSession',
                   return_value=mock_ctx), \
             patch.object(type(cv), '_ComicVine__call_api',
                          side_effect=side_effect):
            result = asyncio.run(cv.fetch_volumes_for_enrichment(volume_ids))

        # Only batch 1's results returned, no exception raised
        self.assertEqual(len(result), 100)


if __name__ == '__main__':
    unittest.main()
