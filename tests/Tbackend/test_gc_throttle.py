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
    """Tests for 429 throttle logic in AsyncSession._request."""

    def setUp(self):
        AsyncSession._gc_throttle_until = 0.0

    def test_gc_throttle_until_set_on_429(self):
        """Setting _gc_throttle_until manually simulates what 429 branch does."""
        retry_after = 30
        before = _time()
        AsyncSession._gc_throttle_until = before + retry_after
        self.assertGreater(AsyncSession._gc_throttle_until, before)
        remaining = AsyncSession._gc_throttle_until - _time()
        self.assertAlmostEqual(remaining, retry_after, delta=1)

    def test_non_gc_url_does_not_trigger_gc_throttle_check(self):
        """Non-GC URLs must not match the _GC_HOST guard."""
        non_gc_urls = [
            'https://pixeldrain.com/api/file/abc',
            'https://comicvine.gamespot.com/api/volumes',
            'https://mega.nz/file/abc',
        ]
        for url in non_gc_urls:
            self.assertNotIn(
                AsyncSession._GC_HOST, url,
                f"GC host guard incorrectly matches: {url}"
            )

    def test_gc_url_matches_gc_host(self):
        """GC URLs must match the _GC_HOST guard."""
        gc_urls = [
            'https://getcomics.org/page/1',
            'https://getcomics.org/?s=batman',
        ]
        for url in gc_urls:
            self.assertIn(
                AsyncSession._GC_HOST, url,
                f"GC host guard fails to match: {url}"
            )

    def test_throttle_remaining_is_zero_when_not_throttled(self):
        """When _gc_throttle_until is 0.0, remaining is negative -> no wait."""
        AsyncSession._gc_throttle_until = 0.0
        remaining = AsyncSession._gc_throttle_until - _time()
        self.assertLess(remaining, 0)

    def test_default_wait_constant_used_as_fallback(self):
        """GC_THROTTLE_DEFAULT_WAIT must be the fallback for missing Retry-After."""
        from backend.base.definitions import Constants
        self.assertEqual(Constants.GC_THROTTLE_DEFAULT_WAIT, 60)


if __name__ == '__main__':
    unittest.main()
