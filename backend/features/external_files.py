# -*- coding: utf-8 -*-

"""
Fork-only: files whose location is managed by an external application.

An external manager (e.g. comicmanager) can move a downloaded file out of its
volume folder. `repoint_file()` records the new location and marks the file
as external. Kapowarr then keeps the issue bound to it, and must not rename,
convert, move or delete it, since the file is not Kapowarr's to touch.
"""

from os import stat
from os.path import isabs, isfile
from typing import Iterable, List, Set, Union

from backend.base.custom_exceptions import FileNotFound, InvalidKeyValue
from backend.base.definitions import FileData
from backend.base.logging import LOGGER
from backend.internals.db import get_db


def is_external(file_id: int) -> bool:
    "Whether the file is managed by an external application"
    return get_db().execute(
        "SELECT 1 FROM external_files WHERE file_id = ?;",
        (file_id,)
    ).fetchone() is not None


def skip_external(files: Iterable[FileData]) -> List[FileData]:
    "Filter out the files managed by an external application"
    files = list(files)
    if not files:
        return files

    external_ids = {
        r[0] for r in get_db().execute(
            "SELECT file_id FROM external_files;"
        ).fetchall()
    }
    return [f for f in files if f["id"] not in external_ids]


def present_external_file_ids(volume_id: int) -> Set[int]:
    """The external files bound to issues of the volume that still exist on
    disk. A scan of the volume folder can't find them, but they're not lost."""
    rows = get_db().execute(
        """
        SELECT DISTINCT f.id, f.filepath
        FROM files f
        INNER JOIN external_files ef ON ef.file_id = f.id
        INNER JOIN issues_files if ON if.file_id = f.id
        INNER JOIN issues i ON i.id = if.issue_id
        WHERE i.volume_id = ?;
        """,
        (volume_id,)
    ).fetchall()
    return {r[0] for r in rows if isfile(r[1])}


def repoint_file(file_id: int, new_path: Union[str, bytes]) -> None:
    """Point a file at a new location and mark it as external. Its issue
    bindings are kept. Can be called again when the external application
    moves the file once more.

    Args:
        file_id (int): The ID of the file.
        new_path (str): Absolute path of the file's new location.

    Raises:
        FileNotFound: The file ID is unknown, or nothing exists at the path.
        InvalidKeyValue: The path is not absolute, or is already used by
            another file.
    """
    new_path = str(new_path)
    cursor = get_db()

    if cursor.execute(
        "SELECT 1 FROM files WHERE id = ?;", (file_id,)
    ).fetchone() is None:
        raise FileNotFound(file_id)

    if not isabs(new_path):
        raise InvalidKeyValue("filepath", new_path)

    if not isfile(new_path):
        raise FileNotFound(new_path)

    if cursor.execute(
        "SELECT 1 FROM files WHERE filepath = ? AND id != ?;",
        (new_path, file_id)
    ).fetchone() is not None:
        raise InvalidKeyValue("filepath", new_path)

    cursor.execute(
        "UPDATE files SET filepath = ?, size = ? WHERE id = ?;",
        (new_path, stat(new_path).st_size, file_id)
    )
    cursor.execute(
        "INSERT OR IGNORE INTO external_files(file_id) VALUES (?);",
        (file_id,)
    )
    LOGGER.info("File %d is now managed externally at %s", file_id, new_path)
    return


def forget_external(file_id: int) -> None:
    "Stop treating a file as externally managed. Its path is kept."
    get_db().execute(
        "DELETE FROM external_files WHERE file_id = ?;",
        (file_id,)
    )
    return
