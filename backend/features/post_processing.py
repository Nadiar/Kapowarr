# -*- coding: utf-8 -*-

from __future__ import annotations

from os.path import basename, exists, isfile, join, splitext
from sqlite3 import OperationalError
from time import sleep, time
from typing import TYPE_CHECKING, Dict

from backend.base.definitions import (BlocklistReason,
                                      DownloadState, FileConstants)
from backend.base.files import (copy_directory, delete_file_folder,
                                rename_file, set_detected_extension)
from backend.base.logging import LOGGER
from backend.implementations.blocklist import add_to_blocklist
from backend.implementations.conversion import mass_convert
from backend.implementations.converters import extract_files_from_folder
from backend.implementations.file_matching import scan_files
from backend.implementations.file_processing import mass_process_files
from backend.implementations.naming import mass_rename
from backend.implementations.volumes import Volume
from backend.internals.db import commit, get_db
from backend.internals.db_models import FilesDB
from backend.internals.settings import Settings

if TYPE_CHECKING:
    from backend.base.definitions import Download


class PostProcessingContext:
    def __init__(self, download: Download) -> None:
        self.download = download
        self.original_files = download.files
        return

    # region Ctx Database
    def remove_from_queue(self) -> None:
        """Delete the download from the queue in the database. Retries a few
        times when the database is locked."""
        retries = 5
        for attempt in range(1, retries + 1):
            try:
                get_db().execute(
                    "DELETE FROM download_queue WHERE id = ?",
                    (self.download.id,)
                )
                return

            except OperationalError as exc:
                if (
                    'database is locked' not in str(exc).lower()
                    or attempt == retries
                ):
                    raise

                LOGGER.warning(
                    'Database locked while removing download %s from queue '
                    '(attempt %s/%s); retrying',
                    self.download.id, attempt, retries
                )
                sleep(min(0.2 * attempt, 1.0))
        return

    def try_remove_from_queue(self) -> None:
        """Like `remove_from_queue()`, but never raises. Used at the start of
        successful post-processing so that a locked database doesn't abort
        moving the files. The download handler removes the entry again
        after post-processing as a fallback."""
        try:
            self.remove_from_queue()
        except Exception:
            LOGGER.exception(
                'Failed to remove download %s from queue table; '
                'continuing with post-processing',
                self.download.id
            )
        return

    def notify_download(self) -> None:
        "Dispatch a notification for a completed download. Never raises."
        download = self.download
        try:
            from backend.features.notifications import (DownloadEvent,
                                                        NotificationService)
            from backend.implementations.volumes import Issue

            vd = Volume(download.volume_id).vd

            issue_number = ''
            issue_title = ''
            issue_comicvine_id = None
            if download.issue_id is not None:
                try:
                    issue_data = Issue(download.issue_id).get_data()
                    issue_number = issue_data.issue_number or ''
                    issue_title = issue_data.title or ''
                    issue_comicvine_id = issue_data.comicvine_id or None
                except Exception:
                    pass

            # add_to_history already ran, so count >= 2 means upgrade
            try:
                count = get_db().execute(
                    """
                    SELECT COUNT(*) FROM download_history
                    WHERE volume_id = ?
                      AND (issue_id = ? OR (issue_id IS NULL AND ? IS NULL))
                      AND success = 1;
                    """,
                    (download.volume_id, download.issue_id, download.issue_id)
                ).fetchone()[0]
                is_upgrade = count >= 2
            except Exception:
                is_upgrade = False

            # Converting adds the new file after the original it removed
            files = [f for f in download.files if exists(f)] or download.files

            event = DownloadEvent(
                volume_id=vd.id,
                volume_title=vd.title,
                volume_year=vd.year or 0,
                volume_comicvine_id=vd.comicvine_id,
                volume_path=vd.folder or '',
                issue_id=download.issue_id,
                issue_comicvine_id=issue_comicvine_id,
                issue_number=issue_number,
                issue_title=issue_title,
                file_path=files[0] if files else '',
                download_source=download.source_name,
                is_upgrade=is_upgrade
            )

            NotificationService().notify_download(event)

        except Exception:
            LOGGER.exception(
                'Failed to build or dispatch download notification '
                'for download %s', getattr(download, 'id', '?')
            )
        return

    def add_to_history(self) -> None:
        "Add the download to history in the database"
        get_db().execute(
            """
            INSERT INTO download_history(
                web_link, web_title, web_sub_title,
                file_title,
                volume_id, issue_id,
                source, downloaded_at, success
            ) VALUES (
                :web_link, :web_title, :web_sub_title,
                :file_title,
                :volume_id, :issue_id,
                :source, :downloaded_at, :success
            );
            """,
            {
                'web_link': self.download.web_link,
                'web_title': self.download.web_title,
                'web_sub_title': self.download.web_sub_title,
                'file_title': self.download.title,
                'volume_id': self.download.volume_id,
                'issue_id': self.download.issue_id,
                'source': self.download.source_name,
                'downloaded_at': round(time()),
                'success': self.download.state != DownloadState.FAILED_STATE
            }
        )
        return

    def add_file_to_database(self) -> None:
        "Register files in database and match to a volume/issue"
        scan_files(
            self.download.volume_id,
            filepath_filter=self.download.files,
            update_websocket=True
        )
        return

    # region Ctx Blocklist
    def add_dl_to_blocklist(self) -> None:
        "Add the download to the blocklist in the database"
        add_to_blocklist(
            self.download.web_link,
            self.download.web_title,
            self.download.web_sub_title,
            self.download.download_link,
            self.download.download_service,
            self.download.volume_id,
            self.download.issue_id,
            BlocklistReason.LINK_BROKEN
        )
        return

    # region Ctx Moving
    def move_to_dest(self) -> None:
        "Move file/fold from download folder to final destination"
        if not exists(self.download.files[0]):
            return

        folder = Volume(self.download.volume_id).vd.folder
        extension = splitext(self.download.files[0])[1].lower()
        if extension not in FileConstants.SCANNABLE_EXTENSIONS:
            extension = ''

        file_dest = join(
            folder,
            self.download.filename_body + extension
        )
        LOGGER.debug(
            f'Moving download to final destination: {self.download}, Dest: {file_dest}'
        )

        if exists(file_dest):
            LOGGER.warning(
                f'The file/folder {file_dest} already exists; replacing with downloaded file'
            )
            delete_file_folder(file_dest)

        rename_file(self.download.files[0], file_dest)
        self.download.files = [file_dest]
        return

    def move_torrent_to_dest(self) -> None:
        """
        Move folder downloaded using torrent from download folder to
        final destination, extract files, scan them, rename them.
        """
        if not exists(self.download.files[0]):
            return

        self.move_to_dest()

        self.download.files = extract_files_from_folder(
            self.download.files[0],
            self.download.volume_id
        )

        if not self.download.files:
            return

        scan_files(
            self.download.volume_id,
            filepath_filter=self.download.files,
            update_websocket=True
        )

        rename_files = Settings().sv.rename_downloaded_files
        if rename_files:
            self.download.files = mass_rename(
                self.download.volume_id,
                filepath_filter=self.download.files,
                process_individual_files=False
            )

        return

    def copy_file_torrent(self) -> None:
        "Copy downloaded files to dest. Change download.file to copy."
        if not exists(self.download.files[0]):
            return

        folder = Volume(self.download.volume_id).vd.folder
        file_dest = join(folder, basename(self.download.files[0]))
        LOGGER.debug(
            f'Copying download to final destination: {self.download}, Dest: {file_dest}'
        )

        if exists(file_dest):
            LOGGER.warning(
                f'The file/folder {file_dest} already exists; replacing with downloaded file'
            )
            delete_file_folder(file_dest)

        copy_directory(self.download.files[0], file_dest)

        self.download.files = extract_files_from_folder(
            file_dest,
            self.download.volume_id
        )

        if not self.download.files:
            return

        scan_files(
            self.download.volume_id,
            filepath_filter=self.download.files,
            update_websocket=True
        )

        rename_files = Settings().sv.rename_downloaded_files
        if rename_files:
            self.download.files = mass_rename(
                self.download.volume_id,
                filepath_filter=self.download.files,
                process_individual_files=False
            )

        return

    # region Ctx Extras
    def delete_file(self) -> None:
        "Delete file from download folder"
        for f in self.download.files:
            delete_file_folder(f)
        return

    def rename_with_proper_extension(self) -> None:
        """
        Rename a file with the proper extension based on mimetype. Rescan files
        in case a rename is done.
        """
        renamed_files: Dict[str, str] = {}
        for idx, file in enumerate(self.download.files):
            if not isfile(file):
                continue

            new_file = set_detected_extension(file)
            if new_file != file:
                rename_file(file, new_file)
                self.download.files[idx] = new_file
                renamed_files[file] = new_file

        if renamed_files:
            FilesDB.update_filepaths(renamed_files)

        return

    def convert_file(self) -> None:
        "Convert a file into a different format based on settings"
        if not Settings().sv.convert:
            return

        self.download.files += mass_convert(
            self.download.volume_id,
            self.download.issue_id,
            filepath_filter=self.download.files,
            update_websocket_files=True,
            process_individual_files=False
        )
        return

    def set_file_properties(self) -> None:
        "Process the file to set ownership, permissions and file date"
        mass_process_files(
            self.download.volume_id,
            self.download.issue_id
        )
        return


