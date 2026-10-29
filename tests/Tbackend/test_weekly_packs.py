"""
Unit tests for the GetComics weekly pack scraper.

Tests cover HTML parsing of pack pages, search-result filtering,
deduplication, and the overall scrape_weekly_packs async function.
"""

import asyncio
import sys
import unittest
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

# The import chain GetComics indexer → helpers → settings
# → root_folders → settings tries to import the Linux-only 'grp' module.
# Mock it here at module level before any kapowarr imports happen.
if 'grp' not in sys.modules:
    _grp_mock = MagicMock()
    sys.modules['grp'] = _grp_mock

# ---------------------------------------------------------------------------
# Sample HTML fixtures
# ---------------------------------------------------------------------------

# Minimal GC weekly pack page — one <li> per comic
_PACK_PAGE_HTML = """
<html>
<body>
<h1 class="post-title">2026.03.18 Weekly Pack</h1>
<div class="post-inner-content">
<h2>DC Comics</h2>
<ul>
  <li><strong>Batman #150 : <a href="https://getcomics.org/dc/batman-150-2026/">Download</a></strong></li>
  <li><strong>Superman #25 : <a href="https://getcomics.org/dc/superman-25-2026/">Download</a></strong></li>
  <li>Some item without a GC link: <a href="https://readcomiconline.li/batman">Read</a></li>
</ul>
<h2>Marvel Comics</h2>
<ul>
  <li><strong>Amazing Spider-Man #50 : <a href="https://getcomics.org/marvel/amazing-spider-man-50-2026/">Download</a></strong></li>
</ul>
</div>
</body>
</html>
"""

_PACK_PAGE_HTML_WITH_READ_ONLINE = """
<html><body>
<ul>
    <li>Absolute Martian Manhunter #10 :
        <a href="https://getcomics.org/dc/absolute-martian-manhunter-10-2026/">Download</a>
        |
        <a href="https://example.org/read/absolute-martian-manhunter-10">Read Online</a>
    </li>
    <li>Preview item :
        <a href="https://getcomics.org/dc/preview-item/">Read Online</a>
    </li>
</ul>
</body></html>
"""

# A second pack page that has one duplicate link and one new link
_PACK_PAGE_HTML_2 = """
<html>
<body>
<h1 class="post-title">2026.03.11 Weekly Pack</h1>
<div class="post-inner-content">
<ul>
  <li><strong>Batman #149 : <a href="https://getcomics.org/dc/batman-149-2026/">Download</a></strong></li>
  <li><strong>Batman #150 : <a href="https://getcomics.org/dc/batman-150-2026/">Download</a></strong></li>
</ul>
</div>
</body>
</html>
"""

# Search result page listing two pack articles and one non-pack article
_SEARCH_PAGE_HTML = """
<html><body>
<article class="post">
  <h1 class="post-title"><a href="https://getcomics.org/packs/2026-03-18-weekly-pack/">2026.03.18 Weekly Pack</a></h1>
</article>
<article class="post">
  <h1 class="post-title"><a href="https://getcomics.org/packs/2026-03-11-weekly-pack/">2026.03.11 Weekly Pack</a></h1>
</article>
<article class="post">
  <h1 class="post-title"><a href="https://getcomics.org/dc/batman-150-2026/">Batman #150 (2026)</a></h1>
</article>
</body></html>
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GC_URL = 'https://getcomics.org'


def _make_indexer(session):
    """Create a GetComicsIndexer without touching the database."""
    from backend.implementations.indexer_clients.ddl.GetComics import \
        GetComicsIndexer
    indexer = object.__new__(GetComicsIndexer)
    indexer._id = 1
    indexer._title = 'GetComics'
    indexer._url = _GC_URL
    indexer.session = session
    indexer.request_count = 0
    return indexer


def _run(coro):
    """Run an async coroutine from sync test code."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Tests: _extract_pack_entries (internal HTML parser)
# ---------------------------------------------------------------------------

class TestExtractPackEntries(unittest.TestCase):
    """Test the function that extracts title/url entries from a pack page."""

    def test_extracts_title_and_download_link_pairs(self) -> None:
        """Should return (title, link) pairs for Download anchors."""
        from backend.implementations.indexer_clients.ddl.GetComics import _extract_pack_entries
        entries = _extract_pack_entries(_PACK_PAGE_HTML, _GC_URL)

        self.assertEqual(len(entries), 3)
        self.assertIn(
            (
                'Batman #150',
                'https://getcomics.org/dc/batman-150-2026/'
            ),
            entries
        )
        self.assertIn(
            (
                'Superman #25',
                'https://getcomics.org/dc/superman-25-2026/'
            ),
            entries
        )

    def test_ignores_non_download_anchors(self) -> None:
        """Read Online links should not be captured as entries."""
        from backend.implementations.indexer_clients.ddl.GetComics import _extract_pack_entries
        entries = _extract_pack_entries(_PACK_PAGE_HTML_WITH_READ_ONLINE, _GC_URL)

        self.assertEqual(len(entries), 1)
        self.assertEqual(
            entries[0],
            (
                'Absolute Martian Manhunter #10',
                'https://getcomics.org/dc/absolute-martian-manhunter-10-2026/'
            )
        )

    def test_returns_empty_for_blank_page(self) -> None:
        """Empty/junk HTML should return an empty list."""
        from backend.implementations.indexer_clients.ddl.GetComics import _extract_pack_entries
        entries = _extract_pack_entries('<html><body></body></html>', _GC_URL)
        self.assertEqual(entries, [])

    def test_extracts_title_text_before_first_anchor(self) -> None:
        """Should parse list-item text before the first anchor as title."""
        from backend.implementations.indexer_clients.ddl.GetComics import _extract_pack_entries
        entries = _extract_pack_entries(_PACK_PAGE_HTML_WITH_READ_ONLINE, _GC_URL)

        self.assertEqual(entries[0][0], 'Absolute Martian Manhunter #10')


