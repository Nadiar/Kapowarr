import sys
import types
import unittest
from os.path import join
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

if 'grp' not in sys.modules:
    sys.modules['grp'] = MagicMock()
if 'pwd' not in sys.modules:
    sys.modules['pwd'] = types.SimpleNamespace(
        getpwnam=lambda *_args, **_kwargs: None,
        getpwuid=lambda *_args, **_kwargs: None
    )

from backend.features.post_processing import PostProcessingContext


class NotifyDownloadFilePath(unittest.TestCase):
    def notified_path(self, files: list) -> str:
        service = MagicMock()
        volume = MagicMock()
        volume.vd = SimpleNamespace(
            id=1, title='Reign of X', year=2021, comicvine_id=137265,
            folder='/comics/Reign of X (2021)'
        )
        download = SimpleNamespace(
            volume_id=1, issue_id=None, files=files, source_name='getcomics'
        )
        with patch('backend.features.post_processing.Volume',
                   return_value=volume), \
                patch('backend.features.post_processing.get_db'), \
                patch('backend.features.notifications.NotificationService',
                      return_value=service):
            PostProcessingContext(download).notify_download()

        service.notify_download.assert_called_once()
        return service.notify_download.call_args.args[0].file_path

    def test_converted_file_is_reported_not_the_removed_original(self):
        with TemporaryDirectory() as folder:
            converted = join(folder, 'Reign of X (2021) 001.cbz')
            open(converted, 'wb').close()
            original = join(folder, 'Reign of X (2021) 001.cbr')

            self.assertEqual(
                self.notified_path([original, converted]), converted
            )

    def test_unconverted_file_is_reported(self):
        with TemporaryDirectory() as folder:
            downloaded = join(folder, 'Reign of X (2021) 004.cbz')
            open(downloaded, 'wb').close()

            self.assertEqual(self.notified_path([downloaded]), downloaded)


if __name__ == '__main__':
    unittest.main()
