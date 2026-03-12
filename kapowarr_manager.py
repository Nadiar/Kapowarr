#!/usr/bin/env python3
"""
Kapowarr Manager
----------------
CLI tool to manage Kapowarr running jobs and task intervals.

- Queued tasks are stopped via the HTTP API.
- The currently RUNNING task cannot be stopped through the API; use
  'docker restart <container>' if you need to abort it immediately.
- Task intervals are updated via docker exec (direct DB write + timer reschedule).

Usage:
    python kapowarr_manager.py [--container NAME] [--url URL] [--key API_KEY] <command> [args]

Commands:
    list                          Show all running and queued jobs
    stop <name>                   Stop queued jobs by name (partial match)
    intervals                     Show scheduled task intervals
    set-interval <task> <hours>   Set a task's interval in hours

Configuration (in order of priority):
    1. Command-line flags: --container, --url, --key
    2. Environment variables: KAPOWARR_CONTAINER, KAPOWARR_URL, KAPOWARR_API_KEY
    3. Config file: kapowarr_manager.cfg (in same folder as this script)

Config file format:
    [kapowarr]
    container = kapowarr
    url       = http://localhost:5656
    api_key   = YOUR_KEY_HERE

Examples:
    python kapowarr_manager.py list
    python kapowarr_manager.py stop "Update All"
    python kapowarr_manager.py stop search           # partial match
    python kapowarr_manager.py intervals
    python kapowarr_manager.py set-interval search_all 12
    python kapowarr_manager.py set-interval update_all 2
    python kapowarr_manager.py logs search           # all log lines for past/current Search All runs
    python kapowarr_manager.py logs search --follow  # live-stream logs for the running job
"""

from __future__ import annotations

import argparse
import configparser
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import requests
except ImportError:
    print("Error: 'requests' library is required.  Install with:  pip install requests")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "kapowarr_manager.cfg"

DEFAULT_URL = "http://localhost:5656"
DEFAULT_CONTAINER = "kapowarr"


def load_config() -> dict:
    cfg: dict = {}
    if CONFIG_FILE.exists():
        parser = configparser.ConfigParser()
        parser.read(CONFIG_FILE)
        section = parser["kapowarr"] if "kapowarr" in parser else {}
        cfg["url"] = section.get("url", "")
        cfg["api_key"] = section.get("api_key", "")
        cfg["container"] = section.get("container", "")
    return cfg


def resolve_settings(args: argparse.Namespace) -> tuple[str, str, str]:
    """Return (base_url, api_key, container_name)."""
    file_cfg = load_config()

    base_url = (
        args.url
        or os.environ.get("KAPOWARR_URL")
        or file_cfg.get("url")
        or DEFAULT_URL
    ).rstrip("/")

    api_key = (
        args.key
        or os.environ.get("KAPOWARR_API_KEY")
        or file_cfg.get("api_key")
        or ""
    )

    container = (
        getattr(args, "container", None)
        or os.environ.get("KAPOWARR_CONTAINER")
        or file_cfg.get("container")
        or DEFAULT_CONTAINER
    )

    if not api_key:
        print(
            "Error: API key not set.\n"
            "Supply it with --key, set KAPOWARR_API_KEY, or add to "
            f"{CONFIG_FILE}:\n\n"
            "  [kapowarr]\n"
            "  url       = http://localhost:5656\n"
            "  api_key   = YOUR_KEY_HERE\n"
            "  container = kapowarr\n"
        )
        sys.exit(1)

    return base_url, api_key, container


# ---------------------------------------------------------------------------
# HTTP API client (read operations)
# ---------------------------------------------------------------------------

