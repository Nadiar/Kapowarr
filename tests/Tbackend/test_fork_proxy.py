"""The local ComicVine cache must never be sent through the HTTP proxy."""

import unittest

from backend.internals.fork_proxy import proxy_ignored_addresses

IGNORED = ['localhost', '127.0.0.1', 'flaresolverr', 'transmission']


class TestProxyIgnoredAddresses(unittest.TestCase):

    def test_local_comicvine_cache_host_is_added(self) -> None:
        result = proxy_ignored_addresses(IGNORED, 'http://cvproxy:8585/api')
        self.assertEqual(list(result), IGNORED + ['cvproxy'])

    def test_result_is_a_comma_list_string_for_no_proxy(self) -> None:
        result = proxy_ignored_addresses(['localhost'], 'http://cvproxy:8585/api')
        self.assertEqual(str(result), 'localhost,cvproxy')

    def test_official_comicvine_still_goes_through_the_proxy(self) -> None:
        result = proxy_ignored_addresses(
            IGNORED, 'https://comicvine.gamespot.com/api'
        )
        self.assertEqual(list(result), IGNORED)

    def test_host_already_ignored_is_not_added_twice(self) -> None:
        result = proxy_ignored_addresses(
            IGNORED + ['cvproxy'], 'http://cvproxy:8585/api'
        )
        self.assertEqual(list(result).count('cvproxy'), 1)

    def test_ip_address_host_is_added(self) -> None:
        result = proxy_ignored_addresses(IGNORED, 'http://10.0.0.5:8585/api')
        self.assertIn('10.0.0.5', list(result))

    def test_empty_or_unparseable_url_changes_nothing(self) -> None:
        for url in ('', 'not a url'):
            self.assertEqual(list(proxy_ignored_addresses(IGNORED, url)),
                             IGNORED)

    def test_input_list_is_not_modified(self) -> None:
        ignored = list(IGNORED)
        proxy_ignored_addresses(ignored, 'http://cvproxy:8585/api')
        self.assertEqual(ignored, IGNORED)


if __name__ == '__main__':
    unittest.main()
