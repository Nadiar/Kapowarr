# -*- coding: utf-8 -*-

"""
Reading the ComicVine issue a comic file is tagged with
"""

from re import IGNORECASE, compile
from typing import Union
from zipfile import BadZipFile, ZipFile

COMICINFO_FILENAME = 'comicinfo.xml'
# ComicInfo.xml is small; anything bigger isn't worth reading for one tag
COMICINFO_MAX_SIZE = 1024 * 1024

web_issue_regex = compile(
    r'<Web>[^<]*comicvine\.gamespot\.com/[^<]*\b4000-(\d+)', IGNORECASE
)
# ComicTagger writes "[Issue ID 12345]" in the Notes
notes_issue_regex = compile(r'<Notes>[^<]*\[Issue ID (\d+)\]', IGNORECASE)


def read_comicvine_issue_id(filepath: str) -> Union[int, None]:
    """Get the ComicVine issue ID from the ComicInfo.xml inside a CBZ file,
    from its Web link or ComicTagger's note. Only those two tags are needed,
    so they're found with a regex instead of parsing the XML.

    Args:
        filepath (str): The path to the file.

    Returns:
        Union[int, None]: The issue ID, or `None` if the file isn't a CBZ,
            can't be read or has no ComicVine issue in its ComicInfo.xml.
    """
    if not filepath.lower().endswith('.cbz'):
        return None

    try:
        with ZipFile(filepath) as archive:
            info = next(
                (
                    i for i in archive.infolist()
                    if i.filename.lower() == COMICINFO_FILENAME
                ),
                None
            )
            if info is None or info.file_size > COMICINFO_MAX_SIZE:
                return None
            content = archive.read(info).decode('utf-8', errors='replace')

    except (BadZipFile, OSError, KeyError):
        return None

    match = web_issue_regex.search(content) or notes_issue_regex.search(content)
    return int(match.group(1)) if match else None
