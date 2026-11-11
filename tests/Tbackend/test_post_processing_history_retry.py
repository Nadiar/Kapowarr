"""A locked database while noting a finished download in history is retried,
and never stops the downloaded file from being moved into the library."""

import sys
import types
import unittest
from sqlite3 import OperationalError
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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

from backend.features.post_processing import (PostProcessingContext,
                                              PostProcessor)


def locked() -> OperationalError:
    return OperationalError('database is locked')


def make_download():
    return SimpleNamespace(id=42, files=[])


class TestTryAddToHistory(unittest.TestCase):
    def setUp(self):
        patch('backend.features.post_processing.sleep').start()
        self.commit = patch('backend.features.post_processing.commit').start()
        self.addCleanup(patch.stopall)
        self.ctx = PostProcessingContext(make_download())

    def test_locked_history_write_is_retried_then_saved(self):
        with patch.object(
            self.ctx, 'add_to_history',
            side_effect=[locked(), locked(), None]
        ) as add_to_history:
            self.ctx.try_add_to_history()

        self.assertEqual(add_to_history.call_count, 3)
        self.commit.assert_called_once()

    def test_locked_commit_is_retried_without_writing_history_twice(self):
        self.commit.side_effect = [locked(), None]

        with patch.object(self.ctx, 'add_to_history') as add_to_history:
            self.ctx.try_add_to_history()

        add_to_history.assert_called_once()
        self.assertEqual(self.commit.call_count, 2)

    def test_lock_that_never_clears_is_logged_not_raised(self):
        with patch.object(
            self.ctx, 'add_to_history', side_effect=locked()
        ):
            self.ctx.try_add_to_history()


class TestSuccessWithLockedHistory(unittest.TestCase):
    def test_file_is_still_moved_when_history_cannot_be_written(self):
        patch('backend.features.post_processing.sleep').start()
        patch('backend.features.post_processing.commit').start()
        self.addCleanup(patch.stopall)

        processor = PostProcessor(make_download())
        ctx = processor.ctx
        for step in (
            'try_remove_from_queue', 'move_to_dest',
            'rename_with_proper_extension', 'add_file_to_database',
            'convert_file', 'set_file_properties', 'notify_download'
        ):
            setattr(ctx, step, MagicMock())
        ctx.add_to_history = MagicMock(side_effect=locked())

        processor.success()

        ctx.move_to_dest.assert_called_once()
        ctx.add_file_to_database.assert_called_once()


if __name__ == '__main__':
    unittest.main()
