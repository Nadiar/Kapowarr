# -*- coding: utf-8 -*-

"""
Integration tests verifying the 4 new tasks replaced UpdateAll
and that the task registry, action strings, and priorities are correct.
"""
import sys
import unittest
from unittest.mock import MagicMock

# Windows: avoid grp import failure
if 'grp' not in sys.modules:
    sys.modules['grp'] = MagicMock()


class TestTaskLibraryRegistration(unittest.TestCase):

    def _get_task_library(self):
        from backend.features.tasks import TaskHandler
        return TaskHandler.tasks

    def test_new_tasks_in_library(self):
        lib = self._get_task_library()
        self.assertIn('sync_issues', lib)
        self.assertIn('refresh_metadata', lib)
        self.assertIn('scan_files', lib)
        self.assertIn('special_version_refresh', lib)

    def test_update_all_kept_alongside_split_tasks(self):
        lib = self._get_task_library()
        self.assertIn('update_all', lib)

    def test_action_strings(self):
        from backend.features.tasks import (RefreshMetadata, ScanFiles,
                                            SpecialVersionRefresh, SyncIssues)
        self.assertEqual(SyncIssues.action, 'sync_issues')
        self.assertEqual(RefreshMetadata.action, 'refresh_metadata')
        self.assertEqual(ScanFiles.action, 'scan_files')
        self.assertEqual(
            SpecialVersionRefresh.action,
            'special_version_refresh')

    def test_priority_ordering(self):
        from backend.features.tasks import (RefreshMetadata, ScanFiles,
                                            SpecialVersionRefresh, SyncIssues)
        self.assertEqual(SyncIssues.priority, 3)
        self.assertEqual(RefreshMetadata.priority, 3)
        self.assertEqual(ScanFiles.priority, 3)
        self.assertEqual(SpecialVersionRefresh.priority, 4)

    def test_refresh_and_scan_supports_single_and_library(self):
        import inspect

        from backend.implementations.volumes import refresh_and_scan
        params = inspect.signature(refresh_and_scan).parameters
        # volume_id is optional: None means the whole library (UpdateAll)
        self.assertIn('volume_id', params)
        self.assertIsNone(params['volume_id'].default)
        self.assertIn('allow_skipping', params)


if __name__ == '__main__':
    unittest.main()
