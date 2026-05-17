"""
Tests for GetComics 429 throttle handling in AsyncSession.
"""
import asyncio
import unittest
from time import time as _time
from unittest.mock import AsyncMock, MagicMock, patch

from backend.base.definitions import Constants
from backend.base.helpers import AsyncSession


class TestGCThrottleConstant(unittest.TestCase):
    def test_gc_throttle_default_wait_exists(self):
        self.assertTrue(hasattr(Constants, 'GC_THROTTLE_DEFAULT_WAIT'))

    def test_gc_throttle_default_wait_is_positive_int(self):
        self.assertIsInstance(Constants.GC_THROTTLE_DEFAULT_WAIT, int)
        self.assertGreater(Constants.GC_THROTTLE_DEFAULT_WAIT, 0)


class TestAsyncSessionThrottleState(unittest.TestCase):
    def setUp(self):
        # Reset class state before each test
        AsyncSession._gc_throttle_until = 0.0

    def test_gc_host_attribute_exists(self):
        self.assertTrue(hasattr(AsyncSession, '_GC_HOST'))
        self.assertIn('getcomics', AsyncSession._GC_HOST)

    def test_gc_throttle_until_starts_at_zero(self):
        self.assertEqual(AsyncSession._gc_throttle_until, 0.0)

    def test_gc_throttle_until_is_class_level(self):
        # Writing to the class affects all instances
        AsyncSession._gc_throttle_until = 999.0
        self.assertEqual(AsyncSession._gc_throttle_until, 999.0)
        AsyncSession._gc_throttle_until = 0.0  # reset


