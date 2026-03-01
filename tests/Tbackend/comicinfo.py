# -*- coding: utf-8 -*-

import io
import os
import tempfile
import unittest
import zipfile
from xml.etree import ElementTree as ET

from backend.base.comicinfo import (enhance_filename_data,
                                    get_comicvine_issue_id, read_comicinfo,
                                    _read_comicinfo_cached)


def _create_cbz_with_comicinfo(xml_content: str, filename='test.cbz') -> str:
    """Helper: create a temporary CBZ file containing a ComicInfo.xml."""
    tmp = tempfile.NamedTemporaryFile(
        suffix='.cbz', delete=False, prefix='kapowarr_test_'
    )
    tmp.close()
    with zipfile.ZipFile(tmp.name, 'w') as zf:
        zf.writestr('ComicInfo.xml', xml_content)
    return tmp.name


def _minimal_comicinfo(**kwargs) -> str:
    """Build a minimal ComicInfo.xml string from keyword arguments."""
    root = ET.Element('ComicInfo')
    for tag, value in kwargs.items():
        el = ET.SubElement(root, tag)
        el.text = str(value)
    return ET.tostring(root, encoding='unicode')


class TestReadComicinfo(unittest.TestCase):
    """Tests for read_comicinfo()."""

    def tearDown(self):
        # Clean up any temp files created during tests
        for attr in ('_tmpfiles',):
            for f in getattr(self, attr, []):
                try:
                    os.unlink(f)
                except OSError:
                    pass

    def _track(self, path):
        if not hasattr(self, '_tmpfiles'):
            self._tmpfiles = []
        self._tmpfiles.append(path)
        return path

    def test_reads_basic_fields(self):
        xml = _minimal_comicinfo(
            Series='Batman',
            Number='5',
            Year='2020',
            Volume='2'
        )
        path = self._track(_create_cbz_with_comicinfo(xml))
        result = read_comicinfo(path)

        self.assertEqual(result['series'], 'Batman')
        self.assertEqual(result['number'], '5')
        self.assertEqual(result['year'], '2020')
        self.assertEqual(result['volume'], '2')

    def test_keys_are_lowercase(self):
        xml = _minimal_comicinfo(Series='Test', ComicVineIssue='12345')
        path = self._track(_create_cbz_with_comicinfo(xml))
        result = read_comicinfo(path)

        self.assertIn('series', result)
        self.assertIn('comicvineissue', result)
        self.assertNotIn('Series', result)
        self.assertNotIn('ComicVineIssue', result)

    def test_skips_empty_values(self):
        xml = _minimal_comicinfo(Series='Batman', Title='')
        path = self._track(_create_cbz_with_comicinfo(xml))
        result = read_comicinfo(path)

        self.assertIn('series', result)
        self.assertNotIn('title', result)

    def test_non_zip_returns_empty(self):
        tmp = tempfile.NamedTemporaryFile(
            suffix='.cbr', delete=False, prefix='kapowarr_test_'
        )
        tmp.close()
        self._track(tmp.name)
        result = read_comicinfo(tmp.name)

        self.assertEqual(result, {})

    def test_zip_without_comicinfo_returns_empty(self):
        tmp = tempfile.NamedTemporaryFile(
            suffix='.cbz', delete=False, prefix='kapowarr_test_'
        )
        tmp.close()
        with zipfile.ZipFile(tmp.name, 'w') as zf:
            zf.writestr('page001.jpg', b'fake image data')
        self._track(tmp.name)

        result = read_comicinfo(tmp.name)
        self.assertEqual(result, {})

    def test_case_insensitive_xml_name(self):
        """ComicInfo.xml should be found regardless of case."""
        tmp = tempfile.NamedTemporaryFile(
            suffix='.cbz', delete=False, prefix='kapowarr_test_'
        )
        tmp.close()
        xml = _minimal_comicinfo(Series='Test')
        with zipfile.ZipFile(tmp.name, 'w') as zf:
            zf.writestr('COMICINFO.XML', xml)
        self._track(tmp.name)

        result = read_comicinfo(tmp.name)
        self.assertEqual(result['series'], 'Test')

    def test_comicinfo_in_subdirectory(self):
        """ComicInfo.xml nested inside a folder within the archive."""
        tmp = tempfile.NamedTemporaryFile(
            suffix='.cbz', delete=False, prefix='kapowarr_test_'
        )
        tmp.close()
        xml = _minimal_comicinfo(Series='Nested')
        with zipfile.ZipFile(tmp.name, 'w') as zf:
            zf.writestr('subfolder/ComicInfo.xml', xml)
        self._track(tmp.name)

        result = read_comicinfo(tmp.name)
        self.assertEqual(result['series'], 'Nested')

    def test_false_match_notcomicinfo_xml(self):
        """A file named 'notcomicinfo.xml' should NOT be matched."""
        tmp = tempfile.NamedTemporaryFile(
            suffix='.cbz', delete=False, prefix='kapowarr_test_'
        )
        tmp.close()
        xml = _minimal_comicinfo(Series='FalseMatch')
        with zipfile.ZipFile(tmp.name, 'w') as zf:
            zf.writestr('notcomicinfo.xml', xml)
        self._track(tmp.name)

        result = read_comicinfo(tmp.name)
        self.assertEqual(result, {})

    def test_bad_zip_returns_empty(self):
        tmp = tempfile.NamedTemporaryFile(
            suffix='.cbz', delete=False, prefix='kapowarr_test_'
        )
        tmp.write(b'this is not a zip file')
        tmp.close()
        self._track(tmp.name)

        result = read_comicinfo(tmp.name)
        self.assertEqual(result, {})

    def test_malformed_xml_returns_empty(self):
        tmp = tempfile.NamedTemporaryFile(
            suffix='.cbz', delete=False, prefix='kapowarr_test_'
        )
        tmp.close()
        with zipfile.ZipFile(tmp.name, 'w') as zf:
            zf.writestr('ComicInfo.xml', '<broken><xml')
        self._track(tmp.name)

        result = read_comicinfo(tmp.name)
        self.assertEqual(result, {})

    def test_nonexistent_file_returns_empty(self):
        result = read_comicinfo('/nonexistent/path/comic.cbz')
        self.assertEqual(result, {})

    def test_comicvine_issue_field(self):
        xml = _minimal_comicinfo(
            Series='Amazing Spider-Man',
            ComicVineIssue='4000-54321'
        )
        path = self._track(_create_cbz_with_comicinfo(xml))
        result = read_comicinfo(path)

        self.assertEqual(result['comicvineissue'], '4000-54321')

    def test_cache_avoids_double_read(self):
        """Second call for the same file should hit cache, not re-open ZIP."""
        xml = _minimal_comicinfo(Series='Cached')
        path = self._track(_create_cbz_with_comicinfo(xml))

        # Clear cache to get a clean baseline
        _read_comicinfo_cached.cache_clear()

        result1 = read_comicinfo(path)
        info = _read_comicinfo_cached.cache_info()
        self.assertEqual(info.misses, 1)
        self.assertEqual(info.hits, 0)

        result2 = read_comicinfo(path)
        info = _read_comicinfo_cached.cache_info()
        self.assertEqual(info.hits, 1)

        self.assertEqual(result1, result2)
        self.assertEqual(result1['series'], 'Cached')

        # Clean up
        _read_comicinfo_cached.cache_clear()


