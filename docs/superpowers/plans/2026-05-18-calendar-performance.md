# Calendar Performance Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut cold-cache calendar load times by parallelizing volume enrichment, caching volume→publisher data in SQLite, and extending the nightly pre-warm window to ±30 days.

**Architecture:** Three independent changes: (1) `gather()` in `fetch_volumes_for_enrichment` so all volume batches execute concurrently; (2) a new `volume_publisher_cache` SQLite table (migration #50) queried before any CVProxy call; (3) `RefreshCalendar.run()` seeding ±30 days week-by-week after the superset fetch.

**Tech Stack:** Python 3.8+, asyncio.gather, aiohttp AsyncSession, SQLite3, unittest

---

## File Map

| File | Change |
|---|---|
| `backend/implementations/comicvine.py` | Replace sequential loop with `gather()` in `fetch_volumes_for_enrichment` |
| `backend/internals/db_migration.py` | Add migration #50 — `CREATE TABLE volume_publisher_cache` |
| `backend/features/calendar_cv.py` | Update `_fetch_missing_volumes` to read/write SQLite cache; add TTL constant and `get_db` import |
| `backend/features/tasks.py` | Extend `RefreshCalendar.run()` to ±30 days + week-by-week seeding |
| `tests/Tbackend/test_calendar_perf.py` | New — tests for parallel volume fetch |
| `tests/Tbackend/test_migration_50.py` | New — migration #50 smoke test |
| `tests/Tbackend/test_volume_publisher_cache.py` | New — cache hit/miss/expiry tests |
| `tests/Tbackend/test_refresh_calendar.py` | New — ±30-day window + week seeding tests |

---

## Task 1: Parallelize `fetch_volumes_for_enrichment`

**Files:**
- Modify: `backend/implementations/comicvine.py:947-987`
- Create: `tests/Tbackend/test_calendar_perf.py`

- [ ] **Step 1.1: Write the failing test**

Create `tests/Tbackend/test_calendar_perf.py`:

```python
"""
Tests that fetch_volumes_for_enrichment uses gather() for parallel batches.
"""
import asyncio
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

if 'grp' not in sys.modules:
    sys.modules['grp'] = MagicMock()

from backend.implementations.comicvine import ComicVine


def _make_cv() -> ComicVine:
    """Build a ComicVine instance without reading settings."""
    cv = ComicVine.__new__(ComicVine)
    cv.api_url = 'http://fake-cv/api'
    cv._params = {'format': 'json', 'api_key': 'testkey'}
    return cv


class TestFetchVolumesParallel(unittest.TestCase):
    """fetch_volumes_for_enrichment must dispatch all batches via gather()."""

    def _fake_call_api(self, fake_results):
        """Return an async side_effect that returns results for requested IDs."""
        async def side_effect(session, url, params, default=None):
            ids_raw = params['filter'].split('id:')[1]
            ids = {int(x) for x in ids_raw.split('|')}
            return {'results': [r for r in fake_results if int(r['id']) in ids]}
        return side_effect

    def test_gather_called_for_multiple_batches(self):
        """With 150 IDs (2 batches of 100/50), gather() must be called once."""
        cv = _make_cv()
        volume_ids = list(range(1, 151))
        fake_vols = [{'id': str(i), 'name': f'Vol{i}', 'publisher': {}}
                     for i in volume_ids]

        mock_session = AsyncMock()
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch('backend.implementations.comicvine.AsyncSession',
                   return_value=mock_ctx), \
             patch.object(type(cv), '_ComicVine__call_api',
                          side_effect=self._fake_call_api(fake_vols)), \
             patch('backend.implementations.comicvine.gather',
                   wraps=asyncio.gather) as mock_gather:
            result = asyncio.run(cv.fetch_volumes_for_enrichment(volume_ids))

        mock_gather.assert_called_once()
        self.assertEqual(len(result), 150)

    def test_empty_volume_ids_returns_empty(self):
        """Empty input should return [] without calling the API."""
        cv = _make_cv()
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch('backend.implementations.comicvine.AsyncSession',
                   return_value=mock_ctx), \
             patch('backend.implementations.comicvine.gather',
                   wraps=asyncio.gather) as mock_gather:
            result = asyncio.run(cv.fetch_volumes_for_enrichment([]))

        mock_gather.assert_not_called()
        self.assertEqual(result, [])

    def test_single_batch_returns_all_results(self):
        """50 IDs (one batch) → all 50 returned."""
        cv = _make_cv()
        volume_ids = list(range(1, 51))
        fake_vols = [{'id': str(i), 'name': f'Vol{i}', 'publisher': {}}
                     for i in volume_ids]

        mock_session = AsyncMock()
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch('backend.implementations.comicvine.AsyncSession',
                   return_value=mock_ctx), \
             patch.object(type(cv), '_ComicVine__call_api',
                          side_effect=self._fake_call_api(fake_vols)):
            result = asyncio.run(cv.fetch_volumes_for_enrichment(volume_ids))

        self.assertEqual(len(result), 50)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 1.2: Run test to confirm it fails**

```bash
cd "y:/docker/comics/kapowarr"
python -m unittest tests.Tbackend.test_calendar_perf.TestFetchVolumesParallel.test_gather_called_for_multiple_batches -v
```

Expected: `FAIL — AssertionError: gather() was not called` (current code never calls `gather`).

- [ ] **Step 1.3: Implement parallel fetch**

Replace `fetch_volumes_for_enrichment` in `backend/implementations/comicvine.py` (lines 947–987):

```python
    async def fetch_volumes_for_enrichment(
        self,
        volume_ids: List[int]
    ) -> List[Dict[str, Any]]:
        """Fetch raw volume data for publisher enrichment.

        All batches are dispatched concurrently via asyncio.gather so
        latency scales with the slowest single batch rather than the sum.

        Args:
            volume_ids: CV volume IDs to fetch.

        Returns:
            List of raw API result dicts for matching volumes.
        """
        if not volume_ids:
            return []

        enrichment_field_list = ','.join((
            'id', 'name', 'publisher', 'image', 'site_detail_url',
            'aliases', 'count_of_issues', 'deck', 'description', 'start_year'
        ))

        async with AsyncSession() as session:
            tasks = [
                self.__call_api(
                    session,
                    '/volumes',
                    {
                        'field_list': enrichment_field_list,
                        'filter': 'id:{}'.format(
                            '|'.join(str(vid) for vid in id_batch)
                        )
                    },
                    {'results': []}
                )
                for id_batch in batched(volume_ids, 100)
            ]
            responses = await gather(*tasks)

        all_vols: List[Dict[str, Any]] = []
        for resp in responses:
            all_vols.extend(resp.get('results', []))
        return all_vols
```

- [ ] **Step 1.4: Run all three tests**

```bash
python -m unittest tests.Tbackend.test_calendar_perf -v
```

Expected: 3 tests pass.

- [ ] **Step 1.5: Run full test suite to check for regressions**

```bash
python -m unittest discover -s ./tests -p '*.py' 2>&1 | tail -5
```

Expected: no failures introduced.

- [ ] **Step 1.6: Commit**

```bash
git add backend/implementations/comicvine.py tests/Tbackend/test_calendar_perf.py
git commit -m "perf(calendar): parallelize fetch_volumes_for_enrichment with gather()"
```

---

## Task 2: DB Migration #50 — `volume_publisher_cache` Table

**Files:**
- Modify: `backend/internals/db_migration.py`
- Create: `tests/Tbackend/test_migration_50.py`

- [ ] **Step 2.1: Write the failing test**

Create `tests/Tbackend/test_migration_50.py`:

```python
"""Smoke test for DB migration #50: volume_publisher_cache table."""
import sqlite3
import sys
import unittest
from unittest.mock import MagicMock, patch

if 'grp' not in sys.modules:
    sys.modules['grp'] = MagicMock()

from backend.internals.db import KapowarrCursor


def _make_cursor(schema: str = '') -> KapowarrCursor:
    conn = sqlite3.connect(':memory:', check_same_thread=False)
    conn.row_factory = sqlite3.Row
    cursor = KapowarrCursor(conn)
    cursor.row_factory = sqlite3.Row
    if schema:
        cursor.executescript(schema)
    return cursor


class TestMigration50(unittest.TestCase):
    """Migration #50 must create volume_publisher_cache on an empty DB."""

    def _run_migration(self, schema: str = '') -> KapowarrCursor:
        from backend.internals.db_migration import DatabaseMigrationHandler
        handler = DatabaseMigrationHandler.handlers.get(50)
        self.assertIsNotNone(handler, 'Migration 50->51 not registered')
        cursor = _make_cursor(schema)
        with patch('backend.internals.db_migration.get_db', return_value=cursor):
            handler()
        return cursor

    def test_creates_volume_publisher_cache_table(self):
        cursor = self._run_migration()
        tables = {
            row[0]
            for row in cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table';"
            ).fetchall()
        }
        self.assertIn('volume_publisher_cache', tables)

    def test_table_has_expected_columns(self):
        cursor = self._run_migration()
        cols = {
            row['name']
            for row in cursor.execute(
                'PRAGMA table_info(volume_publisher_cache);'
            ).fetchall()
        }
        self.assertEqual(
            cols,
            {'comicvine_id', 'volume_name', 'publisher_name',
             'publisher_id', 'cached_at'}
        )

    def test_idempotent_on_existing_table(self):
        """Running migration twice must not raise."""
        existing = """
            CREATE TABLE volume_publisher_cache (
                comicvine_id   INTEGER PRIMARY KEY,
                volume_name    TEXT    NOT NULL DEFAULT '',
                publisher_name TEXT,
                publisher_id   INTEGER,
                cached_at      REAL    NOT NULL
            );
        """
        cursor = self._run_migration(schema=existing)
        tables = {
            row[0]
            for row in cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table';"
            ).fetchall()
        }
        self.assertIn('volume_publisher_cache', tables)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2.2: Run test to confirm it fails**