class TestAsyncSession429Logic(unittest.TestCase):
    """Tests that _request detects GC 429s and applies site-wide throttle."""

    def setUp(self):
        AsyncSession._gc_throttle_until = 0.0

    def _build_session(self):
        """Create an AsyncSession without triggering __init__.

        aiohttp.ClientSession.headers is a read-only property backed by
        ``_default_headers``, and cookie_jar by ``_cookie_jar``.  We write
        the backing attributes directly via object.__setattr__ so the
        property getters return our mocks without triggering any setter.
        """
        session = AsyncSession.__new__(AsyncSession)
        fs = MagicMock()
        fs.get_ua_cookies.return_value = ('test-ua', {})
        fs.handle_cf_block_async = AsyncMock(return_value=None)
        object.__setattr__(session, 'fs', fs)
        # session.headers -> session._default_headers (aiohttp property)
        default_headers = MagicMock()
        default_headers.update = lambda d: None
        object.__setattr__(session, '_default_headers', default_headers)
        # session.cookie_jar -> session._cookie_jar (aiohttp property)
        cookie_jar = MagicMock()
        object.__setattr__(session, '_cookie_jar', cookie_jar)
        return session

    def _make_response(self, status, headers=None):
        """Build a minimal fake aiohttp response."""
        resp = MagicMock()
        resp.status = status
        resp.headers = headers or {}
        resp.url = 'https://getcomics.org/test'
        resp.real_url = resp.url
        resp.text = AsyncMock(return_value='')
        return resp

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_429_from_gc_url_sets_throttle_timestamp(self):
        """A 429 from a GC URL must set _gc_throttle_until > now."""
        session = self._build_session()
        gc_url = 'https://getcomics.org/?s=batman'

        resp_429 = self._make_response(429, {'Retry-After': '30'})
        resp_200 = self._make_response(200)
        call_count = [0]

        async def fake_parent_request(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return resp_429
            return resp_200

        slept = []

        async def fake_sleep(s):
            slept.append(s)

        before = _time()
        with patch(
            'aiohttp.ClientSession._request',
            new=fake_parent_request
        ), patch('backend.base.helpers.sleep', side_effect=fake_sleep):
            self._run(session._request('GET', gc_url))

        self.assertGreater(
            AsyncSession._gc_throttle_until, before,
            "_gc_throttle_until must be set to a future timestamp after 429"
        )
        # sleep must have been called at least once with the retry_after value
        self.assertIn(30, slept, "sleep(30) must be called for Retry-After: 30")

    def test_429_from_gc_url_retries_until_success(self):
        """After a 429, _request must retry and return the successful response."""
        session = self._build_session()
        gc_url = 'https://getcomics.org/?s=batman'

        resp_429 = self._make_response(429, {'Retry-After': '1'})
        resp_200 = self._make_response(200)
        calls = [resp_429, resp_200]
        idx = [0]

        async def fake_parent_request(*args, **kwargs):
            r = calls[idx[0]]
            idx[0] += 1
            return r

        async def fake_sleep(_):
            pass

        with patch(
            'aiohttp.ClientSession._request',
            new=fake_parent_request
        ), patch('backend.base.helpers.sleep', side_effect=fake_sleep):
            result = self._run(session._request('GET', gc_url))

        self.assertEqual(result.status, 200)

    def test_non_gc_429_does_not_set_gc_throttle(self):
        """A 429 from a non-GC URL must NOT update _gc_throttle_until."""
        session = self._build_session()
        non_gc_url = 'https://pixeldrain.com/api/file/abc'

        resp_429 = self._make_response(429)
        resp_429.url = non_gc_url

        async def fake_parent_request(*args, **kwargs):
            return resp_429

        async def fake_sleep(_):
            pass

        with patch(
            'aiohttp.ClientSession._request',
            new=fake_parent_request
        ), patch('backend.base.helpers.sleep', side_effect=fake_sleep):
            result = self._run(session._request('GET', non_gc_url))

        self.assertEqual(AsyncSession._gc_throttle_until, 0.0,
                         "Non-GC 429 must not modify _gc_throttle_until")
        self.assertEqual(result.status, 429)

    def test_pre_request_sleep_when_throttled(self):
        """When _gc_throttle_until is in the future, _request must sleep first."""
        session = self._build_session()
        gc_url = 'https://getcomics.org/?s=batman'

        future_time = _time() + 10
        AsyncSession._gc_throttle_until = future_time

        resp_200 = self._make_response(200)

        async def fake_parent_request(*args, **kwargs):
            return resp_200

        slept = []

        async def fake_sleep(s):
            slept.append(s)
            # Clear throttle so the loop doesn't sleep again
            AsyncSession._gc_throttle_until = 0.0

        with patch(
            'aiohttp.ClientSession._request',
            new=fake_parent_request
        ), patch('backend.base.helpers.sleep', side_effect=fake_sleep):
            self._run(session._request('GET', gc_url))

        self.assertTrue(
            any(s > 0 for s in slept),
            "Must sleep a positive amount when pre-throttled"
        )

    def test_429_uses_default_wait_when_no_retry_after(self):
        """Missing Retry-After header must fall back to GC_THROTTLE_DEFAULT_WAIT."""
        from backend.base.definitions import Constants

        session = self._build_session()
        gc_url = 'https://getcomics.org/?s=batman'

        resp_429 = self._make_response(429)  # no Retry-After header
        resp_200 = self._make_response(200)
        calls = [resp_429, resp_200]
        idx = [0]

        async def fake_parent_request(*args, **kwargs):
            r = calls[idx[0]]
            idx[0] += 1
            return r

        slept = []

        async def fake_sleep(s):
            slept.append(s)

        with patch(
            'aiohttp.ClientSession._request',
            new=fake_parent_request
        ), patch('backend.base.helpers.sleep', side_effect=fake_sleep):
            self._run(session._request('GET', gc_url))

        self.assertIn(
            Constants.GC_THROTTLE_DEFAULT_WAIT, slept,
            "Must sleep GC_THROTTLE_DEFAULT_WAIT when Retry-After is absent"
        )

    def test_429_uses_default_wait_when_retry_after_is_http_date(self):
        """HTTP-date format in Retry-After must fall back to GC_THROTTLE_DEFAULT_WAIT."""
        from backend.base.definitions import Constants

        session = self._build_session()
        gc_url = 'https://getcomics.org/?s=batman'

        # RFC 7231 HTTP-date format
        http_date = 'Fri, 16 May 2026 12:00:00 GMT'
        resp_429 = self._make_response(429, {'Retry-After': http_date})
        resp_200 = self._make_response(200)
        calls = [resp_429, resp_200]
        idx = [0]

        async def fake_parent_request(*args, **kwargs):
            r = calls[idx[0]]
            idx[0] += 1
            return r

        slept = []

        async def fake_sleep(s):
            slept.append(s)

        with patch(
            'aiohttp.ClientSession._request',
            new=fake_parent_request
        ), patch('backend.base.helpers.sleep', side_effect=fake_sleep):
            result = self._run(session._request('GET', gc_url))

        self.assertEqual(result.status, 200,
                         "Must retry and succeed after HTTP-date Retry-After")
        self.assertIn(
            Constants.GC_THROTTLE_DEFAULT_WAIT, slept,
            "Must sleep GC_THROTTLE_DEFAULT_WAIT when Retry-After is HTTP-date"
        )


if __name__ == '__main__':
    unittest.main()
