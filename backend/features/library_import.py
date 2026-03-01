# -*- coding: utf-8 -*-

from asyncio import run
from glob import glob
from itertools import chain
from os.path import abspath, basename, dirname, isfile, join, splitext
from time import perf_counter
from typing import Any, Dict, List, Union

from backend.base.comicinfo import (enhance_filename_data,
                                    get_comicvine_issue_id,
                                    read_comicinfo)
from backend.base.custom_exceptions import (CVRateLimitReached,
                                            InvalidKeyValue,
                                            VolumeAlreadyAdded)
from backend.base.definitions import (CVFileMapping, FileConstants,
                                      FilenameData, MonitorScheme,
                                      SpecialVersion)
from backend.base.file_extraction import extract_filename_data
from backend.base.files import (change_basefolder, common_folder,
                                delete_empty_parent_folders,
                                folder_is_inside_folder,
                                iter_files, rename_file)
from backend.base.helpers import force_suffix
from backend.base.logging import LOGGER
from backend.implementations.comicvine import ComicVine
from backend.implementations.file_matching import scan_files
from backend.implementations.local_comicvine import (
    is_local_cv_available,
    local_filenames_to_cvs,
    local_resolve_groups_by_issue_ids)
from backend.implementations.naming import mass_rename
from backend.implementations.root_folders import RootFolders
from backend.implementations.volumes import Library
from backend.internals.db import commit, get_db
from backend.internals.db_models import FilesDB


def create_groups(
    files: Dict[str, FilenameData]
) -> Dict[int, Dict[str, FilenameData]]:
    """Group files together that seem like they are for the same volume.

    Args:
        files (Dict[str, FilenameData]): The files in the form of a mapping from
            their filename to their filename data.

    Returns:
        Dict[int, Dict[str, FilenameData]]: A mapping from the group number
            (which doesn't cary any meaning except for identifying the group)
            to the files that are in the group, where the files are in the form
            of a mapping from the filename to their filename data.
    """
    # Use a hashable key for O(1) group lookup instead of O(n) linear scan
    key_to_group: Dict[tuple, int] = {}
    groups: Dict[int, Dict[str, FilenameData]] = {}
    next_group = 1

    for file, file_data in files.items():
        match_data = file_data.copy()
        del match_data['issue_number']  # type: ignore

        # Convert dict to a frozen (hashable) representation
        group_key = tuple(sorted(match_data.items()))

        group_idx = key_to_group.get(group_key)
        if group_idx is not None:
            groups[group_idx][file] = file_data
        else:
            key_to_group[group_key] = next_group
            groups[next_group] = {file: file_data}
            next_group += 1

    LOGGER.debug('File groupings: %s', groups)
    return groups


def _no_match() -> Dict[str, Any]:
    """Return a null-match dict for an unresolved group."""
    return {'id': None, 'title': None, 'issue_count': None, 'link': None}


def _resolve_by_cv_ids(
    groups_with_cv_ids: Dict[int, int],
    only_english: bool,
    use_local: bool,
    local_only: bool
) -> Dict[int, Dict[str, Any]]:
    """Resolve file groups that have ComicVine issue IDs.

    Tries the local CV database first (if available), then falls back
    to the ComicVine API unless ``local_only`` is set.

    Args:
        groups_with_cv_ids: Mapping of group number to CV issue ID.
        only_english: Only match english releases.
        use_local: Whether the local CV database is available.
        local_only: When True, never fall back to the API.

    Returns:
        Mapping of group number to match dict.
    """
    if not groups_with_cv_ids:
        return {}

    result: Dict[int, Dict[str, Any]] = {}

    if use_local:
        local_results = local_resolve_groups_by_issue_ids(
            groups_with_cv_ids,
            only_english=only_english
        )
        api_fallback_ids: Dict[int, int] = {}
        for gn, match in local_results.items():
            if match['id'] is not None:
                result[gn] = match
            else:
                api_fallback_ids[gn] = groups_with_cv_ids[gn]

        if api_fallback_ids and not local_only:
            LOGGER.info(
                'Local DB resolved %d groups by issue ID, '
                '%d need API fallback',
                len(result), len(api_fallback_ids)
            )
            cv = ComicVine()
            result.update(
                run(cv.resolve_groups_by_issue_ids(
                    api_fallback_ids,
                    only_english=only_english
                ))
            )
        elif api_fallback_ids:
            LOGGER.info(
                'Local DB resolved %d groups by issue ID, '
                '%d unresolved (local-only, no API fallback)',
                len(result), len(api_fallback_ids)
            )
            result.update(local_results)
        else:
            LOGGER.info(
                'Local DB resolved all %d groups by issue ID',
                len(groups_with_cv_ids)
            )

    elif not local_only:
        cv = ComicVine()
        result.update(
            run(cv.resolve_groups_by_issue_ids(
                groups_with_cv_ids,
                only_english=only_english
            ))
        )
        LOGGER.info(
            'Resolved %d group(s) via ComicInfo.xml ComicVine IDs (API)',
            len(groups_with_cv_ids)
        )

    else:
        LOGGER.info(
            'Local-only mode but no local DB — '
            '%d groups with CV IDs unresolved',
            len(groups_with_cv_ids)
        )
        for gn in groups_with_cv_ids:
            result[gn] = _no_match()

    return result


