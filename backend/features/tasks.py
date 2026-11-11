# -*- coding: utf-8 -*-

from __future__ import annotations

from abc import abstractmethod
from asyncio import run
from datetime import date, timedelta
from time import sleep, time
from typing import (TYPE_CHECKING, Any, Dict, Iterable,
                    List, Set, Tuple, Type, TypeVar, Union)

from backend.base.custom_exceptions import (CredentialInvalid, InvalidKeyValue,
                                            MetadataSourceNotWorking,
                                            TaskNotFound)
from backend.base.definitions import (BlocklistReasonID, FilenameData,
                                      QueuedTaskData, SearchResultData, Task)
from backend.base.helpers import Singleton, force_range, get_schedules_next_run
from backend.base.logging import LOGGER
from backend.features.download_queue import DownloadHandler
from backend.features.search_discover import discover_downloads
from backend.features.search_full import AutoSearchOutcome, auto_search_outcome
from backend.implementations.comicvine import ComicVine
from backend.implementations.conversion import mass_convert
from backend.implementations.file_matching import scan_files
from backend.implementations.naming import mass_rename
from backend.implementations.volumes import (Volume, determine_special_version,
                                             refresh_and_scan,
                                             update_volume_metadata,
                                             upsert_issues)
from backend.internals.db import commit, get_db
from backend.internals.db_backup_import import backup_database
from backend.internals.db_models_search_stats import (IssueSearchOutcome,
                                                      get_search_all_volumes,
                                                      record_issue_searches,
                                                      record_search_hit,
                                                      record_search_miss)
from backend.internals.server import (Server, TaskAddedEvent, TaskEndedEvent,
                                      TaskStatusEvent, WebSocket)

if TYPE_CHECKING:
    from threading import Timer


# region Task Handler
TASK_INTERVALS = {
    # Note: If there are tasks that should be run at the same time,
    #   but per se after each other, put them in that order in the dict.
    'update_all': '0 * * * *', # every hour at minute 0
    'backup_db': '0 0 * * 1', # every Monday at 00:00
    'rss_sync': '0,30 * * * *', # every half hour

    # Fork tasks
    'search_recent_fresh': '15 * * * *', # every hour at minute 15
    'refresh_calendar': '0 1 * * *', # daily at 01:00
    'sync_issues': '15 2 * * *', # daily at 02:15
    'scan_files': '45 2 * * *', # daily at 02:45
    'special_version_refresh': '15 3 * * *', # daily at 03:15
    'refresh_metadata': '30 3 * * 0', # Sundays at 03:30
    'search_all': '0 4 * * 0', # Sundays at 04:00
    'search_recent_week': '0 5 * * *', # daily at 05:00
    'search_recent_stale': '0 6 */3 * *', # every 3rd day at 06:00
    'health_check': '0 7 * * *' # daily at 07:00
}

LEGACY_TASK_NAMES = ('search_recent',)
"Task names that may still be in the database from older versions"


TaskType = TypeVar(
    "TaskType",
    bound=Task
)


