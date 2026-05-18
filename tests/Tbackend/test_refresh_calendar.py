"""
Tests for RefreshCalendar ±30-day pre-warm with week-by-week seeding.
"""
import sys
import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock, call, patch

if 'grp' not in sys.modules:
    sys.modules['grp'] = MagicMock()

from backend.features.tasks import RefreshCalendar


class TestRefreshCalendarWindow(unittest.TestCase):
    """RefreshCalendar must pre-warm ±30 days and seed each week individually."""

    def _run_with_fake_today(self, fake_today: date):
        """Run RefreshCalendar.run() with a fixed 'today' and capture calls."""
        calls = []

        def fake_get_calendar(start, end,
                               publisher_names=None, force_refresh=False):
            calls.append({'start': start, 'end': end,
                          'force_refresh': force_refresh})
            return []

        ws_mock = MagicMock()
        ws_mock.emit = MagicMock()

        with patch('backend.features.calendar.get_calendar',
                   side_effect=fake_get_calendar), \
             patch('backend.features.tasks.WebSocket',
                   return_value=ws_mock), \
             patch('backend.features.tasks.date') as mock_date_cls:

            # Make date.today() return fake_today while keeping date() constructor
            mock_date_cls.today.return_value = fake_today
            mock_date_cls.side_effect = lambda *a, **kw: date(*a, **kw)

            task = RefreshCalendar()
            task.run()

        return calls

    def test_superset_call_uses_30_day_window(self):
        """First call must be force_refresh=True with ±30-day range."""
        fake_today = date(2026, 3, 15)
        calls = self._run_with_fake_today(fake_today)

        superset = calls[0]
        self.assertTrue(superset['force_refresh'])
        self.assertEqual(superset['start'],
                         (fake_today - timedelta(days=30)).isoformat())
        self.assertEqual(superset['end'],
                         (fake_today + timedelta(days=30)).isoformat())

    def test_week_calls_use_force_refresh_false(self):
        """All week-by-week calls must use force_refresh=False (hits Kapowarr cache)."""
        calls = self._run_with_fake_today(date(2026, 3, 15))
        week_calls = calls[1:]
        self.assertTrue(len(week_calls) >= 8,
                        f'Expected ≥8 week calls, got {len(week_calls)}')
        for c in week_calls:
            self.assertFalse(c['force_refresh'],
                             f'Week call had force_refresh=True: {c}')

    def test_week_calls_cover_entire_window(self):
        """Week calls must collectively span the full ±30-day window."""
        fake_today = date(2026, 3, 15)
        calls = self._run_with_fake_today(fake_today)
        week_calls = calls[1:]

        earliest = min(c['start'] for c in week_calls)
        latest = max(c['end'] for c in week_calls)
        window_start = (fake_today - timedelta(days=30)).isoformat()
        window_end = (fake_today + timedelta(days=30)).isoformat()

        self.assertLessEqual(earliest, window_start)
        self.assertGreaterEqual(latest, window_end)


if __name__ == '__main__':
    unittest.main()
