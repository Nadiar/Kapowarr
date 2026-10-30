# -*- coding: utf-8 -*-

"""
Fork-only: keep the local ComicVine cache out of the HTTP proxy.

The fork's `comicvine_api_url` setting can point at a ComicVine cache on the
local network (e.g. `http://cvproxy:8585/api`). An HTTP proxy usually can't
reach such a host, so every metadata request, the calendar and even the
API key check on saving settings would fail. Unless that URL is ComicVine
itself, its host is added to the addresses that bypass the proxy.
"""

from typing import Iterable
from urllib.parse import urlsplit

from backend.base.helpers import CommaList

# Not Constants.CV_API_URL: the fork points that at the Docker cache
OFFICIAL_COMICVINE_HOST = "comicvine.gamespot.com"


def proxy_ignored_addresses(
    ignored_addresses: Iterable[str],
    comicvine_api_url: str
) -> CommaList:
    """The addresses that should bypass the proxy, including the host of a
    local ComicVine cache.

    Args:
        ignored_addresses (Iterable[str]): The configured ignored addresses.
        comicvine_api_url (str): The configured ComicVine API URL.

    Returns:
        CommaList: The configured addresses, plus the cache's host if needed.
    """
    result = CommaList(list(ignored_addresses))
    host = urlsplit(comicvine_api_url).hostname
    if host and host != OFFICIAL_COMICVINE_HOST and host not in result:
        result.append(host)
    return result
