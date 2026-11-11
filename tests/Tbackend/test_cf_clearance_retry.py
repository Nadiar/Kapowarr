"""On a CloudFlare challenge, FlareSolverr clears the site's front page and
the original request is retried with the clearance cookie. Asking it to
load a file directly never finishes, as the browser downloads the file
instead of showing a page."""

import asyncio
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch

from backend.base.definitions import ProxyType
from backend.base.helpers import AsyncSession, Session
from backend.implementations.flaresolverr import FSCache

CLEARANCE = 'good-clearance'
SOLVED_UA = 'SolvedBrowser/1.0'


class ProtectedSiteAndFlareSolverr(BaseHTTPRequestHandler):
    """`POST /v1` answers like FlareSolverr. Any other path is a file behind
    a CloudFlare challenge, served only with the clearance cookie and the
    user agent that solved it."""

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.solve_requests.append(body['url'])
        self.reply(200, 'application/json', json.dumps({
            'status': 'ok',
            'solution': {
                'url': body['url'],
                'status': 200,
                'headers': {},
                'response': '<html>front page</html>',
                'userAgent': SOLVED_UA,
                'cookies': [{
                    'name': 'cf_clearance', 'value': CLEARANCE,
                    'sameSite': 'None', 'httpOnly': True, 'secure': False,
                    'path': '/', 'domain': '127.0.0.1', 'expiry': 4102444800
                }]
            }
        }).encode())

    def do_GET(self):
        if self.path.startswith('/dls/'):
            # Like GetComics' links, which redirect to its file server
            self.reply(302, 'text/html', b'', {
                'Location': 'http://127.0.0.1:%d/2026/Comic%%20001.cbz'
                % self.server.server_port
            })
            return

        cleared = (
            f'cf_clearance={CLEARANCE}' in (self.headers['Cookie'] or '')
            and self.headers['User-Agent'] == SOLVED_UA
            and self.server.accept_clearance
        )
        if cleared:
            self.reply(200, 'application/octet-stream', b'FILE')
        else:
            self.reply(403, 'text/html', b'Just a moment...',
                       {'cf-mitigated': 'challenge'})

    def reply(self, status, content_type, payload, extra_headers=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(payload)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        return


class ClearanceRetryTestCase(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(
            ('127.0.0.1', 0), ProtectedSiteAndFlareSolverr
        )
        self.server.solve_requests = []
        self.server.accept_clearance = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

        self.base = 'http://127.0.0.1:%d' % self.server.server_port
        self.file_url = self.base + '/2026/Comic%20001.cbz'

        settings = patch(
            'backend.implementations.flaresolverr.Settings').start()
        settings.return_value.sv = MagicMock(
            flaresolverr_base_url=self.base, proxy_type=ProxyType.NONE
        )
        self.addCleanup(patch.stopall)

        FSCache.failed_until.clear()
        FSCache.cookie_mapping.clear()
        FSCache.ua_mapping.clear()

    def fetch_async(self, url=None):
        async def fetch():
            async with AsyncSession() as session:
                response = await session.get(url or self.file_url)
                return response.status, await response.read()
        return asyncio.run(fetch())


class TestClearanceRetry(ClearanceRetryTestCase):
    def test_async_request_gets_the_file_after_clearing_the_front_page(self):
        status, body = self.fetch_async()

        self.assertEqual((status, body), (200, b'FILE'))
        self.assertEqual(self.server.solve_requests, [self.base + '/'])

    def test_sync_request_gets_the_file_after_clearing_the_front_page(self):
        with Session() as session:
            response = session.get(self.file_url)

        self.assertEqual((response.status_code, response.content),
                         (200, b'FILE'))
        self.assertEqual(self.server.solve_requests, [self.base + '/'])

    def test_later_requests_reuse_the_clearance_without_solving_again(self):
        self.fetch_async()
        status, body = self.fetch_async()
        with Session() as session:
            response = session.get(self.file_url)

        self.assertEqual((status, body), (200, b'FILE'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.server.solve_requests), 1)

    def test_link_that_redirects_to_a_cleared_site_reuses_the_clearance(self):
        link = 'http://localhost:%d/dls/abc' % self.server.server_port

        first = self.fetch_async(link)
        second = self.fetch_async(link)
        with Session() as session:
            response = session.get(link)

        self.assertEqual(first, (200, b'FILE'))
        self.assertEqual(second, (200, b'FILE'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.server.solve_requests), 1)

    def test_rejected_clearance_is_solved_once_more_then_given_up(self):
        self.server.accept_clearance = False

        with Session() as session:
            response = session.get(self.file_url)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(self.server.solve_requests), 2)


if __name__ == '__main__':
    unittest.main()