def _resolve_by_title(
    groups_needing_search: Dict[int, Dict[str, FilenameData]],
    only_english: bool,
    use_local: bool,
    local_only: bool
) -> Dict[int, Dict[str, Any]]:
    """Resolve file groups via title-based search.

    Tries the local CV database first (if available), then falls back
    to the ComicVine API unless ``local_only`` is set.

    Args:
        groups_needing_search: Mapping of group number to files dict.
        only_english: Only match english releases.
        use_local: Whether the local CV database is available.
        local_only: When True, never fall back to the API.

    Returns:
        Mapping of group number to match dict.
    """
    if not groups_needing_search:
        return {}

    result: Dict[int, Dict[str, Any]] = {}

    if use_local:
        local_results = local_filenames_to_cvs(
            groups_needing_search,
            only_english=only_english
        )
        result.update(local_results)

        matched_count = sum(
            1 for m in local_results.values() if m['id'] is not None
        )
        LOGGER.info(
            'Local DB matched %d of %d title groups '
            '(%d unmatched, no API fallback)',
            matched_count, len(groups_needing_search),
            len(groups_needing_search) - matched_count
        )

    elif not local_only:
        LOGGER.info(
            'No local CV DB, searching %d groups via API',
            len(groups_needing_search)
        )
        cv = ComicVine()
        result.update(
            run(cv.filenames_to_cvs(
                groups_needing_search,
                only_english=only_english
            ))
        )

    else:
        LOGGER.info(
            'Local-only mode but no local DB — %d groups unmatched',
            len(groups_needing_search)
        )
        for gn in groups_needing_search:
            result[gn] = _no_match()

    return result


