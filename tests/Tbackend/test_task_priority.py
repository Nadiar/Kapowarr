# -*- coding: utf-8 -*-

"""
Tests for task priority levels and cooperative yield mechanism.
"""

import sys
import threading
import unittest
from unittest.mock import MagicMock

# The import chain tasks -> volumes -> ... -> root_folders -> settings
# tries to import the Linux-only 'grp' module. Mock before kapowarr imports.
if 'grp' not in sys.modules:
    sys.modules['grp'] = MagicMock()

# isort: off
from backend.features.tasks import (  # noqa: E402
    AutoSearchIssue, AutoSearchVolume, HealthCheck,
    MassConvertIssue, MassConvertVolume,
    MassRenameIssue, MassRenameVolume,
    RefreshAndScanVolume, RefreshCalendar,
    RefreshMetadata, ScanFiles, SearchAll, SearchRecent,
    SpecialVersionRefresh, SyncIssues, Task)
# isort: on


class TestTaskPriorityAttribute(unittest.TestCase):
    """Priority class attribute present on Task ABC and all subclasses."""

    def test_default_priority_exists_on_base_class(self):
        """Task base class should define a priority class attribute."""
        self.assertTrue(
            hasattr(Task, 'priority'),
            'Task base class must have a priority attribute'
        )

    def test_search_all_lower_priority_than_search_recent(self):
        """SearchAll (slow batch) must have lower priority than SearchRecent."""
        self.assertGreater(SearchAll.priority, SearchRecent.priority)

    def test_search_all_lower_priority_than_health_check(self):
        """SearchAll must have lower priority than HealthCheck."""
        self.assertGreater(SearchAll.priority, HealthCheck.priority)

    def test_search_all_lower_priority_than_refresh_calendar(self):
        """SearchAll must have lower priority than RefreshCalendar."""
        self.assertGreater(SearchAll.priority, RefreshCalendar.priority)

    def test_user_tasks_have_higher_priority_than_search_all(self):
        """User-initiated tasks must have higher priority than SearchAll."""
        for cls in [
            AutoSearchIssue, AutoSearchVolume,
            MassRenameIssue, MassConvertIssue,
            MassRenameVolume, MassConvertVolume,
        ]:
            self.assertLess(
                cls.priority, SearchAll.priority,
                f'{cls.__name__}.priority must be < SearchAll.priority'
            )

    def test_all_concrete_task_subclasses_have_int_priority(self):
        """Every concrete Task subclass should have an integer priority."""
        for cls in [
            AutoSearchIssue, AutoSearchVolume,
            MassRenameIssue, MassConvertIssue,
            MassRenameVolume, MassConvertVolume,
            RefreshAndScanVolume,
            SyncIssues, RefreshMetadata, ScanFiles, SpecialVersionRefresh,
            SearchAll, SearchRecent, HealthCheck, RefreshCalendar,
        ]:
            self.assertIsInstance(
                cls.priority, int,
                f'{cls.__name__}.priority must be an int'
            )


class TestYieldEvent(unittest.TestCase):
    """_ensure_yield_event() lazy init and check_yield() behaviour."""

    def test_yield_event_created_and_set(self):
        """_ensure_yield_event() should create a set (unblocked) Event."""
        task = SearchAll()
        evt = task._ensure_yield_event()
        self.assertIsInstance(evt, threading.Event)
        self.assertTrue(
            evt.is_set(),
            '_yield_event must start in the set (unblocked) state'
        )

    def test_yield_event_independent_per_instance(self):
        """Two instances must have independent yield events."""
        t1 = SearchAll()
        t2 = SearchAll()
        t1._ensure_yield_event().clear()
        self.assertTrue(
            t2._ensure_yield_event().is_set(),
            'Clearing t1._yield_event must not affect t2'
        )


class TestCheckYield(unittest.TestCase):
    """check_yield() cooperative pause/resume behaviour."""

    def test_check_yield_returns_immediately_when_not_paused(self):
        """check_yield() must return without blocking when event is set."""
        task = SearchAll()
        # If this line blocks, the test runner timeout will catch it.
        task.check_yield()

    def test_check_yield_blocks_when_event_cleared(self):
        """check_yield() must block when _yield_event is cleared."""
        task = SearchAll()
        task._ensure_yield_event().clear()

        resumed = threading.Event()

        def worker():
            task.check_yield()
            resumed.set()

        t = threading.Thread(target=worker, daemon=True)
        t.start()

        # 200 ms is enough to confirm the call is blocking
        did_return = resumed.wait(timeout=0.2)

        # Cleanup before assertion so the daemon thread always exits
        task._ensure_yield_event().set()
        t.join(timeout=1.0)

        self.assertFalse(did_return, 'check_yield should block when paused')

    def test_check_yield_resumes_when_event_set(self):
        """check_yield() must unblock once _yield_event is set externally."""
        task = SearchAll()
        task._ensure_yield_event().clear()

        resumed = threading.Event()

        def worker():
            task.check_yield()
            resumed.set()

        t = threading.Thread(target=worker, daemon=True)
        t.start()

        t.join(timeout=0.05)              # let it settle into the wait
        task._ensure_yield_event().set()   # signal resume

        did_resume = resumed.wait(timeout=1.0)
        t.join(timeout=1.0)

        self.assertTrue(
            did_resume,
            'check_yield should unblock after event.set()'
        )


if __name__ == '__main__':
    unittest.main()