class TaskHandler(metaclass=Singleton):
    tasks: Dict[str, Type[Task]] = {}

    queue: List[QueuedTaskData] = []
    task_interval_waiter: Union[Timer, None] = None

    @classmethod
    def register_task(cls, identifier: str):
        """Register a task.

        ```
        @TaskHandler.register_task("my_task")
        class MyTask(Task):
            ...
        ```

        Args:
            identifier (str): The string identifier for the task.

        Raises:
            RuntimeError: A task for the given identifier is already registered.
        """
        def wrapper(action: Type[TaskType]) -> Type[TaskType]:
            if identifier in cls.tasks:
                raise RuntimeError(
                    f"Task with {identifier=} registered multiple times"
                )
            action.action = identifier
            cls.tasks[identifier] = action
            return action
        return wrapper

    @classmethod
    def get_task_class(cls, identifier: str) -> Type[Task]:
        """Get a task implementation based on its identifier.

        Args:
            identifier (str): The identifer of the class.

        Raises:
            TaskNotFound: No task implementation found with the given identifier.

        Returns:
            Type[Task]: The task implementation.
        """
        try:
            return cls.tasks[identifier]
        except KeyError:
            raise TaskNotFound(identifier)

    def __run_task(self, task: Task) -> None:
        """Run a task.

        Args:
            task (Task): The task to run.
        """
        LOGGER.debug(f'Running task {task.display_title}')

        start_time = round(time())
        socket = WebSocket()
        try:
            results = task.run()
            cursor = get_db()

            # Note in history
            cursor.execute(
                """
                INSERT INTO task_history(
                    task_name, display_title, run_at, duration_seconds
                ) VALUES (?, ?, ?, ?);
                """,
                (
                    task.action, task.display_title,
                    round(time()), round(time()) - start_time
                )
            )

            if not task.stop:
                if isinstance(task, DownloadTask) and results:
                    DownloadHandler().add_multiple(
                        (*result, False) for result in results
                    )

                LOGGER.info(f'Finished task {task.display_title}')

        except Exception:
            LOGGER.exception(
                'An error occured while trying to run a task: ')
            task.message = 'AN ERROR OCCURED'
            socket.emit(TaskStatusEvent(task.message))
            sleep(1.5)

        finally:
            if not task.stop:
                socket.emit(TaskEndedEvent(task))
                # Remove our own entry (not necessarily the first one, as
                # a preempting task may be in front of a paused one)
                for entry in self.queue:
                    if entry['task'] is task:
                        self.queue.remove(entry)
                        break
                self._process_queue()

        return

    def _process_queue(self) -> None:
        """
        Handle the queue. In the case that there is something in the queue and
        it isn't already running, start the task. This can safely be called
        multiple times while a task is going or while there is nothing in the queue.
        Also resumes a paused task when it reaches the front of the queue.
        """
        if not self.queue:
            return

        first_entry = self.queue[0]
        if first_entry['status'] == 'paused':
            first_entry['status'] = 'running'
            first_entry['task']._ensure_yield_event().set()

        elif first_entry['status'] == 'queued':
            first_entry['status'] = 'running'
            first_entry['thread'].start()

        return

    def add(self, task: Task) -> int:
        """Add a task to the queue.

        Args:
            task (Task): The task to add to the queue.

        Returns:
            int: The ID of the entry in the queue.
        """
        LOGGER.debug(f'Adding task to queue: {task.display_title}')
        id = max((e['id'] for e in self.queue), default=0) + 1
        task_data: QueuedTaskData = {
            'id': id,
            'task': task,
            'thread': Server().get_db_thread(
                target=self.__run_task,
                name=f"TaskThread-{id}",
                args=(task,)
            ),
            'status': 'queued'
        }

        if (
            self.queue
            and self.queue[0]['status'] == 'running'
            and task.priority < self.queue[0]['task'].priority
        ):
            # Preempt the running lower-priority task. It pauses at its next
            # `check_yield()` and resumes once it's at the front again.
            running_entry = self.queue[0]
            LOGGER.info(
                'Preempting %s (pri %d) for %s (pri %d)',
                running_entry['task'].display_title,
                running_entry['task'].priority,
                task.display_title,
                task.priority
            )
            running_entry['status'] = 'paused'
            running_entry['task']._ensure_yield_event().clear()
            self.queue.insert(0, task_data)

        else:
            # Insert before any paused task so queued tasks drain before the
            # paused task resumes.
            insert_idx = len(self.queue)
            for i, entry in enumerate(self.queue):
                if entry['status'] == 'paused':
                    insert_idx = i
                    break
            self.queue.insert(insert_idx, task_data)

        LOGGER.info(f'Added task: {task.display_title} ({id})')
        WebSocket().emit(TaskAddedEvent(task))
        self._process_queue()
        return id

    @staticmethod
    def task_for_volume_running(volume_id: int) -> bool:
        """Whether or not there is a task in the queue that targets the volume.

        Args:
            volume_id (int): The volume ID to check for.

        Returns:
            bool: Whether or not a task is in the queue targeting the volume.
        """
        return any(
            t
            for t in TaskHandler.queue
            if (
                isinstance(t['task'], LibraryTask)
                or t['task'].volume_id == volume_id
            )
        )

    def __check_intervals(self) -> None:
        "Check if any interval task needs to be run and add to queue if so"
        LOGGER.debug('Checking task intervals')
        current_time = time()

        cursor = get_db()
        interval_tasks = cursor.execute(
            "SELECT task_name, schedule, next_run FROM task_intervals;"
        ).fetchall()
        LOGGER.debug(f'Task intervals: {list(map(dict, interval_tasks))}')

        # Prevent duplicate queueing of the same interval task
        queued_actions = {t['task'].action for t in self.queue}

        for task in interval_tasks:
            if task['next_run'] <= current_time:
                # Update next_run from cron schedule, regardless of whether
                # the task gets queued (prevents piling up missed runs)
                next_run = get_schedules_next_run(task["schedule"])
                cursor.execute(
                    "UPDATE task_intervals SET next_run = ? WHERE task_name = ?;",
                    (next_run, task['task_name']))

                if task['task_name'] in queued_actions:
                    LOGGER.info(
                        'Skipping %s, already in queue', task['task_name']
                    )
                    continue

                TaskClass = self.tasks.get(task['task_name'])
                if TaskClass is None:
                    LOGGER.warning(
                        'Skipping unknown interval task: %s',
                        task['task_name']
                    )
                    continue

                # Add task to queue
                if TaskClass is UpdateAll:
                    inst = TaskClass(allow_skipping=True)
                else:
                    inst = TaskClass()
                self.add(inst)
                queued_actions.add(task['task_name'])

        self.handle_intervals()
        return

    def handle_intervals(self) -> None:
        "Find next time an interval task needs to be run"
        next_run: Union[int, None] = get_db().execute(
            "SELECT MIN(next_run) FROM task_intervals"
        ).fetchone()[0]
        if next_run is None:
            next_run = round(time()) + 60

        wait_time = max(next_run - round(time()) + 1, 1)
        LOGGER.debug(f'Next interval task is in {wait_time} seconds')

        if self.task_interval_waiter:
            self.task_interval_waiter.cancel()

        self.task_interval_waiter = Server().get_db_timer_thread(
            interval=wait_time,
            target=self.__check_intervals,
            name="TaskIntervalThread"
        )
        self.task_interval_waiter.start()
        return

    def update_task_schedule(self, task_identifier: str, schedule: str) -> None:
        """Update the cron schedule for a background task.

        Args:
            task_identifier (str): The identifier of the task to change it for.
            schedule (str): The cron schedule to set.

        Raises:
            TaskNotFound: No task found with the given identifier.
            InvalidKeyValue: Invalid cron schedule.
        """
        # Validate task exists
        self.get_task_class(task_identifier)

        # Validate schedule string
        try:
            next_run = get_schedules_next_run(schedule)
        except ValueError:
            raise InvalidKeyValue('schedule', schedule)

        get_db().execute(
            """
            UPDATE task_intervals
            SET schedule = ?, next_run = ?
            WHERE task_name = ?;
            """,
            (schedule, next_run, task_identifier)
        )

        self.handle_intervals()
        return

    def stop_handle(self) -> None:
        "Stop the task handler"
        LOGGER.debug('Stopping task thread')

        if self.task_interval_waiter:
            self.task_interval_waiter.cancel()

        # Stop all tasks. Resume paused ones so they can observe the stop
        # flag and exit cleanly.
        for entry in self.queue:
            entry['task'].stop = True
            if entry['status'] == 'paused':
                entry['task']._ensure_yield_event().set()

        for entry in self.queue:
            if entry['status'] in ('running', 'paused'):
                entry['thread'].join()

        return

    def __format_entry(self, task: QueuedTaskData) -> Dict[str, Any]:
        """Format a queue entry for API response.

        Args:
            task (QueuedTaskData): The queue entry.

        Returns:
            Dict[str, Any]: The formatted queue entry.
        """
        return {
            'id': task['id'],
            'action': task['task'].action,
            'display_title': task['task'].display_title,
            'running': task['status'] == 'running',
            'status': task['status'],
            'message': task['task'].message,
            'volume_id': task['task'].volume_id,
            'issue_id': task['task'].issue_id
        }

    def get_all(self) -> List[Dict[str, Any]]:
        """Get all tasks in the queue.

        Returns:
            List[Dict[str, Any]]: A list with all tasks in the queue.
        """
        return [self.__format_entry(t) for t in self.queue]

    def get_one(self, task_id: int) -> Dict[str, Any]:
        """Get one task from the queue based on its ID.

        Args:
            task_id (int): The ID of the task to get from the queue.

        Raises:
            TaskNotFound: The ID doesn't match with any task in the queue.

        Returns:
            Dict[str, Any]: The info of the task in the queue.
        """
        return self.__format_entry(self.__get_raw_entry(task_id))

    def __get_raw_entry(self, task_id: int) -> QueuedTaskData:
        """Get the raw entry from the queue based on its ID.

        Args:
            task_id (int): The ID of the task to get from the queue.

        Raises:
            TaskNotFound: The ID doesn't match with any task in the queue.

        Returns:
            QueuedTaskData: The raw entry of the task in the queue.
        """
        for entry in self.queue:
            if entry['id'] == task_id:
                return entry
        raise TaskNotFound(task_id)

    def remove(self, task_id: int) -> None:
        """Remove a task from the queue, including the currently running or a
        paused task.

        Args:
            task_id (int): The ID of the task to delete from the queue.

        Raises:
            TaskNotFound: The id doesn't map to any task in the queue.
        """
        # Raises TaskNotFound if the id isn't found
        task = self.__get_raw_entry(task_id)
        was_active = task['status'] in ('running', 'paused')

        # Signal the task to stop at the next safe checkpoint
        task['task'].stop = True
        if task['status'] == 'paused':
            task['task']._ensure_yield_event().set()

        if was_active:
            # __run_task skips queue cleanup when task.stop is True,
            # so it's handled here.
            task['thread'].join()

        try:
            self.queue.remove(task)
        except ValueError:
            # Finished naturally in the meantime
            return

        LOGGER.info(f'Removed task: {task["task"].display_title} ({task_id})')
        WebSocket().emit(TaskEndedEvent(task['task']))

        if was_active:
            self._process_queue()
        return

    def get_task_planning(self) -> List[Dict[str, Any]]:
        """Get the planning of each interval task (schdule, next run and last run).

        Returns:
            List[Dict[str, Any]]: List of interval tasks and their planning.
        """
        tasks = get_db().execute(
            """
            SELECT
                i.task_name, schedule, next_run, run_at AS last_run
            FROM task_intervals i
            LEFT JOIN (
                SELECT
                    task_name,
                    MAX(run_at) AS run_at
                FROM task_history
                GROUP BY task_name
            ) h
            ON i.task_name = h.task_name
            ORDER BY i.task_name;
            """
        ).fetchalldict()

        for t in tasks:
            TaskClass = self.tasks.get(t['task_name'])
            if TaskClass is None:
                t['display_name'] = t['task_name'].replace('_', ' ').title()
                t['description'] = ''
            else:
                t['display_name'] = TaskClass.display_title
                t['description'] = TaskClass.description

        return tasks