class TestGetComicvineIssueId(unittest.TestCase):
    """Tests for get_comicvine_issue_id()."""

    def test_plain_numeric_id(self):
        self.assertEqual(
            get_comicvine_issue_id({'comicvineissue': '12345'}),
            12345
        )

    def test_prefixed_id(self):
        """CV issue IDs from ComicRack are often '4000-12345'."""
        self.assertEqual(
            get_comicvine_issue_id({'comicvineissue': '4000-12345'}),
            12345
        )

    def test_id_with_whitespace(self):
        self.assertEqual(
            get_comicvine_issue_id({'comicvineissue': '  4000-99999  '}),
            99999
        )

    def test_empty_string(self):
        self.assertIsNone(
            get_comicvine_issue_id({'comicvineissue': ''})
        )

    def test_missing_key(self):
        self.assertIsNone(
            get_comicvine_issue_id({'series': 'Batman'})
        )

    def test_non_numeric(self):
        self.assertIsNone(
            get_comicvine_issue_id({'comicvineissue': 'abc'})
        )

    def test_zero_id(self):
        """Zero is not a valid ComicVine ID."""
        self.assertIsNone(
            get_comicvine_issue_id({'comicvineissue': '0'})
        )

    def test_negative_id(self):
        """A bare negative number should still parse the digits after '-'."""
        self.assertEqual(
            get_comicvine_issue_id({'comicvineissue': '-5'}),
            5
        )

    def test_empty_dict(self):
        self.assertIsNone(
            get_comicvine_issue_id({})
        )

    def test_prefixed_with_different_prefix(self):
        """Some taggers might use different prefixes."""
        self.assertEqual(
            get_comicvine_issue_id({'comicvineissue': '9999-67890'}),
            67890
        )


