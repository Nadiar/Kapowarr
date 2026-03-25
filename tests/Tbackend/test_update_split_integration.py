# -*- coding: utf-8 -*-

"""
Integration tests verifying the 4 new tasks replaced UpdateAll
and that task_library, action strings, and priorities are correct.
"""
import sys
import unittest
from unittest.mock import MagicMock

# Windows: avoid grp import failure
if 'grp' not in sys.modules:
    sys.modules['grp'] = MagicMock()


class TestTaskLibraryRegistration(unittest.TestCase):

    def _get_task_library(self):
        from backend.base.helpers import get_subclasses
        from backend.features.tasks import Task
        return {cls.action: cls for cls in get_subclasses(Task)}

    def test_new_tasks_in_library(self):
        lib = self._get_task_library()
        self.assertIn('sync_issues', lib)
        self.assertIn('refresh_metadata', lib)
        self.assertIn('scan_files', lib)
        self.assertIn('special_version_refresh', lib)

    def test_update_all_not_in_library(self):
        lib = self._get_task_library()
        self.assertNotIn('update_all', lib)

    def test_action_strings(self):
        from backend.features.tasks import (SyncIssues, RefreshMetadata,
                                            ScanFiles, SpecialVersionRefresh)
        self.assertEqual(SyncIssues.action, 'sync_issues')
        self.assertEqual(RefreshMetadata.action, 'refresh_metadata')
        self.assertEqual(ScanFiles.action, 'scan_files')
        self.assertEqual(SpecialVersionRefresh.action, 'special_version_refresh')

    def test_priority_ordering(self):
        from backend.features.tasks import (SyncIssues, RefreshMetadata,
                                            ScanFiles, SpecialVersionRefresh)
        self.assertEqual(SyncIssues.priority, 3)
        self.assertEqual(RefreshMetadata.priority, 3)
        self.assertEqual(ScanFiles.priority, 3)
        self.assertEqual(SpecialVersionRefresh.priority, 4)

    def test_refresh_and_scan_requires_volume_id(self):
        import inspect
        from backend.implementations.volumes import refresh_and_scan
        sig = inspect.signature(refresh_and_scan)
        params = sig.parameters
        # volume_id must be a required positional arg (no default)
        self.assertIn('volume_id', params)
        self.assertEqual(
            params['volume_id'].default,
            inspect.Parameter.empty
        )

    def test_refresh_and_scan_no_allow_skipping(self):
        import inspect
        from backend.implementations.volumes import refresh_and_scan
        sig = inspect.signature(refresh_and_scan)
        self.assertNotIn('allow_skipping', sig.parameters)


if __name__ == '__main__':
    unittest.main()
