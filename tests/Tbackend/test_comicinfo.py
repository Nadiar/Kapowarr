import os
import tempfile
import unittest
import zipfile

from backend.base.comicinfo import read_comicvine_issue_id


class ReadComicVineIssueId(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def make_cbz(self, comicinfo, name='ComicInfo.xml', ext='.cbz') -> str:
        path = os.path.join(self.tmp.name, 'Batman 001' + ext)
        with zipfile.ZipFile(path, 'w') as z:
            z.writestr('001.jpg', b'page')
            if comicinfo is not None:
                z.writestr(name, comicinfo)
        return path

    def test_issue_id_is_read_from_a_comicvine_web_link(self):
        path = self.make_cbz(
            '<ComicInfo><Web>https://comicvine.gamespot.com/batman-1/'
            '4000-1159114/</Web></ComicInfo>'
        )
        self.assertEqual(read_comicvine_issue_id(path), 1159114)

    def test_issue_id_is_read_from_comictagger_notes(self):
        path = self.make_cbz(
            '<ComicInfo><Notes>Tagged with ComicTagger 1.6 using info from '
            'Comic Vine on 2024-01-01. [Issue ID 987654]</Notes></ComicInfo>'
        )
        self.assertEqual(read_comicvine_issue_id(path), 987654)

    def test_web_link_wins_over_notes(self):
        path = self.make_cbz(
            '<ComicInfo><Notes>[Issue ID 1]</Notes><Web>https://'
            'comicvine.gamespot.com/x/4000-2/</Web></ComicInfo>'
        )
        self.assertEqual(read_comicvine_issue_id(path), 2)

    def test_lowercase_comicinfo_name_is_found(self):
        path = self.make_cbz(
            '<ComicInfo><Web>https://comicvine.gamespot.com/x/4000-5/</Web>'
            '</ComicInfo>',
            name='comicinfo.xml'
        )
        self.assertEqual(read_comicvine_issue_id(path), 5)

    def test_link_to_another_site_gives_none(self):
        path = self.make_cbz(
            '<ComicInfo><Web>https://example.org/4000-5/</Web></ComicInfo>'
        )
        self.assertIsNone(read_comicvine_issue_id(path))

    def test_cbz_without_comicinfo_gives_none(self):
        self.assertIsNone(read_comicvine_issue_id(self.make_cbz(None)))

    def test_file_that_is_not_a_zip_gives_none(self):
        path = os.path.join(self.tmp.name, 'broken.cbz')
        with open(path, 'wb') as f:
            f.write(b'not a zip')
        self.assertIsNone(read_comicvine_issue_id(path))

    def test_missing_file_or_folder_gives_none(self):
        self.assertIsNone(read_comicvine_issue_id(
            os.path.join(self.tmp.name, 'missing.cbz')
        ))
        self.assertIsNone(read_comicvine_issue_id(self.tmp.name))

    def test_other_archive_formats_are_not_opened(self):
        path = self.make_cbz(
            '<ComicInfo><Web>https://comicvine.gamespot.com/x/4000-5/</Web>'
            '</ComicInfo>',
            ext='.cbr'
        )
        self.assertIsNone(read_comicvine_issue_id(path))


if __name__ == '__main__':
    unittest.main()