def propose_library_import(
    folder_filter: Union[str, None] = None,
    limit: int = 20,
    limit_parent_folder: bool = False,
    only_english: bool = True,
    local_only: bool = False
) -> List[Dict[str, Any]]:
    """Get list of unimported files and their suggestion for a matching volume
    on CV.

    Args:
        folder_filter (Union[str, None], optional): Only scan the folders that
            match the given value. Can either be a folder or a glob pattern.
            Defaults to None.

        limit (int, optional): The max amount of folders to scan.
            Defaults to 20.

        limit_parent_folder (bool, optional): Base the folder limit on parent
            folder, not folder. Useful if each issue has their own sub-folder.
            Defaults to False.

        only_english (bool, optional): Only match with english releases.
            Defaults to True.

        local_only (bool, optional): Only use the local CV cache for matching.
            When True, no ComicVine API calls are made even as fallback.
            Defaults to False.

    Raises:
        InvalidKeyValue: The file filter matches to folders outside
            the root folders.

    Returns:
        List[Dict[str, Any]]: The list of files and their matches.
    """
    LOGGER.info('Loading library import')
    t_start = perf_counter()

    # Get all files in all root folders (with filter applied if given)
    root_folders = {
        abspath(r)
        for r in RootFolders().get_folder_list()
    }

    if folder_filter:
        scan_folders = set((
            f
            for f in glob(folder_filter, recursive=True)
            if not isfile(f) # Glob pattern could match to a file
        ))
        for f in scan_folders:
            if not any(folder_is_inside_folder(r, f) for r in root_folders):
                # Folder is not inside a root folder
                raise InvalidKeyValue('folder_filter', folder_filter)
    else:
        scan_folders = root_folders.copy()

    # Use iter_files (generator) instead of list_files (full list) so the
    # scan loop's folder-limit break can short-circuit the directory walk.
    # On NAS storage this avoids walking 30k+ files when we only need ~20
    # folders worth of results.
    all_files = chain.from_iterable(
        iter_files(f, FileConstants.CONTENT_EXTENSIONS)
        for f in scan_folders
    )

    # Get imported files
    imported_files = {
        f["filepath"]
        for f in FilesDB.fetch()
    }

    # Filter away imported files and apply limit
    folders = set()
    image_folders = set()
    unimported_files: Dict[str, FilenameData] = {}
    for f in all_files:
        if f in imported_files:
            continue

        d = abspath(dirname(f))
        if d in root_folders:
            # File directly in root folder is not allowed
            continue

        file_data = extract_filename_data(f, prefer_folder_year=True)

        if (
            f.endswith(FileConstants.IMAGE_EXTENSIONS)
            and file_data["special_version"] != SpecialVersion.COVER
        ):
            if d in image_folders:
                continue
            image_folders.add(d)
            d, f = dirname(d), d

        folders.add(
            dirname(d)
            if limit_parent_folder else
            d
        )

        if limit > 0 and len(folders) > limit:
            break

        unimported_files[f] = file_data

    # Sort by filename
    t_scan = perf_counter()
    LOGGER.info(
        'Scan loop done: %d files, %d folders [%.1fs]',
        len(unimported_files), len(folders), t_scan - t_start
    )
    unimported_files = {
        f: d
        for f, d in sorted(
            unimported_files.items(),
            key=lambda e: basename(e[0])
        )
    }

    # Find a match for the groups on CV
    group_to_files = create_groups(unimported_files)
    t_group = perf_counter()
    LOGGER.info(
        'Grouping done: %d groups [%.1fs]',
        len(group_to_files), t_group - t_scan
    )

    # Enhance grouped files with ComicInfo.xml metadata and extract CV IDs.
    # When local_only is set, skip ComicInfo reading to avoid opening every
    # archive over NAS — title-based matching from filenames is sufficient.
    groups_with_cv_ids: Dict[int, int] = {}
    if not local_only:
        for group_number, files in group_to_files.items():
            for filepath in files:
                comicinfo = read_comicinfo(filepath)
                if comicinfo:
                    enhance_filename_data(files[filepath], comicinfo)

                cv_issue_id = get_comicvine_issue_id(comicinfo)
                if cv_issue_id is not None and group_number not in groups_with_cv_ids:
                    groups_with_cv_ids[group_number] = cv_issue_id

        LOGGER.info(
            'ComicInfo enhancement done: %d groups with CV IDs',
            len(groups_with_cv_ids)
        )
    else:
        LOGGER.info(
            'Skipping ComicInfo reading in local-only mode (%d files)',
            sum(len(f) for f in group_to_files.values())
        )

    # Split groups: those with CV IDs get resolved directly,
    # the rest go through title-based search
    groups_needing_search = {
        gn: files
        for gn, files in group_to_files.items()
        if gn not in groups_with_cv_ids
    }

    use_local = is_local_cv_available()
    if use_local:
        LOGGER.info('Local CV database available — using local-first lookup')
    if local_only:
        LOGGER.info('Local-only mode enabled — no API calls will be made')

    group_to_cv = _resolve_by_cv_ids(
        groups_with_cv_ids, only_english, use_local, local_only
    )
    group_to_cv.update(_resolve_by_title(
        groups_needing_search, only_english, use_local, local_only
    ))

    t_match = perf_counter()
    LOGGER.info(
        'All CV matching done [%.1fs, total %.1fs]',
        t_match - t_group, t_match - t_start
    )

    # Build result
    result = [
        {
            'filepath': file,
            'file_title': (
                splitext(basename(file))[0]
                if isfile(file) else
                basename(file)
            ),
            'cv': group_to_cv[group_number],
            'group_number': group_number
        }
        for group_number, files in group_to_files.items()
        for file in files
    ]

    return result