def insert_task_intervals() -> None:
    """Fill the database table for task intervals, and remove rows of tasks
    that no longer exist"""
    cursor = get_db()
    cursor.executemany(
        "DELETE FROM task_intervals WHERE task_name = ?;",
        ((n,) for n in LEGACY_TASK_NAMES)
    )
    cursor.executemany(
        """
        INSERT OR IGNORE INTO task_intervals
        VALUES (?, ?, ?);
        """,
        (
            (task_name, schedule, get_schedules_next_run(schedule))
            for task_name, schedule in TASK_INTERVALS.items()
        )
    )
    return


# region History
def get_task_history(offset: int = 0) -> List[dict]:
    """Get the task history in blocks of 50.

    Args:
        offset (int, optional): The offset of the list. The higher the number,
            the deeper into history you go.
            Defaults to 0.

    Returns:
        List[dict]: The history entries.
    """
    result = get_db().execute(
        """
        SELECT
            task_name, display_title, run_at, duration_seconds
        FROM task_history
        ORDER BY run_at DESC
        LIMIT 50
        OFFSET ?;
        """,
        (offset * 50,)
    ).fetchalldict()
    return result


def delete_task_history() -> None:
    "Delete the complete task history"
    LOGGER.info(f'Deleting task history')
    get_db().execute("DELETE FROM task_history;")
    return


# region Task types
class LibraryTask(Task):
    """
    Tasks that inherit from this class signify that they don't work
    on one specific volume or issue but on all of them
    """


class DownloadTask(Task):
    """
    Tasks that inherit from this class signify that they return downloads
    """

    @abstractmethod
    def run(self) -> List[Tuple[str, int, FilenameData, int, Union[int, None]]]:
        ...


# region Issue tasks
@TaskHandler.register_task('auto_search_issue')
class AutoSearchIssue(DownloadTask):
    "Do an automatic search for an issue"

    stop = False
    message = ''
    display_title = 'Auto Search'
    priority = 1

    @property
    def volume_id(self) -> int:
        return self._volume_id

    @property
    def issue_id(self) -> int:
        return self._issue_id

    def __init__(self, volume_id: int, issue_id: int) -> None:
        """Create the task

        Args:
            volume_id (int): The ID of the volume in which the issue is.
            issue_id (int): The ID of the issue to search for.
        """
        self._volume_id = volume_id
        self._issue_id = issue_id
        return

    def run(self):
        volume = Volume(self._volume_id)
        volume_title = volume.vd.title
        issue_number = volume.get_issue(self._issue_id).get_data().issue_number
        self.message = f'Searching for {volume_title} #{issue_number}'
        WebSocket().emit(TaskStatusEvent(self.message))

        _search_and_queue(self, self._volume_id, self._issue_id)
        return []


@TaskHandler.register_task('mass_rename_issue')
class MassRenameIssue(Task):
    "Trigger a mass rename for an issue"

    stop = False
    message = ''
    display_title = 'Mass Rename'
    priority = 1

    @property
    def volume_id(self) -> int:
        return self._volume_id

    @property
    def issue_id(self) -> int:
        return self._issue_id

    def __init__(
        self,
        volume_id: int,
        issue_id: int,
        filepath_filter: List[str] = []
    ) -> None:
        """Create the task.

        Args:
            volume_id (int): The ID of the volume for which to perform the task.
            issue_id (int): The ID of the issue for which to perform the task.
            filepath_filter (List[str], optional): Only rename files in this
                list.
                Defaults to [].
        """
        self._volume_id = volume_id
        self._issue_id = issue_id
        self.filepath_filter = filepath_filter
        return

    def run(self) -> None:
        volume = Volume(self._volume_id)
        volume_title = volume.vd.title
        issue_number = volume.get_issue(self._issue_id).get_data().issue_number
        self.message = f'Renaming files for {volume_title} #{issue_number}'
        WebSocket().emit(TaskStatusEvent(self.message))

        mass_rename(
            self._volume_id,
            self._issue_id,
            filepath_filter=self.filepath_filter,
            update_websocket=True
        )

        return


@TaskHandler.register_task('mass_convert_issue')
class MassConvertIssue(Task):
    "Trigger a mass convert for an issue"

    stop = False
    message = ''
    display_title = 'Mass Convert'
    priority = 1

    @property
    def volume_id(self) -> int:
        return self._volume_id

    @property
    def issue_id(self) -> int:
        return self._issue_id

    def __init__(
        self,
        volume_id: int,
        issue_id: int,
        filepath_filter: List[str] = []
    ) -> None:
        """Create the task

        Args:
            volume_id (int): The ID of the volume for which to perform the task.
            issue_id (int): The ID of the issue for which to perform the task.
            filepath_filter (List[str], optional): Only rename files in this
                list.
                Defaults to [].
        """
        self._volume_id = volume_id
        self._issue_id = issue_id
        self.filepath_filter = filepath_filter
        return

    def run(self) -> None:
        volume = Volume(self._volume_id)
        volume_title = volume.vd.title
        issue_number = volume.get_issue(self._issue_id).get_data().issue_number
        self.message = f'Converting files for {volume_title} #{issue_number}'
        WebSocket().emit(TaskStatusEvent(self.message))

        mass_convert(
            self._volume_id,
            self._issue_id,
            filepath_filter=self.filepath_filter,
            update_websocket_progress=True,
            update_websocket_files=True
        )

        return