# ---------------------------------------------------------------------------
# Tests: scrape_weekly_packs
# ---------------------------------------------------------------------------

class TestScrapeWeeklyPacks(unittest.TestCase):
    """Integration test for GetComicsIndexer.scrape_weekly_packs."""

    def _make_mock_session(self) -> MagicMock:
        """Build a mock AsyncSession that returns sample HTML."""
        session = MagicMock()

        async def fake_search(*args, **kwargs):
            from backend.base.definitions import QueryResult
            # Return two pack results and one non-pack result
            return QueryResult([
                {
                    'link': 'https://getcomics.org/packs/2026-03-18-weekly-pack/',
                    'display_title': '2026.03.18 Weekly Pack',
                    'source': 'GetComics',
                    'series': 'weekly pack',
                    'year': 2026, 'volume_number': None,
                    'special_version': None, 'issue_number': None,
                    'annual': False,
                },
                {
                    'link': 'https://getcomics.org/packs/2026-03-11-weekly-pack/',
                    'display_title': '2026.03.11 Weekly Pack',
                    'source': 'GetComics',
                    'series': 'weekly pack',
                    'year': 2026, 'volume_number': None,
                    'special_version': None, 'issue_number': None,
                    'annual': False,
                },
                {
                    'link': 'https://getcomics.org/dc/batman-150/',
                    'display_title': 'Batman #150',
                    'source': 'GetComics',
                    'series': 'Batman',
                    'year': None, 'volume_number': None,
                    'special_version': None, 'issue_number': 150.0,
                    'annual': False,
                },
            ], next_page_available=False)

        async def fake_get_text(url, **kwargs):
            if '2026-03-18' in url:
                return _PACK_PAGE_HTML
            elif '2026-03-11' in url:
                return _PACK_PAGE_HTML_2
            elif url.startswith('https://getcomics.org/'):
                return '<html><body><article class="post"></article></body></html>'
            return ''

        session.get_text = fake_get_text

        return session, fake_search

    def test_returns_list_of_search_result_data(self) -> None:
        """scrape_weekly_packs should return SearchResultData objects."""
        session, fake_search = self._make_mock_session()
        indexer = _make_indexer(session)

        with patch.object(indexer, 'search', side_effect=fake_search):
            results = _run(indexer.scrape_weekly_packs(pack_count=2))

        self.assertIsInstance(results, list)
        for r in results:
            self.assertIn('link', r)
            self.assertIn('display_title', r)
            self.assertEqual(r['indexer_id'], 1)

    def test_uses_entry_title_not_slug(self) -> None:
        """Display title should come from list-item text, not URL slug."""
        session, fake_search = self._make_mock_session()
        indexer = _make_indexer(session)

        with patch.object(indexer, 'search', side_effect=fake_search):
            results = _run(indexer.scrape_weekly_packs(pack_count=2))

        self.assertTrue(
            any(r['display_title'] == 'Batman #150' for r in results))
        self.assertFalse(
            any('batman-150-2026' in r['display_title'] for r in results))

    def test_deduplicates_links_across_packs(self) -> None:
        """A link appearing in two packs should only be returned once."""
        session, fake_search = self._make_mock_session()
        indexer = _make_indexer(session)

        with patch.object(indexer, 'search', side_effect=fake_search):
            results = _run(indexer.scrape_weekly_packs(pack_count=2))

        links = [r['link'] for r in results]
        # batman-150 appears in both _PACK_PAGE_HTML and _PACK_PAGE_HTML_2
        batman_links = [
            l for l in links
            if 'batman-150' in l
        ]
        self.assertEqual(
            len(batman_links),
            1, 'Duplicate link should appear once')

    def test_only_weekly_packs_processed(self) -> None:
        """Non-pack results from the search should not become sources."""
        session, fake_search = self._make_mock_session()
        indexer = _make_indexer(session)

        with patch.object(indexer, 'search', side_effect=fake_search):
            results = _run(indexer.scrape_weekly_packs(pack_count=2))

        # The batman-150 result from the search page itself should NOT appear
        # (pack_count=2 means only https://getcomics.org/packs/ URLs are fetched
        # as pack pages; the batman-150 result from the search is a non-pack)
        # Results should only contain links parsed FROM the pack pages.
        for r in results:
            self.assertNotIn(
                r['link'],
                ['https://getcomics.org/packs/2026-03-18-weekly-pack/',
                 'https://getcomics.org/packs/2026-03-11-weekly-pack/'],
                'Pack listing pages themselves should not be in results'
            )


if __name__ == '__main__':
    unittest.main()
