import unittest
from sqlite3 import OperationalError
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import sys
import types

# Some backend imports expect Unix-only modules when loading Settings.
if 'grp' not in sys.modules:
    sys.modules['grp'] = types.SimpleNamespace(
        getgrgid=lambda *_args, **_kwargs: None,
        getgrnam=lambda *_args, **_kwargs: None
    )

if 'pwd' not in sys.modules:
    sys.modules['pwd'] = types.SimpleNamespace(
        getpwnam=lambda *_args, **_kwargs: None,
        getpwuid=lambda *_args, **_kwargs: None
    )

from backend.features.post_processing import remove_from_queue


class _RetryCursor:
    def __init__(self, failures_before_success: int):
        self.failures_before_success = failures_before_success
        self.execute_calls = 0
        self.connection = MagicMock()

    def execute(self, *_args, **_kwargs):
        self.execute_calls += 1
        if self.execute_calls <= self.failures_before_success:
            raise OperationalError('database is locked')
        return self


class TestRemoveFromQueueRetries(unittest.TestCase):
    def test_remove_from_queue_retries_on_db_lock(self):
        cursor = _RetryCursor(failures_before_success=2)
        download = SimpleNamespace(id=42)

        with patch('backend.features.post_processing.get_db',
                   return_value=cursor):
            with patch('backend.features.post_processing.sleep'):
                remove_from_queue(download)

        self.assertEqual(cursor.execute_calls, 3)
        cursor.connection.commit.assert_called_once()

    def test_remove_from_queue_raises_on_non_lock_operational_error(self):
        download = SimpleNamespace(id=7)

        class _ErrorCursor:
            def execute(self, *_args, **_kwargs):
                raise OperationalError('disk I/O error')

        with patch('backend.features.post_processing.get_db',
                   return_value=_ErrorCursor()):
            with self.assertRaises(OperationalError):
                remove_from_queue(download)


if __name__ == '__main__':
    unittest.main()