# region Post-Processors
class PostProcessor:
    def __init__(self, download: Download) -> None:
        self.download = download
        self.ctx = PostProcessingContext(download)
        return

    def success(self) -> None:
        LOGGER.info(
            f'Postprocessing of successful download: {self.download.id}'
        )
        self.ctx.try_remove_from_queue()
        self.ctx.add_to_history()
        commit()
        self.ctx.move_to_dest()
        self.ctx.rename_with_proper_extension()
        commit()
        self.ctx.add_file_to_database()
        self.ctx.convert_file()
        self.ctx.set_file_properties()
        commit()
        self.ctx.notify_download()
        return

    def seeding(self) -> None:
        return

    def canceled(self) -> None:
        LOGGER.info(f'Postprocessing of canceled download: {self.download.id}')
        self.ctx.delete_file()
        self.ctx.remove_from_queue()
        return

    def shutdown(self) -> None:
        LOGGER.info(f'Postprocessing of shut down download: {self.download.id}')
        self.ctx.delete_file()
        return

    def failed(self) -> None:
        LOGGER.info(f'Postprocessing of failed download: {self.download.id}')
        self.ctx.remove_from_queue()
        self.ctx.add_to_history()
        commit()
        self.ctx.delete_file()
        return

    def perm_failed(self) -> None:
        LOGGER.info(
            f'Postprocessing of permanently failed download: {self.download.id}'
        )
        self.ctx.remove_from_queue()
        self.ctx.add_to_history()
        self.ctx.add_dl_to_blocklist()
        commit()
        self.ctx.delete_file()
        return


class PostProcessorTorrentsComplete(PostProcessor):
    def success(self) -> None:
        LOGGER.info(
            f'Postprocessing of successful download: {self.download.id}'
        )
        self.ctx.try_remove_from_queue()
        self.ctx.add_to_history()
        commit()
        self.ctx.move_torrent_to_dest()
        commit()
        self.ctx.convert_file()
        self.ctx.set_file_properties()
        commit()
        self.ctx.notify_download()
        return


class PostProcessorTorrentsCopy(PostProcessor):
    def success(self) -> None:
        LOGGER.info(
            f'Postprocessing of successful download: {self.download.id}'
        )
        self.ctx.remove_from_queue()
        commit()
        self.ctx.delete_file()
        return

    def seeding(self) -> None:
        LOGGER.info(f'Postprocessing of seeding download: {self.download.id}')
        self.ctx.add_to_history()
        commit()
        self.ctx.copy_file_torrent()
        commit()
        self.ctx.convert_file()
        self.ctx.set_file_properties()
        commit()
        self.ctx.notify_download()
        self.download.files = self.ctx.original_files
        commit()
        return