# region Volume tasks
@TaskHandler.register_task('auto_search')
class AutoSearchVolume(DownloadTask):
    "Do an automatic search for a volume"

    stop = False
    message = ''
    display_title = 'Auto Search'
    priority = 1

    @property
    def volume_id(self) -> int:
        return self._volume_id

    @property
    def issue_id(self) -> None:
        return None

    def __init__(self, volume_id: int) -> None:
        """Create the task.

        Args:
            volume_id (int): The ID of the volume to search for.
        """
        self._volume_id = volume_id
        return

    def run(self):
        volume_title = Volume(self._volume_id).vd.title
        self.message = f'Searching for {volume_title}'
        WebSocket().emit(TaskStatusEvent(self.message))

        _search_and_queue(self, self._volume_id)
        return []


@TaskHandler.register_task('refresh_and_scan')
class RefreshAndScanVolume(Task):
    "Trigger a refresh and scan for a volume"

    stop = False
    message = ''
    display_title = 'Refresh And Scan'
    priority = 2

    @property
    def volume_id(self) -> int:
        return self._volume_id

    @property
    def issue_id(self) -> None:
        return None

    def __init__(self, volume_id: int) -> None:
        """Create the task.

        Args:
            volume_id (int): The ID of the volume for which to refresh and scan.
        """
        self._volume_id = volume_id
        return

    def run(self) -> None:
        volume_title = Volume(self._volume_id).vd.title
        self.message = f'Updating info on {volume_title}'
        WebSocket().emit(TaskStatusEvent(self.message))

        try:
            refresh_and_scan(self._volume_id, update_websocket=True)
        except (CredentialInvalid, MetadataSourceNotWorking):
            pass

        return


@TaskHandler.register_task('mass_rename')
class MassRenameVolume(Task):
    "Trigger a mass rename for a volume"

    stop = False
    message = ''
    display_title = 'Mass Rename'
    priority = 1

    @property
    def volume_id(self) -> int:
        return self._volume_id

    @property
    def issue_id(self) -> None:
        return None

    def __init__(
        self,
        volume_id: int,
        filepath_filter: List[str] = []
    ) -> None:
        """Create the task

        Args:
            volume_id (int): The ID of the volume for which to perform the task.
            filepath_filter (List[str], optional): Only rename files in this
                list.
                Defaults to [].
        """
        self._volume_id = volume_id
        self.filepath_filter = filepath_filter
        return

    def run(self) -> None:
        volume_title = Volume(self._volume_id).vd.title
        self.message = f'Renaming files for {volume_title}'
        WebSocket().emit(TaskStatusEvent(self.message))

        mass_rename(
            self._volume_id,
            filepath_filter=self.filepath_filter,
            update_websocket=True
        )

        return


@TaskHandler.register_task('mass_convert')
class MassConvertVolume(Task):
    "Trigger a mass convert for a volume"

    stop = False
    message = ''
    display_title = 'Mass Convert'
    priority = 1

    @property
    def volume_id(self) -> int:
        return self._volume_id

    @property
    def issue_id(self) -> None:
        return None

    def __init__(
        self,
        volume_id: int,
        filepath_filter: List[str] = []
    ) -> None:
        """Create the task

        Args:
            volume_id (int): The ID of the volume for which to perform the task.
            filepath_filter (List[str], optional): Only convert files in this
                list.
                Defaults to [].
        """
        self._volume_id = volume_id
        self.filepath_filter = filepath_filter
        return

    def run(self) -> None:
        volume_title = Volume(self._volume_id).vd.title
        self.message = f'Converting files for {volume_title}'
        WebSocket().emit(TaskStatusEvent(self.message))

        mass_convert(
            self._volume_id,
            filepath_filter=self.filepath_filter,
            update_websocket_progress=True,
            update_websocket_files=True
        )

        return


# region Library tasks
@TaskHandler.register_task('update_all')
class UpdateAll(LibraryTask):
    "Trigger a refresh and scan for each volume in the library"

    stop = False
    message = ''
    display_title = 'Update All'
    priority = 4
    description = (
        'Refreshes metadata and scans files for volumes that have not been'
        ' updated in the last 24 hours or whose issue count changed.'
    )

    @property
    def volume_id(self) -> None:
        return None

    @property
    def issue_id(self) -> None:
        return None

    def __init__(self, allow_skipping: bool = False) -> None:
        """Create the task.

        Args:
            allow_skipping (bool, optional): Skip volumes that have been updated
                in the last 24 hours.
                Defaults to False.
        """
        self.allow_skipping = allow_skipping
        return

    def run(self) -> None:
        self.message = f'Updating info on all volumes'
        WebSocket().emit(TaskStatusEvent(self.message))

        try:
            refresh_and_scan(
                update_websocket=True,
                allow_skipping=self.allow_skipping
            )

        except (CredentialInvalid, MetadataSourceNotWorking):
            pass

        return


# region System tasks
@TaskHandler.register_task("backup_db")
class BackupDatabase(Task):
    "Create a backup of the database"

    stop = False
    message = ''
    display_title = 'Database Backup'
    priority = 3
    description = (
        'Creates a backup of the database.'
    )

    @property
    def volume_id(self) -> None:
        return None

    @property
    def issue_id(self) -> None:
        return None

    def __init__(self) -> None:
        return

    def run(self):
        self.message = 'Creating database backup'
        WebSocket().emit(TaskStatusEvent(self.message))

        backup_database()
        return


@TaskHandler.register_task('rss_sync')
class RssSync(DownloadTask):
    "Do an RSS sync"

    stop = False
    message = ''
    display_title = 'RSS Sync'
    priority = 3
    description = (
        'Checks all indexers for new releases and downloads matches.'
    )

    @property
    def volume_id(self) -> None:
        return None

    @property
    def issue_id(self) -> None:
        return None

    def __init__(self) -> None:
        return

    def run(self):
        self.message = 'Performing RSS Sync'
        WebSocket().emit(TaskStatusEvent(self.message))

        results_per_volume = discover_downloads()
        downloads: List[Tuple[
            str, int, FilenameData, int, Union[int, None]
        ]] = []
        for volume_id, results in results_per_volume.items():
            for result in results:
                title_data: FilenameData = {
                    "series": result["series"],
                    "year": result["year"],
                    "volume_number": result["volume_number"],
                    "special_version": result["special_version"],
                    "issue_number": result["issue_number"],
                    "annual": result["annual"]
                }
                downloads.append((
                    result['link'], result["indexer_id"],
                    title_data,
                    volume_id, None
                ))

        return downloads