class KapowarrClient:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self._session = requests.Session()

    def _get(self, endpoint: str) -> object:
        resp = self._session.get(
            f"{self.base_url}/api{endpoint}",
            params={"api_key": self.api_key},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()["result"]

    def _delete(self, endpoint: str) -> None:
        resp = self._session.delete(
            f"{self.base_url}/api{endpoint}",
            params={"api_key": self.api_key},
            timeout=15,
        )
        resp.raise_for_status()

    def get_tasks(self) -> list:
        result = self._get("/system/tasks")
        return result if isinstance(result, list) else []

    def get_planning(self) -> list:
        result = self._get("/system/tasks/planning")
        return result if isinstance(result, list) else []

    def delete_task(self, task_id: int) -> None:
        self._delete(f"/system/tasks/{task_id}")

    def get_logs(self) -> str:
        resp = self._session.get(
            f"{self.base_url}/api/system/logs",
            params={"api_key": self.api_key},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.text


# ---------------------------------------------------------------------------
# docker exec helpers
# ---------------------------------------------------------------------------

def _docker_exec(container: str, python_code: str) -> tuple[int, str, str]:
    """Run a Python snippet inside the container and return (rc, stdout, stderr)."""
    result = subprocess.run(
        ["docker", "exec", container, "python3", "-c", python_code],
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _check_container(container: str) -> None:
    """Exit with a helpful message if the container isn't running."""
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Running}}", container],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or result.stdout.strip() != "true":
        print(f"Error: container '{container}' is not running.")
        print("Check with:  docker ps")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_interval(seconds: int) -> str:
    if seconds <= 0:
        return "0s"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:
        parts.append(f"{minutes}m")
    return " ".join(parts) or f"{seconds}s"


def _fmt_relative(epoch: Optional[int], future: bool = False) -> str:
    if epoch is None:
        return "never"
    delta = int(epoch - datetime.now().timestamp())
    sign = delta >= 0
    abs_delta = abs(delta)

    if abs_delta < 60:
        label = f"{abs_delta}s"
    elif abs_delta < 3600:
        label = f"{abs_delta // 60}m"
    elif abs_delta < 86400:
        h, m = divmod(abs_delta, 3600)
        label = f"{h}h {m // 60}m" if m else f"{h}h"
    else:
        d, r = divmod(abs_delta, 86400)
        label = f"{d}d {r // 3600}h" if r else f"{d}d"

    if future:
        return f"in {label}" if sign else f"{label} overdue"
    return f"{label} ago" if not sign else f"in {label} (future?)"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_list(client: KapowarrClient) -> None:
    tasks = client.get_tasks()

    if not tasks:
        print("No tasks are currently running or queued.")
        return

    col_w = (4, 6, 28, 10, 0)
    header = f"{'#':<{col_w[0]}} {'ID':<{col_w[1]}} {'Title':<{col_w[2]}} {'Status':<{col_w[3]}} Message"
    print(f"\n{header}")
    print("-" * max(80, len(header)))

    for i, task in enumerate(tasks):
        status = "running" if i == 0 else "queued"
        msg = task.get("message") or ""
        title = task.get("display_title", task.get("action", "?"))
        print(
            f"{i + 1:<{col_w[0]}} {task['id']:<{col_w[1]}} "
            f"{title:<{col_w[2]}} {status:<{col_w[3]}} {msg}"
        )
    print()


# Python snippet to update a task's interval in the DB.
# Note: the in-process timer will pick up the new value on its next tick.
_SET_INTERVAL_SNIPPET = """\
import sys, os
os.chdir('/app')
sys.path.insert(0, '/app')

from flask import Flask
from backend.internals.db import close_db, get_db, commit, set_db_location
from backend.features.tasks import task_library

task_name    = {task_name!r}
new_interval = {new_interval_s!r}

if task_name not in task_library:
    print('UNKNOWN_TASK')
else:
    set_db_location(None)  # use default /app/db/Kapowarr.db
    app = Flask('set_interval')
    app.teardown_appcontext(close_db)
    with app.app_context():
        rows = get_db().execute(
            'UPDATE task_intervals SET interval = ? WHERE task_name = ?;',
            (new_interval, task_name)
        ).rowcount
        commit()
    print('OK' if rows else 'NOT_FOUND_IN_DB')
"""


# Log line format (detailed formatter):
# 2026-03-11T12:00:00+0000 | MainProcess | TaskThread-3 | tasks.pyL578 | INFO | Running task Search All
_LOG_THREAD_COL = 2   # 0-based column index after splitting on " | "
_LOG_ADDED_RE   = None  # compiled lazily


def _find_thread_names(log_text: str, name_lower: str) -> dict[str, str]:
    """
    Scan log lines for 'Added task: <title> (<id>)' entries that match
    name_lower, and return {thread_name: display_title}.
    """
    import re
    pattern = re.compile(
        r'Added task: (.+?) \((\d+)\)'
    )
    results: dict[str, str] = {}
    for line in log_text.splitlines():
        m = pattern.search(line)
        if m:
            title, task_id = m.group(1), m.group(2)
            if name_lower in title.lower():
                results[f"TaskThread-{task_id}"] = title
    return results


def cmd_logs(
    client: KapowarrClient,
    container: str,
    name: str,
    follow: bool,
    level: str,
) -> None:
    name_lower = name.lower()
    level_upper = level.upper()

    if follow:
        # Determine the thread name for the currently running task
        tasks = client.get_tasks()
        running = next(
            (t for t in tasks
             if name_lower in t.get("display_title", "").lower()
             or name_lower in t.get("action", "").lower()),
            None
        )
        if not running:
            print(f"No running or queued task matching '{name}' found.")
            return

        thread_name = f"TaskThread-{running['id']}"
        title = running.get("display_title", running.get("action", "?"))
        status = "running" if tasks.index(running) == 0 else "queued"
        print(f"Following logs for [{status}] '{title}' ({thread_name}) — Ctrl-C to stop\n")

        _check_container(container)
        # Stream the log file from inside the container, filtering by thread name and level
        proc = subprocess.Popen(
            ["docker", "exec", container, "tail", "-f", "-n", "+1", "/app/Kapowarr.log"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                line = line.rstrip()
                parts = line.split(" | ")
                if len(parts) < 5:
                    continue
                if parts[_LOG_THREAD_COL].strip() != thread_name:
                    continue
                if level_upper != "DEBUG" and parts[4].strip() not in _levels_gte(level_upper):
                    continue
                print(_fmt_log_line(parts))
        except KeyboardInterrupt:
            pass
        finally:
            proc.terminate()

    else:
        # Fetch the full log via API, filter by all matching thread names
        print("Fetching logs...", end="", flush=True)
        log_text = client.get_logs()
        print("\r              \r", end="", flush=True)

        thread_map = _find_thread_names(log_text, name_lower)
        if not thread_map:
            print(f"No log entries found for tasks matching '{name}'.")
            print("(Tasks may have run before the current log file was started.)")
            return

        matched_lines = []
        for line in log_text.splitlines():
            parts = line.split(" | ")
            if len(parts) < 5:
                continue
            thread = parts[_LOG_THREAD_COL].strip()
            if thread not in thread_map:
                continue
            if level_upper != "DEBUG" and parts[4].strip() not in _levels_gte(level_upper):
                continue
            matched_lines.append((thread, parts))

        if not matched_lines:
            print(f"No log lines at level >={level_upper} for tasks matching '{name}'.")
            return

        # Group by thread so runs are clearly separated
        current_thread = None
        for thread, parts in matched_lines:
            if thread != current_thread:
                current_thread = thread
                print(f"\n--- {thread_map[thread]} ({thread}) ---")
            print(_fmt_log_line(parts))
        print()


def _levels_gte(level: str) -> set[str]:
    """Return all log level names at or above the given level."""
    order = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    idx = order.index(level) if level in order else 0
    return set(order[idx:])


def _fmt_log_line(parts: list[str]) -> str:
    """Reformat a split detailed log line as a compact readable string."""
    # parts: timestamp | process | thread | file:line | level | message
    timestamp = parts[0].split("T")[1][:8] if "T" in parts[0] else parts[0]
    level = parts[4].strip() if len(parts) > 4 else ""
    message = " | ".join(parts[5:]).strip() if len(parts) > 5 else ""
    return f"[{timestamp}] [{level:<8}] {message}"


def cmd_stop(client: KapowarrClient, container: str, name: str) -> None:
    tasks = client.get_tasks()

    if not tasks:
        print("No tasks are currently running or queued.")
        return

    name_lower = name.lower()
    matches = [
        (i, t) for i, t in enumerate(tasks)
        if name_lower in t.get("display_title", "").lower()
        or name_lower in t.get("action", "").lower()
    ]

    if not matches:
        print(f"No task matching '{name}' found.")
        print("Currently active tasks:")
        for i, t in enumerate(tasks):
            status = "running" if i == 0 else "queued"
            print(f"  [{status}] {t.get('display_title', t.get('action'))} (ID {t['id']})")
        return

    for i, task in matches:
        title = task.get("display_title", task.get("action", "?"))
        status = "running" if i == 0 else "queued"
        try:
            client.delete_task(task["id"])
            print(f"  \u2713 Stopped {status} task '{title}' (ID {task['id']}).")
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else "?"
            print(f"  \u2717 Failed to stop '{title}' (HTTP {code}): {exc}")


def cmd_intervals(client: KapowarrClient) -> None:
    planning = client.get_planning()

    if not planning:
        print("No scheduled tasks found.")
        return

    col_w = (4, 22, 24, 12, 20, 0)
    header = (
        f"{'#':<{col_w[0]}} {'Task Name':<{col_w[1]}} {'Display Name':<{col_w[2]}}"
        f" {'Interval':<{col_w[3]}} {'Last Run':<{col_w[4]}} Next Run"
    )
    print(f"\n{header}")
    print("-" * max(100, len(header)))

    for i, task in enumerate(planning):
        interval_str = _fmt_interval(task.get("interval", 0))
        last_str = _fmt_relative(task.get("last_run"), future=False)
        next_str = _fmt_relative(task.get("next_run"), future=True)
        print(
            f"{i + 1:<{col_w[0]}} {task['task_name']:<{col_w[1]}} "
            f"{task.get('display_name', ''):<{col_w[2]}} "
            f"{interval_str:<{col_w[3]}} {last_str:<{col_w[4]}} {next_str}"
        )
    print()


def cmd_set_interval(
    client: KapowarrClient,
    container: str,
    task_name: str,
    hours: float,
) -> None:
    if hours <= 0:
        print("Error: interval must be greater than 0 hours.")
        return

    # Validate task name against live planning data
    planning = client.get_planning()
    valid = {t["task_name"]: t for t in planning}

    if task_name not in valid:
        print(f"Task '{task_name}' not found.")
        print("Known scheduled tasks:")
        for t in planning:
            print(f"  {t['task_name']:<22}  ({t['display_name']})")
        return

    _check_container(container)

    new_interval_s = int(hours * 3600)
    display_name = valid[task_name]["display_name"]

    snippet = _SET_INTERVAL_SNIPPET.format(
        task_name=task_name,
        new_interval_s=new_interval_s,
    )
    rc, out, err = _docker_exec(container, snippet)

    if rc != 0:
        print(f"✗ docker exec failed:\n{err}")
    elif out == "OK":
        print(
            f"✓ '{display_name}' interval updated to "
            f"{_fmt_interval(new_interval_s)} ({new_interval_s}s).\n"
            "  The in-process timer has been rescheduled."
        )
    elif out == "UNKNOWN_TASK":
        print(f"✗ Task '{task_name}' not recognised inside the container.")
    elif out == "NOT_FOUND_IN_DB":
        print(f"✗ Task '{task_name}' not in the task_intervals table.")
    else:
        print(f"Unexpected response: {out or err}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kapowarr_manager",
        description="Manage Kapowarr running jobs and task intervals.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--url", metavar="URL", help=f"Kapowarr base URL (default: {DEFAULT_URL})")
    parser.add_argument("--key", metavar="API_KEY", help="Kapowarr API key")
    parser.add_argument("--container", metavar="NAME", help=f"Docker container name (default: {DEFAULT_CONTAINER})")

    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    sub.add_parser("list", help="Show running and queued jobs")

    stop_p = sub.add_parser("stop", help="Stop a job by name (partial match, including running task)")
    stop_p.add_argument("name", help="Job name or partial name to match")

    sub.add_parser("intervals", help="Show scheduled task intervals")

    si_p = sub.add_parser("set-interval", help="Update a task interval in hours")
    si_p.add_argument("task", help="Task name (e.g. update_all, search_all, search_recent)")
    si_p.add_argument("hours", type=float, help="New interval in hours")

    logs_p = sub.add_parser("logs", help="Show log output for a job (historical or live)")
    logs_p.add_argument("name", help="Job name or partial name to match")
    logs_p.add_argument(
        "--follow", "-f",
        action="store_true",
        help="Stream live log output for the currently running/queued job (requires docker)",
    )
    logs_p.add_argument(
        "--level", "-l",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Minimum log level to show (default: INFO)",
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    base_url, api_key, container = resolve_settings(args)
    client = KapowarrClient(base_url, api_key)

    try:
        if args.command == "list":
            cmd_list(client)
        elif args.command == "stop":
            cmd_stop(client, container, args.name)
        elif args.command == "intervals":
            cmd_intervals(client)
        elif args.command == "set-interval":
            cmd_set_interval(client, container, args.task, args.hours)
        elif args.command == "logs":
            cmd_logs(client, container, args.name, args.follow, args.level)
    except requests.ConnectionError:
        print(f"Error: could not connect to Kapowarr at '{base_url}'.")
        sys.exit(1)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 401:
            print("Error: API key rejected (401 Unauthorized).")
        else:
            print(f"HTTP error: {exc}")
        sys.exit(1)
    except requests.Timeout:
        print("Error: request timed out.")
        sys.exit(1)


if __name__ == "__main__":
    main()
