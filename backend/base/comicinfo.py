# -*- coding: utf-8 -*-

"""
Reading and parsing ComicInfo.xml metadata from inside comic archives (CBZ/ZIP).
"""

import zipfile
from functools import lru_cache
from os.path import getmtime, splitext
from typing import Any, Dict, Tuple, Union
from xml.etree import ElementTree as ET

from backend.base.logging import LOGGER


def _parse_issue_number(raw: str) -> Union[float, None]:
    """Parse a simple numeric issue number from a ComicInfo Number field.

    Args:
        raw: The issue number string from ComicInfo.xml.

    Returns:
        The issue number as a float, or None if not parseable.
    """
    raw = raw.strip().replace('½', '.5').replace('¼', '.25')
    try:
        return float(raw)
    except (ValueError, TypeError):
        # Try stripping trailing letter suffix (e.g. '2b' -> '2')
        stripped = raw.rstrip('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ')
        if stripped:
            try:
                return float(stripped)
            except (ValueError, TypeError):
                pass
        return None


# ComicInfo.xml fields we care about for library import matching
COMICINFO_FIELDS = (
    "Series",
    "Number",
    "Volume",
    "Title",
    "Year",
    "Month",
    "Day",
    "Writer",
    "Publisher",
    "Imprint",
    "Genre",
    "Tags",
    "LanguageISO",
    "Summary",
    "Web",
    # ComicVine IDs — set by taggers like ComicRack, ComicTagger, Mylar
    "ComicVineIssue",
)


def read_comicinfo(filepath: str) -> Dict[str, str]:
    """Read ComicInfo.xml from inside a CBZ/ZIP archive.

    Results are cached in-memory (LRU, up to 4096 entries) to avoid
    re-opening the same archive when called multiple times for the
    same file (e.g. during scan + group resolution).

    Args:
        filepath: Path to a CBZ or ZIP file.

    Returns:
        A dictionary mapping lowercase field names to their string values.
        Returns an empty dict if the file is not a ZIP, has no ComicInfo.xml,
        or on any error.
    """
    # Delegate to the cached implementation; results are frozen as
    # tuples internally so that they're hashable for lru_cache,
    # then converted back to a mutable dict for callers.
    # Including mtime in the cache key ensures re-tagged files are re-read.
    try:
        mtime = getmtime(filepath)
    except OSError:
        mtime = 0.0
    frozen = _read_comicinfo_cached(filepath, mtime)
    return dict(frozen)


@lru_cache(maxsize=4096)
def _read_comicinfo_cached(
    filepath: str,
    _mtime: float = 0.0
) -> Tuple[Tuple[str, str], ...]:
    """Cached inner implementation of read_comicinfo.

    Returns an immutable tuple-of-tuples so lru_cache can store it.
    The ``_mtime`` parameter is not used in the function body but
    serves as a cache-busting key: when a file is re-tagged its
    mtime changes, producing a new cache key and forcing a re-read.
    """
    ext = splitext(filepath)[1].lower()
    if ext not in ('.cbz', '.zip'):
        return ()

    meta: Dict[str, str] = {}
    try:
        with zipfile.ZipFile(filepath, 'r') as zf:
            # Case-insensitive search for ComicInfo.xml
            # Use split('/') to check the exact filename, preventing
            # false matches like 'notcomicinfo.xml'.
            xml_name = next(
                (
                    n for n in zf.namelist()
                    if n.lower().split('/')[-1] == 'comicinfo.xml'
                    and not n.endswith('/')
                ),
                None
            )
            if not xml_name:
                return ()

            with zf.open(xml_name) as fp:
                root = ET.parse(fp).getroot()
                for el in root:
                    val = (el.text or '').strip()
                    if not val:
                        continue
                    meta[el.tag.lower()] = val

                # Alias: prefer Title, fallback to BookTitle
                if 'title' not in meta and 'booktitle' in meta:
                    meta['title'] = meta['booktitle']

    except (zipfile.BadZipFile, OSError, ET.ParseError) as e:
        LOGGER.debug('Failed to read ComicInfo.xml from %s: %s', filepath, e)
    except Exception as e:
        LOGGER.warning(
            'Unexpected error reading ComicInfo.xml from %s: %s', filepath, e
        )

    return tuple(meta.items())


def get_comicvine_issue_id(comicinfo: Dict[str, str]) -> Union[int, None]:
    """Extract the ComicVine issue ID from parsed ComicInfo metadata.

    The ComicVineIssue field may be a plain number or a full CV ID
    like "4000-12345". We extract just the numeric ID.

    Args:
        comicinfo: Metadata dict from read_comicinfo().

    Returns:
        The numeric CV issue ID, or None if not present/valid.
    """
    raw = comicinfo.get('comicvineissue', '').strip()
    if not raw:
        return None

    # Handle "4000-12345" format
    if '-' in raw:
        parts = raw.rsplit('-', 1)
        # Reject if the part after '-' is empty
        if not parts[-1].strip():
            return None
        raw = parts[-1]

    try:
        cv_id = int(raw)
        return cv_id if cv_id > 0 else None
    except (ValueError, TypeError):
        return None


def enhance_filename_data(
    file_data: Dict[str, Any],
    comicinfo: Dict[str, str]
) -> Dict[str, Any]:
    """Enhance filename-extracted data with ComicInfo.xml metadata.

    When ComicInfo.xml has a value, it takes precedence over the
    regex-parsed filename data for series, year, and volume_number.
    Issue number is also enhanced if present.

    Args:
        file_data: A FilenameData dict from extract_filename_data().
        comicinfo: Metadata dict from read_comicinfo().

    Returns:
        The enhanced file_data dict (modified in place and returned).
    """
    if not comicinfo:
        return file_data

    # Series name from ComicInfo is authoritative
    ci_series = comicinfo.get('series', '').strip()
    if ci_series:
        LOGGER.debug(
            'ComicInfo override series: "%s" -> "%s"',
            file_data.get('series'), ci_series
        )
        file_data['series'] = ci_series

    # Year
    ci_year = comicinfo.get('year', '').strip()
    if ci_year:
        try:
            file_data['year'] = int(ci_year)
        except (ValueError, TypeError):
            pass

    # Volume number
    ci_volume = comicinfo.get('volume', '').strip()
    if ci_volume:
        try:
            file_data['volume_number'] = int(ci_volume)
        except (ValueError, TypeError):
            pass

    # Issue number — only override when ComicInfo has one and filename doesn't,
    # or when the file was tagged as TPB just because no issue was found
    ci_number = comicinfo.get('number', '').strip()
    if ci_number:
        ci_issue = _parse_issue_number(ci_number)
        if ci_issue is not None:
            if (
                file_data.get('issue_number') is None
                or file_data.get('special_version') == 'tpb'
            ):
                file_data['issue_number'] = ci_issue
                # Clear the TPB assumption if we found an actual issue number
                if file_data.get('special_version') == 'tpb':
                    file_data['special_version'] = None

    return file_data