def import_library(
    matches: List[CVFileMapping],
    rename_files: bool = False
) -> None:
    """Add volume to library and import linked files.

    Args:
        matches (List[CVFileMapping]): List of file mappings.

        rename_files (bool, optional): Trigger a rename after importing files.
            Defaults to False.
    """
    LOGGER.info('Starting library import')

    cvid_to_filepath: Dict[int, List[str]] = {}
    for m in matches:
        cvid_to_filepath.setdefault(m['id'], []).append(m['filepath'])
    LOGGER.debug('id_to_filepath: %s', cvid_to_filepath)

    root_folders = RootFolders().get_all()
    for cv_id, files in cvid_to_filepath.items():
        # Find root folder that media is in
        for root_folder in root_folders:
            if folder_is_inside_folder(root_folder.folder, files[0]):
                break
        else:
            continue

        lcf = common_folder(files)
        if not rename_files and force_suffix(lcf) == root_folder.folder:
            # Back out. Volume folder will be equal to root folder.
            continue

        volume_already_added = False

        try:
            volume_id = Library.add(
                comicvine_id=cv_id,
                root_folder_id=root_folder.id,
                monitored=True,
                monitor_scheme=MonitorScheme.ALL,
                monitor_new_issues=True,
                volume_folder=lcf if not rename_files else None
            )
            commit()

        except VolumeAlreadyAdded as e:
            # The volume that the files are for is already in the library, while
            # the files aren't matched to it. This has two reasons:
            # 1. The files are for an existing volume but in a common folder.
            #    Like some users that download files externally and put them all
            #    in a to-be-imported folder. Their idea is that they use LI to
            #    move and rename the files to the proper volume folder because
            #    they don't want to move it themselves, even though the volume
            #    is already in their library.
            # 2. The files matched to the wrong volume, and the wrong volume
            #    happens to already be in the library.
            # The propability of bullet 1 happening is quite low, so moving the
            # files into the volume folder is worth more to users of bullet 1
            # than it is a bad thing for the users that experience bullet 2. So
            # solution is to move the file to the volume folder of the match,
            # and rename if that was chosen.
            volume_already_added = True
            volume_id = e.volume_id

        except CVRateLimitReached:
            # Hit rate limit so can't add any volumes anymore
            break

        if rename_files or volume_already_added:
            # Move files not already in the volume folder into the volume folder
            vf = Library.get_volume(volume_id).vd.folder

            if volume_already_added or folder_is_inside_folder(vf, lcf):
                file_changes = {
                    f: (
                        join(vf, basename(f))
                        if not folder_is_inside_folder(vf, f) else
                        f
                    )
                    for f in files
                }

            else:
                file_changes = change_basefolder(files, lcf, vf)

            for old, new in file_changes.items():
                if old != new:
                    rename_file(old, new)
                    delete_empty_parent_folders(
                        dirname(old), root_folder.folder
                    )

            files = list(file_changes.values())

        scan_files(volume_id, filepath_filter=files)

        if rename_files:
            # Rename the filenames themselves
            mass_rename(volume_id, filepath_filter=files)

    return


def import_library_local(
    matches: List[CVFileMapping]
) -> int:
    """Add volumes using local CV data only (no API calls).

    Groups files by CV ID, fetches volume metadata from the local
    CV database, and adds each new volume via ``Library.add_from_local()``.
    Already-added volumes are silently skipped.

    Args:
        matches: List of CVFileMapping dicts with 'id' and 'filepath'.

    Returns:
        int: Number of volumes successfully added.
    """
    from backend.implementations.local_comicvine import fetch_volume_local

    LOGGER.info('Starting local-only library import')

    cvid_to_filepath: Dict[int, List[str]] = {}
    for m in matches:
        cvid_to_filepath.setdefault(m['id'], []).append(m['filepath'])

    # Pre-filter out CV IDs already in the library
    existing_cv_ids = set(
        r[0] for r in get_db().execute(
            "SELECT comicvine_id FROM volumes;"
        ).fetchall()
    )
    new_cv_ids = {
        cv_id: files
        for cv_id, files in cvid_to_filepath.items()
        if cv_id not in existing_cv_ids
    }
    skipped = len(cvid_to_filepath) - len(new_cv_ids)
    if skipped:
        LOGGER.info(
            'Local import: skipping %d already-added volumes', skipped
        )

    if not new_cv_ids:
        LOGGER.info('Local import: nothing new to add')
        return 0

    root_folders = RootFolders().get_all()
    added = 0

    for cv_id, files in new_cv_ids.items():
        # Find root folder
        for root_folder in root_folders:
            if folder_is_inside_folder(root_folder.folder, files[0]):
                break
        else:
            continue

        lcf = common_folder(files)
        if force_suffix(lcf) == root_folder.folder:
            continue

        # Fetch full volume + issues from local DB
        vd = fetch_volume_local(cv_id)
        if vd is None:
            LOGGER.debug('Local import: CV %d not in local DB, skipping', cv_id)
            continue

        try:
            volume_id = Library.add_from_local(
                volume_data=vd,
                root_folder_id=root_folder.id,
                monitored=True,
                monitor_scheme=MonitorScheme.ALL,
                monitor_new_issues=True,
                volume_folder=lcf
            )
            commit()
            added += 1

        except VolumeAlreadyAdded:
            # Race condition — another task added it between our check and now
            continue

        try:
            scan_files(volume_id, filepath_filter=files)
        except Exception as e:
            LOGGER.warning(
                'scan_files failed for volume %d: %s', volume_id, e
            )

    LOGGER.info('Local import complete: %d volumes added', added)
    return added
