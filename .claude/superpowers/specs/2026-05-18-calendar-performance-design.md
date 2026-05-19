# Calendar Performance Fixes — Design Spec

**Date:** 2026-05-18
**Branch:** feature/calendar+connections
**Status:** Approved

---

## Background

Investigation showed the calendar `/api/calendar` endpoint has three independent performance problems:

| Scenario | Observed latency | Root cause |
|---|---|---|
| Month view, CVProxy cold | ~5,200ms | CVProxy fetches real CV API (~4–5s per request) |
| Month view, CVProxy warm but Kapowarr cache cold | ~540ms | Sequential volume enrichment batches |
| Any range after Kapowarr restart | Same as cold | In-memory cache lost; volume-publisher lookups repeat every restart |

Measurements taken against production at `https://kapowarr.genjack.net` using the `/api/calendar` endpoint with various date ranges and `force=1` to bypass Kapowarr's in-memory cache.

---

## Fix 1 — Parallelize `fetch_volumes_for_enrichment`

**File:** `backend/implementations/comicvine.py`
**Function:** `fetch_volumes_for_enrichment`

### Problem

The volume enrichment loop is sequential:

```python
for id_batch in batched(volume_ids, 100):
    result = await self.__call_api(session, '/volumes', {...})
    all_vols.extend(result.get('results', []))
```

A month view with ~885 unique volumes produces 9 batches × ~60ms/batch = ~540ms of purely sequential wait time.

### Fix

Collect all batch coroutines and dispatch them with `asyncio.gather()`, mirroring how `fetch_issues_by_date_range` handles pagination:

```python
async with AsyncSession() as session:
    tasks = [
        self.__call_api(
            session,
            '/volumes',
            {'field_list': enrichment_field_list, 'filter': 'id:{}'.format('|'.join(str(v) for v in id_batch))},
            {'results': []}
        )
        for id_batch in batched(volume_ids, 100)
    ]
    responses = await gather(*tasks)
    for resp in responses:
        all_vols.extend(resp.get('results', []))
```

No `__sleep_iter` needed: volume enrichment hits `/volumes`, not a rate-limited paginated endpoint, and batches are already capped at 100 IDs.

### Expected outcome

9-batch month: ~540ms → ~60ms (single round-trip time, all batches concurrent).

---

## Fix 2 — Extend `RefreshCalendar` Pre-warm Window

**File:** `backend/features/tasks.py`
**Class:** `RefreshCalendar`

### Problem

`RefreshCalendar` pre-warms a ±7-day (14-day) window as a single superset fetch. Two issues:

1. Users navigating to a past or future week outside ±7 days get a cold CVProxy hit (~4–5s).
2. Fetching one large superset doesn't individually prime CVProxy for the sub-ranges that users navigate to week-by-week.

### Fix

- Extend the window to **±30 days** (62-day total).
- After the superset fetch, iterate week-by-week across the window and call `get_calendar()` for each week with `force_refresh=False` (uses the superset cache — no extra CVProxy calls; just seeds Kapowarr's per-week cache entries).
- The superset fetch already seeds CVProxy for that date range. The week-by-week loop seeds Kapowarr's LRU cache so each individual week is instantly retrievable.

### Expected outcome

Any week within ±30 days of today loads from Kapowarr's in-memory cache instantly after the daily `RefreshCalendar` run.

---

## Fix 3 — Volume→Publisher SQLite Cache

**Files:**
- `backend/internals/db_migration.py` — migration #48
- `backend/features/calendar_cv.py` — `_fetch_missing_volumes`

### Problem

`_fetch_missing_volumes` calls CVProxy every time a calendar date range is cold in Kapowarr (after restart, or on `force=1`). Publisher info for a volume is stable — it changes at most once in a series' lifetime. Re-fetching it from CVProxy on every Kapowarr restart is wasteful.

### Schema

Migration #48 adds:

```sql
CREATE TABLE IF NOT EXISTS volume_publisher_cache (
    comicvine_id   INTEGER PRIMARY KEY,
    volume_name    TEXT    NOT NULL DEFAULT '',
    publisher_name TEXT,
    publisher_id   INTEGER,
    cached_at      REAL    NOT NULL  -- epoch seconds
);
```

TTL: **30 days** (constant `VOLUME_PUBLISHER_CACHE_TTL = 30 * 86400` in `calendar_cv.py`).

### Logic in `_fetch_missing_volumes`

1. **Lookup:** Query `volume_publisher_cache` for all requested IDs where `cached_at > now - TTL`. Build a `{cv_id: info}` map from hits.
2. **Gap list:** Any ID not in the hit map needs a CVProxy fetch.
3. **Fetch gaps:** Call `run(cv.fetch_volumes_for_enrichment(gap_ids))` (already parallelized by Fix 1).
4. **Upsert:** `INSERT OR REPLACE` new results into `volume_publisher_cache`.
5. **Merge:** Combine cached hits + freshly fetched results into `volume_publisher_map`.

### Expected outcome

After any month is loaded once, subsequent Kapowarr restarts load volume publisher info entirely from local SQLite — CVProxy is not contacted for volume data. A full month (~885 volumes) becomes < 5ms for the lookup.

---

## Data Flow After All Three Fixes

```
GET /api/calendar?start=S&end=E
  │
  ├─ Kapowarr in-memory cache hit?  → return in ~15ms
  │
  └─ Cache miss:
       ├─ fetch_issues_by_date_range (CVProxy /issues)
       │    ├─ page 1 (single request ~50ms)
       │    └─ pages 2..N (gather, parallel ~50ms)
       │
       └─ _fetch_missing_volumes
            ├─ SQLite lookup (volume_publisher_cache)  ~1ms
            ├─ gap IDs only → CVProxy /volumes (gather, parallel ~60ms)
            └─ upsert new rows to SQLite
```

**Warm CVProxy, all volumes cached:** ~50ms (issues only, no CVProxy volume calls)
**Warm CVProxy, volumes not yet cached:** ~110ms (issues + one parallel volume round-trip)
**Cold CVProxy (new range, real CV API):** ~4–5s (unchanged — bottleneck is the CV API itself)

---

## Database Migration

**Migration #50** adds `volume_publisher_cache`. No existing tables modified. (Migrations 47–49 are already taken by search stats, split update all, and task history duration.)

`db_migration.py` registration:
```python
@DatabaseMigrationHandler.register_handler(49)
def _migrate_add_volume_publisher_cache():
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS volume_publisher_cache (
            comicvine_id   INTEGER PRIMARY KEY,
            volume_name    TEXT    NOT NULL DEFAULT '',
            publisher_name TEXT,
            publisher_id   INTEGER,
            cached_at      REAL    NOT NULL
        );
    """)
```

---

## Testing

| Test | Method |
|---|---|
| Parallel volume fetch returns same results as sequential | Unit test comparing output of old vs new gather approach with mocked session |
| `_fetch_missing_volumes` uses cache on second call, skips CVProxy | Unit test: mock CVProxy call, call twice, assert CVProxy called once |
| Expired cache entries are re-fetched | Unit test: insert row with `cached_at = 0`, assert CVProxy called |
| Migration #48 runs cleanly on fresh and existing DB | Existing migration test pattern |
| `RefreshCalendar` generates correct week boundaries for ±30 days | Unit test: assert N week ranges covered, no overlaps, edges align |

---

## Out of Scope

- Persisting Kapowarr's in-memory LRU calendar cache to SQLite (separate, larger change)
- Streaming/WebSocket calendar loading
- CVProxy changes