# region Fork helpers
def _results_to_downloads(
    results: Iterable[SearchResultData],
    volume_id: int,
    issue_id: Union[int, None]
) -> List[Tuple[str, int, FilenameData, int, Union[int, None]]]:
    """Convert search results into the download tuples that `DownloadTask`s
    return.

    Args:
        results (Iterable[SearchResultData]): The search results.
        volume_id (int): The ID of the volume the downloads are for.
        issue_id (Union[int, None]): The ID of the issue the downloads are for,
            if any.

    Returns:
        List[Tuple[str, int, FilenameData, int, Union[int, None]]]: The
            download tuples.
    """
    downloads: List[Tuple[str, int, FilenameData, int, Union[int, None]]] = []
    for result in results:
        title_data: FilenameData = {
            "series": result["series"],
            "year": result["year"],
            "volume_number": result["volume_number"],
            "special_version": result["special_version"],
            "issue_number": result["issue_number"],
            "annual": result["annual"]
        }
        downloads.append((
            result["link"], result["indexer_id"],
            title_data,
            volume_id, issue_id
        ))
    return downloads


def _search_and_queue(
    task: Task,
    volume_id: int,
    issue_id: Union[int, None] = None
) -> int:
    """Search for the open issues of a volume, or for one issue, and queue
    the chosen downloads right away, so a task that is interrupted later
    keeps what it found. Logs the outcome per issue and for the volume.

    Args:
        task (Task): The calling task. Yields to higher-priority tasks
            between downloads and stops when asked to.
        volume_id (int): The ID of the volume to search for.
        issue_id (Union[int, None], optional): The ID of the issue to search
            for instead of the whole volume.
            Defaults to None.

    Returns:
        int: The number of downloads that were queued.
    """
    outcome = auto_search_outcome(volume_id, issue_id)

    handler = DownloadHandler()
    queued: List[
        Tuple[str, int, FilenameData, int, Union[int, None], bool]
    ] = []
    for download in _results_to_downloads(
        outcome.results, volume_id, issue_id
    ):
        task.check_yield()
        if task.stop:
            break
        queued += handler.add_multiple([(*download, False)])

    _record_search_outcome(
        volume_id, outcome, [download[2] for download in queued]
    )
    return len(queued)


def _covers_issue(
    title_datas: Iterable[FilenameData],
    calculated_issue_number: float
) -> bool:
    """Whether any of the releases contains the issue. A special version
    (e.g. a TPB) or a release without issue number covers every issue.
    """
    for title_data in title_datas:
        if (
            title_data["special_version"]
            or title_data["issue_number"] is None
        ):
            return True

        start, end = force_range(title_data["issue_number"])
        if start <= calculated_issue_number <= end:
            return True
    return False


def _record_search_outcome(
    volume_id: int,
    outcome: AutoSearchOutcome,
    queued: List[FilenameData]
) -> None:
    """Log how the search went for each searched issue and for the volume.
    A search that failed is neither a hit nor a miss.

    Args:
        volume_id (int): The ID of the searched volume.
        outcome (AutoSearchOutcome): The outcome of the search.
        queued (List[FilenameData]): The releases that were queued.
    """
    found: List[FilenameData] = list(outcome.results) # type: ignore
    issue_outcomes: Dict[int, IssueSearchOutcome] = {}
    for searched_issue_id, number in outcome.searched_issues:
        if _covers_issue(queued, number):
            issue_outcomes[searched_issue_id] = IssueSearchOutcome.QUEUED
        elif _covers_issue(found, number):
            issue_outcomes[searched_issue_id] = IssueSearchOutcome.FOUND
        elif outcome.errored:
            issue_outcomes[searched_issue_id] = IssueSearchOutcome.ERROR
        else:
            issue_outcomes[searched_issue_id] = IssueSearchOutcome.NOT_FOUND

    record_issue_searches(issue_outcomes)

    if queued:
        record_search_hit(volume_id)
    elif not outcome.errored:
        record_search_miss(volume_id)
    return


# region Fork library tasks
@TaskHandler.register_task('sync_issues')
class SyncIssues(LibraryTask):
    "Fetch new/updated issues from ComicVine for all monitored volumes"

    stop = False
    message = ''
    display_title = 'Sync Issues'
    priority = 3
    description = (
        'Fetches new and updated issues from ComicVine for all monitored'
        ' volumes, using the timestamp of the last successful sync.'
    )

    @property
    def volume_id(self) -> None:
        return None

    @property
    def issue_id(self) -> None:
        return None

    def __init__(self) -> None:
        return

    def run(self) -> None:
        self.message = 'Syncing issues from ComicVine'
        WebSocket().emit(TaskStatusEvent(self.message))
        cursor = get_db()

        row = cursor.execute(
            "SELECT value FROM config WHERE key='last_issue_sync'"
        ).fetchonedict()
        last_sync = int(row['value']) if row else 0
        since = last_sync - 7200

        volumes = cursor.execute(
            "SELECT comicvine_id, id, last_cv_fetch "
            "FROM volumes WHERE monitored = 1;"
        ).fetchalldict()
        if not volumes:
            return

        cv_to_id_fetch = {
            v['comicvine_id']: (v['id'], v['last_cv_fetch'])
            for v in volumes
        }

        try:
            issue_datas = run(ComicVine().fetch_issues_since(
                tuple(cv_to_id_fetch.keys()), since
            ))
        except (CredentialInvalid, MetadataSourceNotWorking):
            return

        if issue_datas:
            upsert_issues(cv_to_id_fetch, issue_datas)

        cursor.execute(
            "INSERT OR REPLACE INTO config(key, value) "
            "VALUES ('last_issue_sync', ?);",
            (str(int(time())),)
        )
        commit()
        return


