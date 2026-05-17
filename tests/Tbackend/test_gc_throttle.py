"""
Tests for GetComics 429 throttle handling in AsyncSession.
"""
import unittest

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


if __name__ == '__main__':
    unittest.main()