class TestEnhanceFilenameData(unittest.TestCase):
    """Tests for enhance_filename_data()."""

    def _base_file_data(self, **overrides):
        data = {
            'series': 'Unknown',
            'year': None,
            'volume_number': 1,
            'special_version': None,
            'issue_number': None,
            'annual': False
        }
        data.update(overrides)
        return data

    def test_overrides_series(self):
        fd = self._base_file_data(series='Bttmn')
        ci = {'series': 'Batman'}
        result = enhance_filename_data(fd, ci)

        self.assertEqual(result['series'], 'Batman')

    def test_overrides_year(self):
        fd = self._base_file_data(year=None)
        ci = {'year': '2020'}
        result = enhance_filename_data(fd, ci)

        self.assertEqual(result['year'], 2020)

    def test_overrides_volume_number(self):
        fd = self._base_file_data(volume_number=1)
        ci = {'volume': '3'}
        result = enhance_filename_data(fd, ci)

        self.assertEqual(result['volume_number'], 3)

    def test_overrides_issue_when_none(self):
        fd = self._base_file_data(issue_number=None)
        ci = {'number': '5'}
        result = enhance_filename_data(fd, ci)

        self.assertEqual(result['issue_number'], 5.0)

    def test_clears_tpb_when_issue_found(self):
        fd = self._base_file_data(
            special_version='tpb', issue_number=None
        )
        ci = {'number': '10'}
        result = enhance_filename_data(fd, ci)

        self.assertEqual(result['issue_number'], 10.0)
        self.assertIsNone(result['special_version'])

    def test_does_not_override_existing_issue(self):
        """When file already has an issue number and is not TPB, keep it."""
        fd = self._base_file_data(issue_number=3.0)
        ci = {'number': '5'}
        result = enhance_filename_data(fd, ci)

        self.assertEqual(result['issue_number'], 3.0)

    def test_empty_comicinfo_no_changes(self):
        fd = self._base_file_data(series='Iron Man', year=2020)
        result = enhance_filename_data(fd, {})

        self.assertEqual(result['series'], 'Iron Man')
        self.assertEqual(result['year'], 2020)

    def test_invalid_year_ignored(self):
        fd = self._base_file_data(year=2020)
        ci = {'year': 'not-a-year'}
        result = enhance_filename_data(fd, ci)

        self.assertEqual(result['year'], 2020)

    def test_invalid_volume_ignored(self):
        fd = self._base_file_data(volume_number=1)
        ci = {'volume': 'abc'}
        result = enhance_filename_data(fd, ci)

        self.assertEqual(result['volume_number'], 1)

    def test_modifies_in_place(self):
        fd = self._base_file_data(series='Old')
        ci = {'series': 'New'}
        result = enhance_filename_data(fd, ci)

        self.assertIs(result, fd)
        self.assertEqual(fd['series'], 'New')

    def test_all_overrides_together(self):
        fd = self._base_file_data(
            series='Bad Name',
            year=None,
            volume_number=1,
            issue_number=None,
            special_version='tpb'
        )
        ci = {
            'series': 'Amazing Spider-Man',
            'year': '2018',
            'volume': '5',
            'number': '1'
        }
        result = enhance_filename_data(fd, ci)

        self.assertEqual(result['series'], 'Amazing Spider-Man')
        self.assertEqual(result['year'], 2018)
        self.assertEqual(result['volume_number'], 5)
        self.assertEqual(result['issue_number'], 1.0)
        self.assertIsNone(result['special_version'])


if __name__ == '__main__':
    unittest.main()