```bash
python -m unittest tests.Tbackend.test_migration_50.TestMigration50.test_creates_volume_publisher_cache_table -v
```

Expected: `FAIL — AssertionError: Migration 50->51 not registered` (no migration #50 exists yet).

- [ ] **Step 2.3: Add migration #50 to `db_migration.py`**

`register_handler(N)` means "migrate FROM version N". The last existing handler is `register_handler(49)` (task history duration, 49→50). The new one migrates 50→51, so use `register_handler(50)`.

Append after the last handler in `backend/internals/db_migration.py`:

```python
@DatabaseMigrationHandler.register_handler(50)
def _migrate_add_volume_publisher_cache():
    """Migration 50→51: add volume_publisher_cache for calendar enrichment."""
    get_db().executescript("""
        CREATE TABLE IF NOT EXISTS volume_publisher_cache (
            comicvine_id   INTEGER PRIMARY KEY,
            volume_name    TEXT    NOT NULL DEFAULT '',
            publisher_name TEXT,
            publisher_id   INTEGER,
            cached_at      REAL    NOT NULL
        );
    """)
```

- [ ] **Step 2.4: Run migration tests**

```bash
python -m unittest tests.Tbackend.test_migration_50 -v
```

Expected: 3 tests pass.

- [ ] **Step 2.5: Run full suite**

```bash
python -m unittest discover -s ./tests -p '*.py' 2>&1 | tail -5
```

Expected: no failures.

- [ ] **Step 2.6: Commit**

```bash
git add backend/internals/db_migration.py tests/Tbackend/test_migration_50.py
git commit -m "feat(db): migration 50 — add volume_publisher_cache table"
```

---

## Task 3: Volume→Publisher SQLite Cache in `_fetch_missing_volumes`

**Files:**
- Modify: `backend/features/calendar_cv.py`
- Create: `tests/Tbackend/test_volume_publisher_cache.py`

- [ ] **Step 3.1: Write failing tests**

Create `tests/Tbackend/test_volume_publisher_cache.py`:

```python
"""
Tests for the volume_publisher_cache integration in _fetch_missing_volumes.

Uses an in-memory SQLite DB so no live Kapowarr instance is needed.
"""
import sqlite3
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

if 'grp' not in sys.modules:
    sys.modules['grp'] = MagicMock()

from backend.internals.db import KapowarrCursor

_CACHE_SCHEMA = """
    CREATE TABLE volume_publisher_cache (
        comicvine_id   INTEGER PRIMARY KEY,
        volume_name    TEXT    NOT NULL DEFAULT '',
        publisher_name TEXT,
        publisher_id   INTEGER,
        cached_at      REAL    NOT NULL
    );
"""


def _make_cursor() -> KapowarrCursor:
    conn = sqlite3.connect(':memory:', check_same_thread=False)
    conn.row_factory = sqlite3.Row
    cursor = KapowarrCursor(conn)
    cursor.row_factory = sqlite3.Row
    cursor.executescript(_CACHE_SCHEMA)
    return cursor


class TestFetchMissingVolumesCache(unittest.TestCase):
    """_fetch_missing_volumes must use the SQLite cache to skip CVProxy calls."""

    def _make_fake_cv(self, raw_vols):
        """Return a mock ComicVine whose fetch_volumes_for_enrichment returns raw_vols."""
        mock_cv = MagicMock()
        mock_cv.fetch_volumes_for_enrichment = MagicMock(return_value=raw_vols)
        return mock_cv

    def test_cache_miss_calls_cvproxy_and_stores_result(self):
        """On a cold cache, CVProxy is called and the result is stored."""
        cursor = _make_cursor()
        volume_map = {}
        raw_vols = [
            {'id': '101', 'name': 'Alpha', 'publisher': {'id': '10', 'name': 'Marvel'}},
        ]
        mock_cv = self._make_fake_cv(raw_vols)

        with patch('backend.features.calendar_cv.get_db', return_value=cursor), \
             patch('backend.features.calendar_cv.ComicVine', return_value=mock_cv), \
             patch('backend.features.calendar_cv.run',
                   side_effect=lambda coro: raw_vols):
            from backend.features.calendar_cv import _fetch_missing_volumes
            _fetch_missing_volumes([101], volume_map)

        self.assertIn(101, volume_map)
        self.assertEqual(volume_map[101]['volume_name'], 'Alpha')

        row = cursor.execute(
            'SELECT * FROM volume_publisher_cache WHERE comicvine_id = 101;'
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row['volume_name'], 'Alpha')

    def test_cache_hit_skips_cvproxy(self):
        """A fresh cached entry → CVProxy is NOT called."""
        cursor = _make_cursor()
        now = time.time()
        with cursor:
            cursor.execute(
                'INSERT INTO volume_publisher_cache '
                '(comicvine_id, volume_name, publisher_name, publisher_id, cached_at) '
                'VALUES (?, ?, ?, ?, ?);',
                (201, 'Beta', 'DC Comics', 20, now)
            )

        volume_map = {}
        mock_cv = MagicMock()

        with patch('backend.features.calendar_cv.get_db', return_value=cursor), \
             patch('backend.features.calendar_cv.ComicVine', return_value=mock_cv), \
             patch('backend.features.calendar_cv.run') as mock_run:
            from backend.features.calendar_cv import _fetch_missing_volumes
            _fetch_missing_volumes([201], volume_map)

        mock_run.assert_not_called()
        self.assertIn(201, volume_map)
        self.assertEqual(volume_map[201]['publisher_name'], 'DC Comics')

    def test_expired_cache_entry_triggers_refresh(self):
        """An expired entry (older than TTL) must be re-fetched from CVProxy."""
        from backend.features.calendar_cv import VOLUME_PUBLISHER_CACHE_TTL
        cursor = _make_cursor()
        stale_time = time.time() - VOLUME_PUBLISHER_CACHE_TTL - 1
        with cursor:
            cursor.execute(
                'INSERT INTO volume_publisher_cache '
                '(comicvine_id, volume_name, publisher_name, publisher_id, cached_at) '
                'VALUES (?, ?, ?, ?, ?);',
                (301, 'Gamma', 'Image', 30, stale_time)
            )

        volume_map = {}
        raw_vols = [
            {'id': '301', 'name': 'Gamma Updated',
             'publisher': {'id': '30', 'name': 'Image Updated'}},
        ]
        mock_cv = self._make_fake_cv(raw_vols)

        with patch('backend.features.calendar_cv.get_db', return_value=cursor), \
             patch('backend.features.calendar_cv.ComicVine', return_value=mock_cv), \
             patch('backend.features.calendar_cv.run',
                   side_effect=lambda coro: raw_vols):
            from backend.features.calendar_cv import _fetch_missing_volumes
            _fetch_missing_volumes([301], volume_map)

        self.assertEqual(volume_map[301]['volume_name'], 'Gamma Updated')

    def test_empty_volume_ids_is_noop(self):
        """Empty input must not touch the DB or CVProxy."""
        cursor = _make_cursor()
        volume_map = {}

        with patch('backend.features.calendar_cv.get_db', return_value=cursor), \
             patch('backend.features.calendar_cv.ComicVine') as MockCV, \
             patch('backend.features.calendar_cv.run') as mock_run:
            from backend.features.calendar_cv import _fetch_missing_volumes
            _fetch_missing_volumes([], volume_map)

        MockCV.assert_not_called()
        mock_run.assert_not_called()
        self.assertEqual(volume_map, {})


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 3.2: Run tests to confirm they fail**

```bash
python -m unittest tests.Tbackend.test_volume_publisher_cache -v
```

Expected: All tests fail — `ImportError: cannot import name 'VOLUME_PUBLISHER_CACHE_TTL'` or `AssertionError` because no cache logic exists yet.

- [ ] **Step 3.3: Implement cache logic in `calendar_cv.py`**

Replace the full contents of `backend/features/calendar_cv.py` with:

```python
# -*- coding: utf-8 -*-

"""
ComicVine API integration for the calendar feature.

Fetches issues by store_date range and enriches them with
publisher information from the CV API.
"""

from asyncio import run
from time import time as _time
from typing import Any, Dict, List

from backend.base.custom_exceptions import InvalidComicVineApiKey
from backend.base.logging import LOGGER
from backend.features.calendar import CalendarIssue
from backend.features.calendar_publishers import (normalize_publisher_name,
                                                  resolve_parent_publisher)
from backend.implementations.comicvine import ComicVine
from backend.internals.db import get_db

# Volume-publisher cache TTL: 30 days (publishers rarely change)
VOLUME_PUBLISHER_CACHE_TTL: float = 30 * 86400


def _pick_effective_date(
    store_date: str | None,
    cover_date: str | None
) -> str:
    """Return store_date, falling back to cover_date, then ``'Unknown'``.

    store_date is the actual shelf date and takes priority.
    cover_date is used only when store_date is absent.
    """
    return store_date or cover_date or 'Unknown'


def _enrich_with_publishers(
    issues: List[Dict[str, Any]]
) -> List[CalendarIssue]:
    """Enrich raw issue dicts with publisher info from the CV API.

    Filters out stub entries that have no description and no creators
    (likely placeholder or erroneous CV entries).

    Args:
        issues: List of raw CV API issue result dicts.

    Returns:
        List of CalendarIssue TypedDicts with publisher info filled in.
    """
    # NOTE: Stub filter removed — CVProxy may return issues without
    # description/person_credits from local cache; filtering here
    # Collect unique volume IDs
    volume_ids = {
        int(issue['volume']['id'])
        for issue in issues
        if issue.get('volume')
    }

    volume_publisher_map: Dict[int, Dict[str, Any]] = {}

    # Fetch all volumes from CV API (or local SQLite cache)
    if volume_ids:
        _fetch_missing_volumes(list(volume_ids), volume_publisher_map)

    # Build CalendarIssue list
    result: List[CalendarIssue] = []
    for issue in issues:
        vol = issue.get('volume') or {}
        vol_id = int(vol.get('id', 0))
        vol_info = volume_publisher_map.get(vol_id, {})

        image = issue.get('image') or {}
        image_url = (
            image.get('small_url')
            or image.get('medium_url')
            or image.get('original_url')
            or ''
        )

        pub_id = vol_info.get('publisher_id')
        pub_name = vol_info.get('publisher_name')
        parent_name = resolve_parent_publisher(
            pub_name
        ) if pub_name else None

        calendar_issue: CalendarIssue = {
            'comicvine_id': int(issue['id']),
            'volume_id': vol_id,
            'volume_name': vol_info.get(
                'volume_name', vol.get('name', '')
            ),
            'issue_number': (
                issue.get('issue_number') or ''
            ).strip(),
            'title': issue.get('name') or None,
            'store_date': issue.get('store_date') or None,
            'cover_date': issue.get('cover_date') or None,
            'effective_date': _pick_effective_date(
                issue.get('store_date'),
                issue.get('cover_date')
            ),
            'image_url': image_url,
            'publisher_name': pub_name,
            'publisher_id': pub_id,
            'parent_publisher_name': parent_name,
            'parent_publisher_id': None,
            'site_url': issue.get('site_detail_url') or '',
            # Library status defaults (enriched later)
            'in_library': False,
            'monitored': False,
            'volume_monitored': False,
            'volume_id_local': None,
            'has_files': False
        }
        result.append(calendar_issue)

    return result


def _fetch_missing_volumes(
    volume_ids: List[int],
    volume_publisher_map: Dict[int, Dict[str, Any]]
) -> None:
    """Fetch volumes from the SQLite cache or CV API and update the publisher map.

    Checks the local ``volume_publisher_cache`` table first (30-day TTL).
    Only calls CVProxy for IDs that are absent or expired.
    Freshly fetched results are upserted into the cache.

    Args:
        volume_ids: CV volume IDs to look up.
        volume_publisher_map: Dict to update with fetched publisher info.
    """
    if not volume_ids:
        return

    now = _time()
    cursor = get_db()
    placeholders = ','.join('?' * len(volume_ids))

    # --- 1. Load fresh cached entries ---
    rows = cursor.execute(
        f'SELECT comicvine_id, volume_name, publisher_name, publisher_id '
        f'FROM volume_publisher_cache '
        f'WHERE comicvine_id IN ({placeholders}) '
        f'  AND cached_at > ?;',
        (*volume_ids, now - VOLUME_PUBLISHER_CACHE_TTL)
    ).fetchall()

    cached_ids: set = set()
    for row in rows:
        cid = row[0]
        volume_publisher_map[cid] = {
            'volume_name': row[1] or '',
            'publisher_id': row[3],
            'publisher_name': row[2],
        }
        cached_ids.add(cid)

    gap_ids = [vid for vid in volume_ids if vid not in cached_ids]
    if not gap_ids:
        return

    # --- 2. Fetch gaps from CVProxy ---
    try:
        cv = ComicVine()
    except InvalidComicVineApiKey:
        LOGGER.warning(
            'No CV API key set; cannot enrich calendar with publishers'
        )
        return

    try:
        raw_vols = run(cv.fetch_volumes_for_enrichment(gap_ids))
    except Exception as e:
        LOGGER.warning('Failed to fetch missing volumes for calendar: %s', e)
        return

    # --- 3. Update map and upsert to cache ---
    rows_to_upsert = []
    for vol in raw_vols:
        vid = int(vol['id'])
        pub = vol.get('publisher') or {}
        pub_name = normalize_publisher_name(pub.get('name'))
        pub_id = int(pub['id']) if pub.get('id') else None
        info: Dict[str, Any] = {
            'volume_name': vol.get('name', ''),
            'publisher_id': pub_id,
            'publisher_name': pub_name,
        }
        volume_publisher_map[vid] = info
        rows_to_upsert.append((
            vid,
            info['volume_name'],
            info['publisher_name'],
            info['publisher_id'],
            now,
        ))

    if rows_to_upsert:
        with cursor:
            cursor.executemany(
                'INSERT OR REPLACE INTO volume_publisher_cache '
                '(comicvine_id, volume_name, publisher_name, '
                ' publisher_id, cached_at) '
                'VALUES (?, ?, ?, ?, ?);',
                rows_to_upsert
            )


def fetch_calendar_issues(
    start_date: str,
    end_date: str
) -> List[CalendarIssue]:
    """Fetch all issues in the given date range from ComicVine.

    Queries by ``store_date`` only (the actual shelf date).

    Args:
        start_date: Start date in YYYY-MM-DD format (inclusive).
        end_date: End date in YYYY-MM-DD format (inclusive).

    Returns:
        List of CalendarIssue dicts with an ``effective_date`` field
        set to store_date (falling back to cover_date if absent).
    """
    try:
        cv = ComicVine()
    except InvalidComicVineApiKey:
        LOGGER.warning('No CV API key set; cannot fetch calendar issues')
        return []

    try:
        all_raw_issues = run(
            cv.fetch_issues_by_date_range(start_date, end_date)
        )
    except Exception as e:
        LOGGER.error('Calendar fetch failed: %s', e)
        return []

    # Enrich with publisher info
    return _enrich_with_publishers(all_raw_issues)
```

- [ ] **Step 3.4: Run cache tests**

```bash
python -m unittest tests.Tbackend.test_volume_publisher_cache -v
```

Expected: 4 tests pass.

- [ ] **Step 3.5: Run full suite**

```bash
python -m unittest discover -s ./tests -p '*.py' 2>&1 | tail -5
```

Expected: no failures.

- [ ] **Step 3.6: Commit**

```bash
git add backend/features/calendar_cv.py tests/Tbackend/test_volume_publisher_cache.py
git commit -m "feat(calendar): add volume_publisher_cache SQLite layer to _fetch_missing_volumes"
```

---

## Task 4: Extend `RefreshCalendar` to ±30 Days

**Files:**
- Modify: `backend/features/tasks.py:759-775`
- Create: `tests/Tbackend/test_refresh_calendar.py`

- [ ] **Step 4.1: Write failing tests**

Create `tests/Tbackend/test_refresh_calendar.py`:

```python
"""
Tests for RefreshCalendar ±30-day pre-warm with week-by-week seeding.
"""
import sys
import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock, call, patch

if 'grp' not in sys.modules:
    sys.modules['grp'] = MagicMock()

from backend.features.tasks import RefreshCalendar


class TestRefreshCalendarWindow(unittest.TestCase):
    """RefreshCalendar must pre-warm ±30 days and seed each week individually."""

    def _run_with_fake_today(self, fake_today: date):
        """Run RefreshCalendar.run() with a fixed 'today' and capture calls."""
        calls = []

        def fake_get_calendar(start, end,
                               publisher_names=None, force_refresh=False):
            calls.append({'start': start, 'end': end,
                          'force_refresh': force_refresh})
            return []

        ws_mock = MagicMock()
        ws_mock.emit = MagicMock()

        with patch('backend.features.calendar.get_calendar',
                   side_effect=fake_get_calendar), \
             patch('backend.features.tasks.WebSocket',
                   return_value=ws_mock), \
             patch('backend.features.tasks.date') as mock_date_cls:

            # Make date.today() return fake_today while keeping date() constructor
            mock_date_cls.today.return_value = fake_today
            mock_date_cls.side_effect = lambda *a, **kw: date(*a, **kw)

            task = RefreshCalendar()
            task.run()

        return calls

    def test_superset_call_uses_30_day_window(self):
        """First call must be force_refresh=True with ±30-day range."""
        fake_today = date(2026, 3, 15)
        calls = self._run_with_fake_today(fake_today)

        superset = calls[0]
        self.assertTrue(superset['force_refresh'])
        self.assertEqual(superset['start'],
                         (fake_today - timedelta(days=30)).isoformat())
        self.assertEqual(superset['end'],
                         (fake_today + timedelta(days=30)).isoformat())

    def test_week_calls_use_force_refresh_false(self):
        """All week-by-week calls must use force_refresh=False (hits Kapowarr cache)."""
        calls = self._run_with_fake_today(date(2026, 3, 15))
        week_calls = calls[1:]
        self.assertTrue(len(week_calls) >= 8,
                        f'Expected ≥8 week calls, got {len(week_calls)}')
        for c in week_calls:
            self.assertFalse(c['force_refresh'],
                             f'Week call had force_refresh=True: {c}')

    def test_week_calls_cover_entire_window(self):
        """Week calls must collectively span the full ±30-day window."""
        fake_today = date(2026, 3, 15)
        calls = self._run_with_fake_today(fake_today)
        week_calls = calls[1:]

        earliest = min(c['start'] for c in week_calls)
        latest = max(c['end'] for c in week_calls)
        window_start = (fake_today - timedelta(days=30)).isoformat()
        window_end = (fake_today + timedelta(days=30)).isoformat()

        self.assertLessEqual(earliest, window_start)
        self.assertGreaterEqual(latest, window_end)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 4.2: Run tests to confirm they fail**

```bash
python -m unittest tests.Tbackend.test_refresh_calendar -v
```

Expected: `FAIL — AssertionError` on the window test (current code only uses ±7 days) and on the week-calls test (no week-by-week loop).

- [ ] **Step 4.3: Update `RefreshCalendar.run()` in `tasks.py`**

Replace lines 759–775 (the `run` method) with:

```python
    def run(self) -> List[Tuple[str, int, Union[int, None]]]:
        from backend.features.calendar import get_calendar
        today = date.today()
        window_start = (today - timedelta(days=30)).isoformat()
        window_end = (today + timedelta(days=30)).isoformat()

        self.message = 'Refreshing calendar cache'
        ws = WebSocket()
        ws.emit(TaskStatusEvent(self.message))

        # Superset fetch: seeds CVProxy and Kapowarr's in-memory cache
        # for the full ±30-day window in one request.
        get_calendar(window_start, window_end, force_refresh=True)

        # Week-by-week seeding: populates Kapowarr's per-week cache entries
        # so individual week navigation is instant after this task runs.
        # force_refresh=False means these hit the superset cache above —
        # no extra CVProxy calls are made.
        current = today - timedelta(days=30)
        while current <= today + timedelta(days=30):
            week_end = current + timedelta(days=6)
            get_calendar(current.isoformat(), week_end.isoformat())
            current += timedelta(days=7)

        self.message = 'Calendar cache refreshed'
        ws.emit(TaskStatusEvent(self.message))

        return []
```

- [ ] **Step 4.4: Run refresh calendar tests**

```bash
python -m unittest tests.Tbackend.test_refresh_calendar -v
```

Expected: 3 tests pass.

- [ ] **Step 4.5: Run full suite**

```bash
python -m unittest discover -s ./tests -p '*.py' 2>&1 | tail -5
```

Expected: no failures.

- [ ] **Step 4.6: Commit**

```bash
git add backend/features/tasks.py tests/Tbackend/test_refresh_calendar.py
git commit -m "feat(calendar): extend RefreshCalendar to ±30 days with week-by-week seeding"
```

---

## Progress Tracking

- [ ] Task 1: Parallelize `fetch_volumes_for_enrichment`
- [ ] Task 2: DB migration #50 — `volume_publisher_cache`
- [ ] Task 3: Volume→publisher SQLite cache in `_fetch_missing_volumes`
- [ ] Task 4: Extend `RefreshCalendar` to ±30 days

**Total Tasks:** 4 | **Completed:** 0 | **Remaining:** 4
