"""A link behind a Cloudflare challenge that FlareSolverr can't solve is
temporarily unavailable, not broken: it isn't blocklisted, so it's tried
again later."""

import asyncio
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

if 'grp' not in sys.modules:
    sys.modules['grp'] = MagicMock()

from requests import HTTPError

from backend.base.custom_exceptions import (DownloadLinkBroken,
                                            DownloadLinkChallenged)
from backend.base.definitions import DownloadService, GCDownloadService
from backend.implementations.download_clients.base import BaseDirectDownload
from backend.implementations.download_preppers.ddl import \
    GetComics as gc_prepper

LINK = 'https://getcomics.org/dls/abc'
CHALLENGE_HEADERS = {'cf-mitigated': 'challenge'}


def fake_response(status: int, headers: dict) -> MagicMock:
    response = MagicMock(status=status, ok=status < 400)
    response.headers = headers
    return response


def purify(response: MagicMock):
    session = MagicMock()
    session.get = AsyncMock(return_value=response)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    with patch.object(gc_prepper, 'AsyncSession', return_value=session):
        return asyncio.run(
            gc_prepper._purify_link(GCDownloadService.GETCOMICS, LINK)
        )


class TestPurifyLink(unittest.TestCase):
    def test_unsolved_challenge_is_not_a_broken_link(self):
        with self.assertRaises(DownloadLinkChallenged):
            purify(fake_response(403, CHALLENGE_HEADERS))

    def test_missing_page_is_still_a_broken_link(self):
        with self.assertRaises(DownloadLinkBroken):
            purify(fake_response(404, {}))


class TestDownloadGroup(unittest.TestCase):
    def test_challenged_link_is_not_blocklisted_and_counts_as_temporary(self):
        prepper = gc_prepper.GetComicsPrepper.__new__(
            gc_prepper.GetComicsPrepper
        )
        prepper.link = 'https://getcomics.org/x/comic-1/'
        prepper._web_title = 'Comic #1'
        prepper.volume_id = 1
        prepper.issue_id = None
        prepper.force_match = False
        group = {
            'links': {GCDownloadService.GETCOMICS: [LINK]},
            'web_sub_title': None,
            'info': {'issue_number': 1.0}
        }
        purify_group = getattr(
            prepper, '_GetComicsPrepper__purify_download_group'
        )

        with patch.object(
            gc_prepper, '_purify_link',
            AsyncMock(side_effect=DownloadLinkChallenged(LINK))
        ), patch.object(gc_prepper, 'add_to_blocklist') as blocklist, \
                patch.object(gc_prepper, 'iter_commit', lambda x: x):
            result = asyncio.run(purify_group(group))

        self.assertEqual(result, (None, True))
        blocklist.assert_not_called()


class TestDirectDownload(unittest.TestCase):
    def test_challenged_download_raises_challenged_not_broken(self):
        response = MagicMock(
            status_code=403,
            url='https://fs2.comicfiles.ru/file.cbz',
            headers=CHALLENGE_HEADERS
        )
        response.__enter__ = MagicMock(return_value=response)
        response.__exit__ = MagicMock(return_value=False)
        response.raise_for_status.side_effect = HTTPError(response=response)

        module = 'backend.implementations.download_clients.base'
        with patch(f'{module}.Settings'), \
                patch(f'{module}.Session') as session, \
                patch.object(
                    BaseDirectDownload, '_convert_to_pure_link',
                    return_value=response.url
        ):
            session.return_value.get.return_value = response
            with self.assertRaises(DownloadLinkChallenged):
                BaseDirectDownload(
                    download_link=LINK,
                    volume_id=1,
                    covered_issues=1.0,
                    download_service=DownloadService.GETCOMICS,
                    source_name='GetComics',
                    web_link=None,
                    web_title=None,
                    web_sub_title=None
                )


if __name__ == '__main__':
    unittest.main()