@TaskHandler.register_task('refresh_metadata')
class RefreshMetadata(LibraryTask):
    "Refresh volume metadata from ComicVine for stale volumes"

    stop = False
    message = ''
    display_title = 'Refresh Metadata'
    priority = 3
    description = (
        'Refreshes volume metadata (cover images and descriptions) from'
        ' ComicVine for volumes not updated in over 30 days or missing data.'
    )

    @property
    def volume_id(self) -> None:
        return None

    @property
    def issue_id(self) -> None:
        return None

    def __init__(self) -> None:
        return

    def run(self) -> None:
        self.message = 'Refreshing volume metadata'
        WebSocket().emit(TaskStatusEvent(self.message))
        thirty_days_ago = time() - (30 * 86400)

        volumes = get_db().execute(
            """
            SELECT comicvine_id, id, last_cv_fetch
            FROM volumes
            WHERE last_cv_fetch <= :thirty_days_ago
               OR id NOT IN (
                   SELECT volume_id FROM volumes_covers
                   WHERE cover IS NOT NULL
               )
               OR description IS NULL OR description = ''
            ORDER BY last_cv_fetch ASC;
            """,
            {'thirty_days_ago': thirty_days_ago}
        ).fetchalldict()

        if not volumes:
            return

        cv_to_id_fetch = {
            v['comicvine_id']: (v['id'], v['last_cv_fetch'])
            for v in volumes
        }

        try:
            volume_datas = run(
                ComicVine().fetch_volumes(tuple(cv_to_id_fetch.keys()))
            )
        except (CredentialInvalid, MetadataSourceNotWorking):
            return

        if volume_datas:
            update_volume_metadata(cv_to_id_fetch, volume_datas)

        self.check_yield()
        return


@TaskHandler.register_task('scan_files')
class ScanFiles(LibraryTask):
    "Scan files on disk for all monitored volumes"

    stop = False
    message = ''
    display_title = 'Scan Files'
    priority = 3
    description = (
        'Scans files on disk for all monitored volumes and updates'
        ' which issues have local files.'
    )

    @property
    def volume_id(self) -> None:
        return None

    @property
    def issue_id(self) -> None:
        return None

    def __init__(self) -> None:
        return

    def run(self) -> None:
        volume_ids = [
            row[0] for row in get_db().execute(
                "SELECT id FROM volumes WHERE monitored = 1;"
            )
        ]
        total = len(volume_ids)
        ws = WebSocket()
        for idx, vid in enumerate(volume_ids, 1):
            self.check_yield()
            if self.stop:
                break
            self.message = f'Scanning files {idx}/{total}'
            ws.emit(TaskStatusEvent(self.message))
            scan_files(vid)
        return


@TaskHandler.register_task('special_version_refresh')
class SpecialVersionRefresh(LibraryTask):
    "Re-determine the special version type for all non-locked volumes"

    stop = False
    message = ''
    display_title = 'Refresh Special Versions'
    priority = 4
    description = (
        'Re-evaluates the special version type (TPB, Hardcover, One Shot,'
        ' etc.) for all volumes that have not been manually locked.'
    )

    @property
    def volume_id(self) -> None:
        return None

    @property
    def issue_id(self) -> None:
        return None

    def __init__(self) -> None:
        return

    def run(self) -> None:
        cursor = get_db()
        volume_ids = [
            row[0] for row in cursor.execute(
                "SELECT id FROM volumes WHERE special_version_locked = 0;"
            )
        ]
        for vid in volume_ids:
            self.check_yield()
            if self.stop:
                break
            cursor.execute(
                "UPDATE volumes "
                "SET special_version = ? "
                "WHERE id = ? AND special_version_locked = 0",
                (determine_special_version(vid), vid)
            )
        commit()
        return


@TaskHandler.register_task('search_all')
class SearchAll(LibraryTask, DownloadTask):
    "Trigger an automatic search for each volume in the library"

    stop = False
    message = ''
    display_title = 'Search All'
    priority = 5
    description = (
        'Searches for missing issues across every monitored volume in the'
        ' library, starting with the volumes whose newest missing issue came'
        ' out most recently. Downloads are queued as they are found.'
    )

    @property
    def volume_id(self) -> None:
        return None

    @property
    def issue_id(self) -> None:
        return None

    def __init__(self) -> None:
        return

    def run(self) -> List[Tuple[str, int, FilenameData, int, Union[int, None]]]:
        # Downloads are queued per volume as the search goes, so they start
        # right away and survive the task being interrupted.
        ws = WebSocket()
        for volume_id, volume_title in get_search_all_volumes():
            self.check_yield()
            if self.stop:
                break
            self.message = f'Searching for {volume_title}'
            ws.emit(TaskStatusEvent(self.message))

            _search_and_queue(self, volume_id)

        return []


# region Fork calendar tasks
@TaskHandler.register_task('refresh_calendar')
class RefreshCalendar(Task):
    "Pre-warm the calendar cache with +/- 30 days from today"

    stop = False
    message = ''
    display_title = 'Refresh Calendar'
    priority = 3
    description = (
        'Pre-warms the calendar cache with releases in the \u00b130-day window'
        ' around today so that calendar page loads are instant.'
    )

    @property
    def volume_id(self) -> None:
        return None

    @property
    def issue_id(self) -> None:
        return None

    def __init__(self) -> None:
        return

    def run(self) -> None:
        from backend.features.calendar import get_calendar
        today = date.today()
        window_start = (today - timedelta(days=30)).isoformat()
        window_end = (today + timedelta(days=30)).isoformat()

        self.message = 'Refreshing calendar cache'
        ws = WebSocket()
        ws.emit(TaskStatusEvent(self.message))

        # Superset fetch: seeds CVProxy and Kapowarr's in-memory cache for the
        # full window in one request.
        calendar_issues = get_calendar(
            window_start, window_end, force_refresh=True
        )

        # Week-by-week seeding so individual week navigation is instant. These
        # hit the superset cache above, so no extra CVProxy calls are made.
        current = today - timedelta(days=30)
        while current <= today + timedelta(days=30):
            week_end = current + timedelta(days=6)
            get_calendar(current.isoformat(), week_end.isoformat())
            current += timedelta(days=7)

        self.message = 'Calendar cache refreshed'
        ws.emit(TaskStatusEvent(self.message))

        # Refresh metadata for every library volume in the calendar window;
        # these are most likely to have updated CV data.
        local_vol_ids = {
            i['volume_id_local']
            for i in calendar_issues
            if i.get('volume_id_local') and i.get('in_library')
        }
        if not local_vol_ids:
            return

        self.message = 'Refreshing metadata for calendar volumes'
        ws.emit(TaskStatusEvent(self.message))
        LOGGER.info(
            'RefreshCalendar: refreshing metadata for %d calendar volumes',
            len(local_vol_ids)
        )

        placeholders = ','.join('?' * len(local_vol_ids))
        rows = get_db().execute(
            f"SELECT comicvine_id, id, last_cv_fetch "
            f"FROM volumes WHERE id IN ({placeholders});",
            tuple(local_vol_ids)
        ).fetchalldict()
        if not rows:
            return

        cv_to_id_fetch = {
            r['comicvine_id']: (r['id'], r['last_cv_fetch'])
            for r in rows
        }
        try:
            volume_datas = run(
                ComicVine().fetch_volumes(tuple(cv_to_id_fetch.keys()))
            )
        except (CredentialInvalid, MetadataSourceNotWorking):
            return

        if volume_datas:
            update_volume_metadata(cv_to_id_fetch, volume_datas)

        return


