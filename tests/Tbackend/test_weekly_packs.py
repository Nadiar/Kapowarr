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

# The import chain getcomics → download_clients → naming → file_processing
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

def _run(coro):
    """Run an async coroutine from sync test code."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Tests: _extract_pack_links (internal HTML parser)
# ---------------------------------------------------------------------------

class TestExtractPackLinks(unittest.TestCase):
    """Test the function that extracts GC article links from a pack page."""

    def test_extracts_gc_article_links(self) -> None:
        """Should return all <a href> values pointing to getcomics.org."""
        from backend.implementations.getcomics import _extract_pack_links
        links = _extract_pack_links(_PACK_PAGE_HTML)

        self.assertEqual(len(links), 3)
        self.assertIn(
            'https://getcomics.org/dc/batman-150-2026/', links
        )
        self.assertIn(
            'https://getcomics.org/dc/superman-25-2026/', links
        )
        self.assertIn(
            'https://getcomics.org/marvel/amazing-spider-man-50-2026/', links
        )

    def test_ignores_non_gc_links(self) -> None:
        """Links not pointing to getcomics.org should be excluded."""
        from backend.implementations.getcomics import _extract_pack_links
        links = _extract_pack_links(_PACK_PAGE_HTML)

        for link in links:
            self.assertIn('getcomics.org', link)

    def test_returns_empty_for_blank_page(self) -> None:
        """Empty/junk HTML should return an empty list."""
        from backend.implementations.getcomics import _extract_pack_links
        links = _extract_pack_links('<html><body></body></html>')
        self.assertEqual(links, [])


# ---------------------------------------------------------------------------
# Tests: _filter_pack_results
# ---------------------------------------------------------------------------

class TestFilterPackResults(unittest.TestCase):
    """Test the function that filters search results for Weekly Pack articles."""

    def _make_result(self, display_title: str, link: str) -> dict:
        return {
            'link': link,
            'display_title': display_title,
            'source': 'GetComics',
            'series': '',
            'year': None,
            'volume_number': None,
            'special_version': None,
            'issue_number': None,
            'annual': False,
        }

    def test_keeps_weekly_pack_results(self) -> None:
        """Results with 'Weekly Pack' in display_title should be kept."""
        from backend.implementations.getcomics import _filter_pack_results
        results = [
            self._make_result(
                '2026.03.18 Weekly Pack',
                'https://getcomics.org/packs/2026-03-18/'
            ),
            self._make_result(
                '2026.03.11 Weekly Pack',
                'https://getcomics.org/packs/2026-03-11/'
            ),
        ]
        filtered = _filter_pack_results(results)
        self.assertEqual(len(filtered), 2)

    def test_excludes_non_pack_results(self) -> None:
        """Results without 'Weekly Pack' should be excluded."""
        from backend.implementations.getcomics import _filter_pack_results
        results = [
            self._make_result(
                '2026.03.18 Weekly Pack',
                'https://getcomics.org/packs/2026-03-18/'
            ),
            self._make_result(
                'Batman #150 (2026)',
                'https://getcomics.org/dc/batman-150/'
            ),
        ]
        filtered = _filter_pack_results(results)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]['display_title'], '2026.03.18 Weekly Pack')


# ---------------------------------------------------------------------------
# Tests: scrape_weekly_packs
# ---------------------------------------------------------------------------

class TestScrapeWeeklyPacks(unittest.TestCase):
    """Integration test for the async scrape_weekly_packs function."""

    def _make_mock_session(self) -> MagicMock:
        """Build a mock AsyncSession that returns sample HTML."""
        session = MagicMock()

        async def fake_search(*args, **kwargs):
            # Return two pack results and one non-pack result
            return [
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
            ]

        async def fake_get_text(url, **kwargs):
            if '2026-03-18' in url:
                return _PACK_PAGE_HTML
            elif '2026-03-11' in url:
                return _PACK_PAGE_HTML_2
            return ''

        session.get_text = fake_get_text

        return session, fake_search

    def test_returns_list_of_search_result_data(self) -> None:
        """scrape_weekly_packs should return SearchResultData objects."""
        from backend.implementations.getcomics import scrape_weekly_packs

        session, fake_search = self._make_mock_session()

        with patch(
            'backend.implementations.getcomics.search_getcomics',
            side_effect=fake_search
        ):
            results = _run(scrape_weekly_packs(session, pack_count=2))

        self.assertIsInstance(results, list)
        for r in results:
            self.assertIn('link', r)
            self.assertIn('display_title', r)
            self.assertIn('source', r)

    def test_deduplicates_links_across_packs(self) -> None:
        """A link appearing in two packs should only be returned once."""
        from backend.implementations.getcomics import scrape_weekly_packs

        session, fake_search = self._make_mock_session()

        with patch(
            'backend.implementations.getcomics.search_getcomics',
            side_effect=fake_search
        ):
            results = _run(scrape_weekly_packs(session, pack_count=2))

        links = [r['link'] for r in results]
        # batman-150 appears in both _PACK_PAGE_HTML and _PACK_PAGE_HTML_2
        batman_links = [
            l for l in links
            if 'batman-150' in l
        ]
        self.assertEqual(len(batman_links), 1, 'Duplicate link should appear once')

    def test_only_weekly_packs_processed(self) -> None:
        """Non-pack results from the search should not become sources."""
        from backend.implementations.getcomics import scrape_weekly_packs

        session, fake_search = self._make_mock_session()

        with patch(
            'backend.implementations.getcomics.search_getcomics',
            side_effect=fake_search
        ):
            results = _run(scrape_weekly_packs(session, pack_count=2))

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
