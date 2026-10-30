# -*- coding: utf-8 -*-

"""
Fork-only database schema (not part of upstream Casvt/Kapowarr).

Upstream migrations are numbered by `database_version`, which collides with
any numbered migration the fork adds. Instead of numbering, the fork declares
the schema it needs and `reconcile_fork_schema()` makes the database match,
on every startup. Everything here is idempotent, so `db_migration.py` and
`DB_SCHEMA` can stay identical to upstream.

To add fork schema: add the table to `FORK_TABLES`, the column to
`FORK_COLUMNS`, or the default to `FORK_CONFIG_DEFAULTS`. Renames, drops and
type changes aren't supported; write those as a separate idempotent step.
"""

from typing import Dict, Tuple

from backend.base.logging import LOGGER
from backend.internals.db import get_db

FORK_TABLES = """
CREATE TABLE IF NOT EXISTS notifications(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    provider_type TEXT NOT NULL,
    settings TEXT NOT NULL DEFAULT '{}',
    on_download BOOL NOT NULL DEFAULT 1,
    on_health_check BOOL NOT NULL DEFAULT 1,
    on_volume_add BOOL NOT NULL DEFAULT 1,
    on_application_update BOOL NOT NULL DEFAULT 1,
    enabled BOOL NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS volume_search_stats(
    volume_id INTEGER PRIMARY KEY,
    last_searched INTEGER NOT NULL DEFAULT 0,
    consecutive_misses INTEGER NOT NULL DEFAULT 0,
    total_searches INTEGER NOT NULL DEFAULT 0,
    total_hits INTEGER NOT NULL DEFAULT 0,

    FOREIGN KEY (volume_id) REFERENCES volumes(id)
        ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS volume_publisher_cache(
    comicvine_id INTEGER PRIMARY KEY,
    volume_name TEXT NOT NULL DEFAULT '',
    publisher_name TEXT,
    publisher_id INTEGER,
    cached_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS external_files(
    file_id INTEGER PRIMARY KEY,

    FOREIGN KEY (file_id) REFERENCES files(id)
        ON DELETE CASCADE
);
"""

FORK_COLUMNS: Tuple[Tuple[str, str, str], ...] = (
    # (table, column, definition) added to tables owned by upstream
    ("task_history", "duration_seconds", "INTEGER"),
)

FORK_CONFIG_DEFAULTS: Dict[str, str] = {
    "comicvine_api_url": "https://comicvine.gamespot.com/api",
    "last_issue_sync": "0",
}

# Before merging upstream v1.3.2 the fork numbered its own migrations 45-50,
# colliding with upstream's 45-51.
_UPSTREAM_FIRST_COLLIDING_VERSION = 45
_UPSTREAM_LAST_COLLIDING_VERSION = 51


def _is_pre_merge_fork_db(database_version: int) -> bool:
    """Whether the database was migrated with the fork's original (colliding)
    migrations 45-50 instead of upstream's 45-51.

    Upstream migration 45 adds `external_download_clients.enabled`, and the
    fork's migration 46 created the `notifications` table. A database with a
    version in 46..51 that has `notifications` but lacks `enabled` can
    therefore only have come from the fork.
    """
    if not (
        _UPSTREAM_FIRST_COLLIDING_VERSION
        < database_version
        <= _UPSTREAM_LAST_COLLIDING_VERSION
    ):
        return False

    cursor = get_db()
    has_notifications = cursor.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='table' AND name='notifications';"
    ).exists() is not None
    if not has_notifications:
        return False

    ec_columns = {
        r["name"]
        for r in cursor.execute(
            "PRAGMA table_info(external_download_clients);"
        ).fetchall()
    }
    return "enabled" not in ec_columns


def rewind_pre_merge_fork_db() -> None:
    """Rewind `database_version` of a pre-merge fork database to 45, so
    upstream's migrations 45-51 run. Call before
    `DatabaseMigrationHandler.migrate()`. A no-op for any other database.

    The version is written straight to the config table: going through
    `Settings.update()` would validate all settings, including a live proxy
    check, and fail startup if the proxy is down.
    """
    from backend.internals.settings import Settings

    settings = Settings()
    database_version = settings.sv.database_version
    if not _is_pre_merge_fork_db(database_version):
        return

    LOGGER.info(
        "Detected pre-merge fork database (version %d), "
        "rewinding to %d to apply upstream migrations",
        database_version, _UPSTREAM_FIRST_COLLIDING_VERSION
    )
    get_db().execute(
        "UPDATE config SET value = ? WHERE key = 'database_version';",
        (_UPSTREAM_FIRST_COLLIDING_VERSION,)
    )
    settings.clear_cache()
    return


def reconcile_fork_schema() -> None:
    """Make the database contain everything the fork needs. Call after
    `DatabaseMigrationHandler.migrate()`, so upstream-owned tables are in
    their final shape. Safe to run on every startup."""
    cursor = get_db()
    cursor.executescript(FORK_TABLES)

    for table, column, definition in FORK_COLUMNS:
        existing_columns = {
            r["name"]
            for r in cursor.execute(f"PRAGMA table_info({table});").fetchall()
        }
        if column not in existing_columns:
            cursor.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition};"
            )

    cursor.executemany(
        "INSERT OR IGNORE INTO config(key, value) VALUES (?, ?);",
        FORK_CONFIG_DEFAULTS.items()
    )
    return