def _scrape_gc_weekly_packs(
    task: Task,
    downloads: List[Tuple[str, int, FilenameData, int, Union[int, None]]]
) -> None:
    """Scrape the most recent GetComics weekly packs and add downloads for any
    open issue that matches an entry. Modifies `downloads` in place.

    Args:
        task (Task): The calling task.
        downloads (List[Tuple[str, int, FilenameData, int, Union[int, None]]]):
            The downloads found so far. Links already in here are skipped.
    """
    from backend.implementations.indexer_client_manager import IndexerClients
    from backend.implementations.indexer_clients.ddl.GetComics import \
        GetComicsIndexer
    from backend.implementations.matching import match_title

    gc_indexers = [
        c
        for c in IndexerClients.get_all_clients()
        if isinstance(c, GetComicsIndexer)
        and c.get_indexer_data()['enabled']
    ]
    if not gc_indexers:
        return
    gc = gc_indexers[0]

    task.message = 'Scanning GetComics weekly packs'
    WebSocket().emit(TaskStatusEvent(task.message))

    async def _fetch_packs() -> List[SearchResultData]:
        try:
            return await gc.scrape_weekly_packs(pack_count=3)
        finally:
            await gc.shutdown()

    pack_articles = run(_fetch_packs())
    if not pack_articles:
        return

    open_rows = get_db().execute(
        """
        SELECT v.id AS volume_id, v.title,
               i.id AS issue_id,
               i.calculated_issue_number
        FROM volumes v
        INNER JOIN issues i ON i.volume_id = v.id
        LEFT JOIN issues_files if_ ON if_.issue_id = i.id
        WHERE v.monitored = 1
          AND i.monitored = 1
          AND if_.issue_id IS NULL;
        """
    ).fetchalldict()

    vol_data: Dict[int, Tuple[str, Dict[float, int]]] = {}
    for row in open_rows:
        vid = row['volume_id']
        if vid not in vol_data:
            vol_data[vid] = (row['title'], {})
        vol_data[vid][1][row['calculated_issue_number']] = row['issue_id']

    queued_links: Set[str] = {d[0] for d in downloads}

    for article in pack_articles:
        if task.stop:
            break

        art_series = (article.get('series') or '').strip()
        art_issue_num = article.get('issue_number')
        if (
            not article['link']
            or not art_series
            or article['link'] in queued_links
        ):
            continue

        for vid, (vol_title, issue_map) in vol_data.items():
            if not match_title(vol_title, art_series, allow_contains=True):
                continue

            iss_id = issue_map.get(art_issue_num) # type: ignore
            if iss_id is not None:
                downloads.extend(
                    _results_to_downloads((article,), vid, iss_id)
                )
                queued_links.add(article['link'])
                break

    return


def _run_search_recent(
    task: Task,
    search_start: str,
    search_end: str,
    *,
    clear_days_back: int = 0,
    sync_missing_issues: bool = False,
    run_packs: bool = False,
) -> List[Tuple[str, int, FilenameData, int, Union[int, None]]]:
    """Shared search logic for windowed SearchRecent tasks.

    Args:
        task (Task): Calling task instance. Provides stop flag and message.
        search_start (str): ISO date for the start of the search window.
        search_end (str): ISO date for the end of the search window.
        clear_days_back (int, optional): If > 0, clear auto-blocklist entries
            for all in-library missing issues in the past N days before
            searching, so the next Fresh run can retry previously-failed links.
            Defaults to 0.
        sync_missing_issues (bool, optional): If True, batch-fetch and upsert
            issues for any library volume that appears in the calendar but has
            entries with no local issue ID, then re-enrich the calendar before
            searching.
            Defaults to False.
        run_packs (bool, optional): Whether to scrape GetComics weekly packs.
            Defaults to False.

    Returns:
        List[Tuple[str, int, FilenameData, int, Union[int, None]]]: Downloads.
    """
    from backend.features.calendar import get_calendar

    ws = WebSocket()

    # Blocklist clearing. Fetches a wider range than the search window so it
    # also un-blocks issues in the Fresh window, causing the next Fresh run to
    # retry them.
    if clear_days_back:
        today_iso = date.today().isoformat()
        clear_start = (
            date.today() - timedelta(days=clear_days_back)
        ).isoformat()
        clear_issues = get_calendar(clear_start, today_iso)
        in_scope_ids = sorted({
            i['issue_id_local']
            for i in clear_issues
            if i.get('issue_id_local')
            and i.get('in_library')
            and not i.get('has_files')
        })
        if in_scope_ids:
            placeholders = ','.join('?' * len(in_scope_ids))
            get_db().execute(
                "DELETE FROM blocklist "
                f"WHERE issue_id IN ({placeholders}) "
                "AND reason != ?",
                (*in_scope_ids, BlocklistReasonID.ADDED_BY_USER.value)
            )
            commit()
            LOGGER.debug(
                '%s: cleared stale blocklist for %d issue(s) '
                'across the past %d days',
                task.display_title, len(in_scope_ids), clear_days_back
            )

    calendar_issues = get_calendar(search_start, search_end)

    # Pre-pass: for volumes that appear in the calendar but have issues with
    # no local ID, batch-fetch and upsert their recent issues from CV so the
    # main loop can search by specific issue instead of refreshing the whole
    # volume one at a time.
    if sync_missing_issues:
        vols_with_gaps = {
            i['volume_id_local']
            for i in calendar_issues
            if i.get('volume_id_local')
            and i.get('in_library')
            and i.get('issue_id_local') is None
        }
        if vols_with_gaps:
            placeholders = ','.join('?' * len(vols_with_gaps))
            vol_rows = get_db().execute(
                f"SELECT id, comicvine_id, last_cv_fetch FROM volumes "
                f"WHERE id IN ({placeholders});",
                tuple(vols_with_gaps)
            ).fetchalldict()
            if vol_rows:
                cv_to_id_fetch = {
                    r['comicvine_id']: (r['id'], r['last_cv_fetch'])
                    for r in vol_rows
                }
                # 7-day window catches new issues and recent CV edits.
                since = int(time()) - 7 * 86400
                try:
                    issue_datas = run(ComicVine().fetch_issues_since(
                        tuple(cv_to_id_fetch.keys()), since
                    ))
                    if issue_datas:
                        upsert_issues(cv_to_id_fetch, issue_datas)
                        LOGGER.info(
                            '%s: upserted issues for %d volume(s) '
                            'with calendar gaps',
                            task.display_title, len(vols_with_gaps)
                        )
                        # Re-enrich so issue_id_local reflects new issues.
                        calendar_issues = get_calendar(
                            search_start, search_end
                        )
                except Exception:
                    LOGGER.warning(
                        '%s: issue upsert pre-pass failed',
                        task.display_title,
                        exc_info=True
                    )

    downloads: List[Tuple[str, int, FilenameData, int, Union[int, None]]] = []
    searched_volumes: Set[int] = set()

    for issue in calendar_issues:
        task.check_yield()
        if task.stop:
            break
        if not issue['in_library'] or not issue['volume_monitored']:
            continue

        vol_id = issue['volume_id_local']
        if vol_id is None:
            continue

        iss_id = issue['issue_id_local']

        if iss_id is not None:
            if not issue['monitored'] or issue['has_files']:
                continue
            task.message = (
                f'Searching for {issue["volume_name"]} '
                f'#{issue.get("issue_number", "?")}'
            )
            ws.emit(TaskStatusEvent(task.message))
            _search_and_queue(task, vol_id, iss_id)
            continue

        # Issue not yet in local DB. Refresh the volume first so the issue
        # gets added, then search for the specific issue only.
        if vol_id in searched_volumes:
            continue
        searched_volumes.add(vol_id)
        task.message = (
            f'Syncing {issue["volume_name"]} '
            f'(new issue #{issue.get("issue_number", "?")})'
        )
        ws.emit(TaskStatusEvent(task.message))
        try:
            refresh_and_scan(vol_id)
        except Exception:
            LOGGER.warning(
                'Failed to refresh volume %d during %s',
                vol_id, task.display_title
            )
            continue

        local_iss_id = None
        cv_issue_id = issue.get('comicvine_id')
        if cv_issue_id:
            row = get_db().execute(
                "SELECT id FROM issues WHERE comicvine_id = ?",
                (cv_issue_id,)
            ).fetchone()
            if row:
                local_iss_id = row[0]

        task.message = (
            f'Searching for {issue["volume_name"]} '
            f'#{issue.get("issue_number", "?")}'
        )
        ws.emit(TaskStatusEvent(task.message))
        _search_and_queue(task, vol_id, local_iss_id)

    if run_packs and not task.stop:
        try:
            _scrape_gc_weekly_packs(task, downloads)
        except Exception:
            LOGGER.warning(
                '%s: weekly pack scraping failed',
                task.display_title,
                exc_info=True
            )

    return downloads


