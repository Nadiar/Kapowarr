from asyncio import sleep
from datetime import datetime
from time import time
from typing import Any, Dict, List, Set, Tuple, Union

from bs4 import BeautifulSoup, NavigableString, Tag
from requests import RequestException

from backend.base.custom_exceptions import ClientNotWorking
from backend.base.definitions import (BrokenClientReason, DownloadType,
                                      IndexerClientField as ICF, QueryResult,
                                      SearchQuery, SearchResultData)
from backend.base.file_extraction import extract_filename_data
from backend.base.helpers import (AsyncSession, Session,
                                  first_of_range, normalise_size)
from backend.implementations.indexer_client_manager import (BaseIndexerClient,
                                                            IndexerClients)

ENGLISH_MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December"
]

# In-memory cache for GetComics search pages. Avoids re-scraping the same query
# during bulk tasks like SearchAll and the SearchRecent tasks.
_SEARCH_CACHE_TTL = 3600 # seconds
_SEARCH_CACHE_MAX = 200
_search_cache: Dict[Tuple[str, str, int], Tuple[float, QueryResult]] = {}


def _get_cached_search(key: Tuple[str, str, int]) -> Union[QueryResult, None]:
    cached = _search_cache.get(key)
    if cached is None or time() - cached[0] >= _SEARCH_CACHE_TTL:
        return None
    return QueryResult(
        [dict(r) for r in cached[1].results], # type: ignore
        next_page_available=cached[1].next_page_available
    )


def _store_cached_search(
    key: Tuple[str, str, int],
    result: QueryResult
) -> None:
    if key not in _search_cache and len(_search_cache) >= _SEARCH_CACHE_MAX:
        oldest_key = min(_search_cache, key=lambda k: _search_cache[k][0])
        del _search_cache[oldest_key]
    _search_cache[key] = (time(), result)
    return


def _extract_pack_entries(html: str, base_url: str) -> List[Tuple[str, str]]:
    """Extract (title, article_url) entries from a weekly pack page.

    Each list item usually has the shape:
        "Title : <a href=\"...\">Download</a>"

    Only links whose anchor text is exactly "Download" are captured, and the
    text before the first anchor is used as the title.

    Args:
        html (str): HTML source of a GC weekly pack page.
        base_url (str): The base URL of the GC instance. Links outside of it
            are ignored.

    Returns:
        List[Tuple[str, str]]: Unique `(title, article_url)` entries.
    """
    soup = BeautifulSoup(html, 'html.parser')
    seen: Set[str] = set()
    entries: List[Tuple[str, str]] = []
    prefix = base_url.rstrip('/') + '/'

    for li in soup.find_all('li'):
        download_link: Union[Tag, None] = None
        for anchor in li.find_all('a', href=True):
            if anchor.get_text(strip=True).lower() == 'download':
                download_link = anchor
                break

        if not download_link:
            continue

        href = str(download_link.get('href', '')).strip()
        if not href.startswith(prefix):
            continue

        title_parts: List[str] = []
        for node in li.descendants:
            if isinstance(node, Tag) and node.name == 'a':
                break
            if isinstance(node, NavigableString):
                text = str(node).strip()
                if text:
                    title_parts.append(text.strip(':').strip())

        title = ' '.join(title_parts).strip().strip(':').strip()
        if not title or href in seen:
            continue

        seen.add(href)
        entries.append((title, href))

    return entries


