import unittest
from os import makedirs, rmdir, scandir
from os.path import join
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backend.base.definitions import OSType
from backend.base.files import (list_files, set_volume_folder_owner_group,
                                set_volume_folder_permissions)

ROOT = join('comics', '')
VOLUME = join('comics', 'Reign of X (2021)')
# A parallel download's conversion removes its extraction folder while the
# volume folder is being processed
GONE = join(VOLUME, '.archive_extract_001', '001-000.jpg')
KEPT = join(VOLUME, 'Reign of X (2021) 004.cbz')


def vanished(path, *_args, **_kwargs):
    if path == GONE:
        raise FileNotFoundError(path)


class VanishedFilesAreSkipped(unittest.TestCase):
    def patched(self, name: str):
        return (
            patch('backend.base.files.get_os_type',
                  return_value=OSType.LINUX),
            patch('backend.base.files.list_folders',
                  return_value=[join(VOLUME, '.archive_extract_001')]),
            patch('backend.base.files.list_files', return_value=[GONE, KEPT]),
            patch(f'backend.base.files.{name}', side_effect=vanished),
        )

    def run_patched(self, name: str, function, *args) -> list:
        os_type, folders, files, call = self.patched(name)
        with os_type, folders, files, call as mock:
            function(*args)
        return [c.args[0] for c in mock.call_args_list]

    def test_permissions_continue_past_a_vanished_file(self):
        paths = self.run_patched(
            'chmod', set_volume_folder_permissions, VOLUME, ROOT, '775'
        )
        self.assertIn(KEPT, paths)

    def test_ownership_continues_past_a_vanished_file(self):
        paths = self.run_patched(
            'chown', set_volume_folder_owner_group, VOLUME, ROOT, '100'
        )
        self.assertIn(KEPT, paths)


class ListFilesSkipsExtractionFolders(unittest.TestCase):
    def test_extraction_folder_removed_while_listing(self):
        with TemporaryDirectory() as volume:
            extraction = join(volume, '.archive_extract_001')
            makedirs(extraction)
            open(join(volume, 'Reign of X (2021) 004.cbz'), 'w').close()

            def scandir_after_conversion(path):
                # The conversion finishes between listing the volume folder
                # and descending into its extraction folder
                entries = list(scandir(path))
                if path == volume:
                    rmdir(extraction)
                return iter(entries)

            with patch('backend.base.files.scandir',
                       side_effect=scandir_after_conversion):
                files = list_files(volume, ['.cbz'])

        self.assertEqual(len(files), 1)


if __name__ == '__main__':
    unittest.main()