@TaskHandler.register_task('search_recent_fresh')
class SearchRecentFresh(LibraryTask, DownloadTask):
    """Search issues in the -6/+7-day window. Runs hourly, no blocklist
    clearing, so it stays fast. The blocklist is reset by SearchRecentWeek
    (daily) so the run after that is longer as previously-blocked issues
    retry."""

    stop = False
    message = ''
    display_title = 'Search Recent (Fresh)'
    priority = 3
    description = (
        'Searches for missing issues released in the past 6 days or due in'
        ' the next 7 days. Runs hourly with no blocklist clearing to stay'
        ' fast. Includes GetComics weekly-pack scraping.'
    )

    @property
    def volume_id(self) -> None:
        return None

    @property
    def issue_id(self) -> None:
        return None

    def __init__(self) -> None:
        return

    def run(self) -> List[Tuple[str, int, FilenameData, int, Union[int, None]]]:
        today = date.today()
        self.message = 'Searching for recent releases (fresh)'
        WebSocket().emit(TaskStatusEvent(self.message))
        return _run_search_recent(
            self,
            search_start=(today - timedelta(days=6)).isoformat(),
            search_end=(today + timedelta(days=7)).isoformat(),
            sync_missing_issues=True,
            run_packs=True,
        )


@TaskHandler.register_task('search_recent_week')
class SearchRecentWeek(LibraryTask, DownloadTask):
    """Search 7-13-day-old issues daily. Also clears the auto-blocklist for the
    full 0-13-day window, so the next Fresh run retries blocked issues."""

    stop = False
    message = ''
    display_title = 'Search Recent (Week-old)'
    priority = 3
    description = (
        'Searches for missing issues released 7-13 days ago. Also clears the'
        ' auto-blocklist for the past 13 days so the next Fresh run retries'
        ' any previously-failed links.'
    )

    @property
    def volume_id(self) -> None:
        return None

    @property
    def issue_id(self) -> None:
        return None

    def __init__(self) -> None:
        return

    def run(self) -> List[Tuple[str, int, FilenameData, int, Union[int, None]]]:
        today = date.today()
        self.message = 'Searching for week-old releases'
        WebSocket().emit(TaskStatusEvent(self.message))
        return _run_search_recent(
            self,
            search_start=(today - timedelta(days=13)).isoformat(),
            search_end=(today - timedelta(days=7)).isoformat(),
            clear_days_back=13,
        )


@TaskHandler.register_task('search_recent_stale')
class SearchRecentStale(LibraryTask, DownloadTask):
    """Search 14-20-day-old issues every 72 hours. No blocklist clearing, as
    links that old have already been retried enough by the Week task."""

    stop = False
    message = ''
    display_title = 'Search Recent (Stale)'
    priority = 3
    description = (
        'Searches for missing issues released 14-20 days ago, every 72 hours.'
        ' No blocklist clearing, as issues that old have already had plenty of'
        ' retry opportunities from the Week task.'
    )

    @property
    def volume_id(self) -> None:
        return None

    @property
    def issue_id(self) -> None:
        return None

    def __init__(self) -> None:
        return

    def run(self) -> List[Tuple[str, int, FilenameData, int, Union[int, None]]]:
        today = date.today()
        self.message = 'Searching for stale releases'
        WebSocket().emit(TaskStatusEvent(self.message))
        return _run_search_recent(
            self,
            search_start=(today - timedelta(days=20)).isoformat(),
            search_end=(today - timedelta(days=14)).isoformat(),
        )


# region Fork system tasks
@TaskHandler.register_task('health_check')
class HealthCheck(Task):
    "Run health checks and dispatch notifications for any issues found"

    stop = False
    message = ''
    display_title = 'Health Check'
    priority = 3
    description = (
        'Runs library health checks (missing root folders, invalid API keys,'
        ' etc.) and dispatches any configured notifications for issues found.'
    )

    @property
    def volume_id(self) -> None:
        return None

    @property
    def issue_id(self) -> None:
        return None

    def __init__(self) -> None:
        return

    def run(self) -> None:
        from backend.features.health_checks import run_health_checks
        from backend.features.notifications import NotificationService

        self.message = 'Running health checks'
        ws = WebSocket()
        ws.emit(TaskStatusEvent(self.message))

        issues = run_health_checks()

        if issues:
            ns = NotificationService()
            for issue in issues:
                try:
                    ns.notify_health_check(issue)
                except Exception:
                    LOGGER.exception(
                        'Failed to dispatch health check notification'
                    )

        count = len(issues)
        self.message = (
            f'Health check complete: {count} issue(s) found'
            if count else 'Health check complete: no issues found'
        )
        ws.emit(TaskStatusEvent(self.message))
        return
