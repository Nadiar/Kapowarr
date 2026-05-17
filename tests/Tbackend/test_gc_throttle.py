"""
Tests for GetComics 429 throttle handling in AsyncSession.
"""
import unittest

from backend.base.definitions import Constants


class TestGCThrottleConstant(unittest.TestCase):
    def test_gc_throttle_default_wait_exists(self):
        self.assertTrue(hasattr(Constants, 'GC_THROTTLE_DEFAULT_WAIT'))

    def test_gc_throttle_default_wait_is_positive_int(self):
        self.assertIsInstance(Constants.GC_THROTTLE_DEFAULT_WAIT, int)
        self.assertGreater(Constants.GC_THROTTLE_DEFAULT_WAIT, 0)


if __name__ == '__main__':
    unittest.main()
