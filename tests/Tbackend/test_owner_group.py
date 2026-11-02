import unittest
from os.path import join
from unittest.mock import patch

from backend.base.definitions import OSType
from backend.base.files import set_volume_folder_owner_group

ROOT = join('comics', '')
VOLUME = join('comics', 'Batman')


class SetVolumeFolderOwnerGroup(unittest.TestCase):
    def run_with_group(self, owner_group: str) -> list:
        with patch('backend.base.files.get_os_type',
                   return_value=OSType.LINUX), \
                patch('backend.base.files.list_folders', return_value=[]), \
                patch('backend.base.files.list_files', return_value=[]), \
                patch('backend.base.files.chown') as chown:
            set_volume_folder_owner_group(VOLUME, ROOT, owner_group)
        return [c.kwargs['group'] for c in chown.call_args_list]

    def test_numeric_group_is_passed_as_group_id(self):
        # shutil.chown looks a str up as a group name, so '100' must be an int
        self.assertEqual(self.run_with_group('100'), [100])

    def test_group_name_is_passed_unchanged(self):
        self.assertEqual(self.run_with_group('media'), ['media'])


if __name__ == '__main__':
    unittest.main()
