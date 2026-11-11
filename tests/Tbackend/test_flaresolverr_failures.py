"""When FlareSolverr can't solve a challenge, Kapowarr asks once instead of
retrying, and skips that server for a while instead of asking again for
every link."""

import asyncio
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import time
from unittest.mock import MagicMock, patch

from backend.base.definitions import ProxyType
from backend.base.helpers import AsyncSession
from backend.implementations.flaresolverr import (FAILED_SOLVE_COOLDOWN,
                                                  FlareSolverr, FSCache)

CHALLENGED_URL = 'https://files.example/comic.cbz'
CHALLENGE_HEADERS = {'cf-mitigated': 'challenge'}
SOLUTION = {
    'url': CHALLENGED_URL,
    'status': 200,
    'headers': {},
    'response': '<html></html>',
    'userAgent': 'Mozilla/5.0',
    'cookies': []
}


class FakeFlareSolverr(BaseHTTPRequestHandler):
    """Answers like FlareSolverr and records every request it gets."""

    def do_POST(self):
        length = int(self.headers['Content-Length'])
        self.server.requests.append(json.loads(self.rfile.read(length)))
        status, body = self.server.reply
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        return


class FlareSolverrTestCase(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), FakeFlareSolverr)
        self.server.requests = []
        self.server.reply = (500, {
            'status': 'error',
            'message': 'Error: Error solving the challenge. Timeout after 60s.'
        })
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

        settings = patch(
            'backend.implementations.flaresolverr.Settings').start()
        settings.return_value.sv = MagicMock(
            flaresolverr_base_url='http://127.0.0.1:%d' %
            self.server.server_port, proxy_type=ProxyType.NONE)
        patch('backend.base.helpers.sleep', new=self.no_sleep).start()
        self.addCleanup(patch.stopall)

        FSCache.failed_until.clear()
        FSCache.cookie_mapping.clear()
        FSCache.ua_mapping.clear()

    @staticmethod
    async def no_sleep(*args):
        return

    def solve_async(self):
        async def solve():
            async with AsyncSession() as session:
                return await session.fs.handle_cf_block_async(
                    session, CHALLENGED_URL, CHALLENGE_HEADERS
                )
        return asyncio.run(solve())

    def solve_sync(self):
        return FlareSolverr().handle_cf_block(
            CHALLENGED_URL, CHALLENGE_HEADERS
        )


class TestFailedSolve(FlareSolverrTestCase):
    def test_failed_solve_is_asked_once_not_retried(self):
        self.assertIsNone(self.solve_async())
        self.assertEqual(len(self.server.requests), 1)

    def test_server_that_failed_is_skipped_for_a_while(self):
        self.solve_async()
        self.assertIsNone(self.solve_async())
        self.assertIsNone(self.solve_sync())

        self.assertEqual(len(self.server.requests), 1)

    def test_server_is_tried_again_after_the_cooldown(self):
        self.solve_sync()

        later = time() + FAILED_SOLVE_COOLDOWN + 1
        with patch('backend.implementations.flaresolverr.time',
                   return_value=later):
            self.solve_sync()

        self.assertEqual(len(self.server.requests), 2)

    def test_sync_failed_solve_returns_none(self):
        self.assertIsNone(self.solve_sync())
        self.assertEqual(len(self.server.requests), 1)


class TestSuccessfulSolve(FlareSolverrTestCase):
    def setUp(self):
        super().setUp()
        self.server.reply = (200, {'status': 'ok', 'solution': SOLUTION})

    def test_solution_is_returned_and_solving_is_capped_at_a_minute(self):
        result = self.solve_async()

        self.assertEqual(result, SOLUTION)
        self.assertEqual(self.server.requests[0]['maxTimeout'], 60_000)


if __name__ == '__main__':
    unittest.main()