@IndexerClients.register_client(
    DownloadType.DDL, 'GetComics',
    (
        ICF.TITLE, ICF.ENABLED, ICF.URL,
        ICF.GC_SERVICE_PREFERENCE, ICF.GC_AVOID_LARGE_DOWNLOADS
    ),
    allow_multiple_instances=False
)
class GetComicsIndexer(BaseIndexerClient):
    def __init__(self, indexer_id: int) -> None:
        super().__init__(indexer_id)

        self.session: Union[AsyncSession, None] = None
        self.request_count = 0

        return

    @staticmethod
    def __get_articles(soup: BeautifulSoup) -> List[Tuple[str, str, int]]:
        """From a GC search result page, extract article (single search result)
        data.

        Args:
            soup (BeautifulSoup): The soup of the GC search result page.

        Returns:
            List[Tuple[str, str, int]]: The data of the articles. First string
                is the link, second string is the title, the integer is the byte
                size.
        """
        result: List[Tuple[str, str, int]] = []
        for article in soup.find_all("article", {"class": "post"}):
            title_el = article.find("h1", {"class": "post-title"})
            if not title_el:
                continue

            anchor = title_el.find('a')
            if not anchor:
                continue

            cat_el = article.find("a", {"class": "post-category"})
            if (
                not cat_el
                or cat_el.get_text(strip=True) in ("News", "Sponsored")
            ):
                continue

            title = title_el.get_text(strip=True)
            link: str = first_of_range(anchor.get('href') or '')

            size_container = title_el.next_sibling
            if not isinstance(size_container, Tag):
                size = -1
            else:
                size_p = next(size_container.children, None)
                if not size_p:
                    size = -1
                else:
                    split_size = size_p.get_text().split("Size : ")
                    if len(split_size) != 2:
                        size = -1
                    else:
                        size = normalise_size(split_size[1])

            result.append((link, title, size))

        return result

    async def search(self, query: SearchQuery) -> QueryResult:
        cache_key = (self._url, query["query"], query["page"])
        cached = _get_cached_search(cache_key)
        if cached is not None:
            return cached

        if not self.session:
            self.session = AsyncSession()

        next_page_available = False

        if self.request_count > 5:
            await sleep(1.0)

        page = await self.session.get_text(
            f"{self._url}/",
            params={
                "s": query["query"],
                "paged": query["page"]
            },
            quiet_fail=True
        )
        self.request_count += 1
        if not page:
            return QueryResult([], next_page_available=False)

        soup = BeautifulSoup(page, "html.parser")

        page_links = soup.find_all(["a", "span"], {"class": "page-numbers"})
        if page_links and not (
            page_links[-1].name == "span"
            and "current" in (page_links[-1].get("class") or [])
        ):
            next_page_available = True

        # Process the search results on each page
        formatted_results: List[SearchResultData] = [
            {
                **extract_filename_data(
                    article[1],
                    assume_volume_number=False,
                    fix_year=True
                ),
                "link": article[0],
                "display_title": article[1],
                "size": article[2],
                "indexer_id": self._id,
                "indexer_title": self._title,
                "seeders": None,
                "leechers": None
            }
            for article in self.__get_articles(soup)
        ]

        result = QueryResult(
            formatted_results,
            next_page_available=next_page_available
        )
        _store_cached_search(cache_key, result)
        return result

    async def scrape_weekly_packs(
        self,
        pack_count: int = 3
    ) -> List[SearchResultData]:
        """Search GC for 'Weekly Pack', fetch the N most recent packs, and
        return all individual comic article links as search results.

        Each weekly pack page lists ~80-90 comics. Entries are deduplicated
        across packs and each article page is fetched once so that dead links
        are skipped early.

        Args:
            pack_count (int, optional): How many recent weekly pack pages to
                process.
                Defaults to 3.

        Returns:
            List[SearchResultData]: Individual comic articles from pack pages.
        """
        if not self.session:
            self.session = AsyncSession()

        pack_search = await self.search({
            "query": "Weekly Pack",
            "page": 1,
            "total_available_variations": 1
        })
        pack_articles = [
            r
            for r in pack_search.results
            if 'weekly pack' in r['display_title'].lower()
        ][:pack_count]

        seen: Set[str] = set()
        results: List[SearchResultData] = []
        for pack in pack_articles:
            html = await self.session.get_text(pack['link'], quiet_fail=True)
            if not html:
                continue

            for title, article_url in _extract_pack_entries(html, self._url):
                if article_url in seen:
                    continue
                seen.add(article_url)

                article_html = await self.session.get_text(
                    article_url, quiet_fail=True
                )
                if not article_html:
                    continue

                results.append({
                    **extract_filename_data(
                        title,
                        assume_volume_number=False,
                        fix_year=True
                    ),
                    "link": article_url,
                    "display_title": title,
                    "size": -1,
                    "indexer_id": self._id,
                    "indexer_title": self._title,
                    "seeders": None,
                    "leechers": None
                })

        return results

    async def discover(self, last_check: datetime) -> List[SearchResultData]:
        if not self.session:
            self.session = AsyncSession()

        last_check = last_check.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0
        )

        page = await self.session.get_text(
            f"{self._url}/sitemap/",
            quiet_fail=True
        )
        if not page:
            return []

        result: List[SearchResultData] = []
        soup = BeautifulSoup(page, "html.parser")
        for entry in soup.select(
            ".post-contents > div:first-of-type ul.lcp_catlist > li"
        ):
            strings = list(entry.stripped_strings)
            if len(strings) != 2:
                continue

            display_title, date = strings
            split_date = date.split()
            parsed_date = datetime(
                int(split_date[2]),
                ENGLISH_MONTHS.index(split_date[0]) + 1,
                int(split_date[1].strip(","))
            )
            if last_check > parsed_date:
                continue

            anchor = entry.find('a')
            if not anchor:
                continue

            link: str = first_of_range(anchor.get('href') or '')

            result.append({
                **extract_filename_data(
                    display_title,
                    assume_volume_number=False,
                    fix_year=True
                ),
                "link": link,
                "display_title": display_title,
                "size": -1,
                "indexer_id": self._id,
                "indexer_title": self._title,
                "seeders": None,
                "leechers": None
            })

        return result

    async def shutdown(self) -> None:
        if self.session:
            await self.session.close()
        return

    @classmethod
    def test(cls, url: str, **extra_fields: Any) -> None:
        with Session() as session:
            try:
                html = session.get(url).text
                is_getcomics = "GetComics" in html

                if not is_getcomics:
                    raise ClientNotWorking(
                        BrokenClientReason.NOT_CLIENT_INSTANCE
                    )

            except RequestException:
                raise ClientNotWorking(
                    BrokenClientReason.CONNECTION_ERROR
                )

        return
