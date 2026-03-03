# -*- coding: utf-8 -*-

"""
Local ComicVine database lookup and write-through cache.

Uses a local SQLite copy of the ComicVine database (with FTS5 index)
to resolve volume titles and issue IDs without API calls. Also acts
as a write-through cache: every ComicVine API response is upserted
into the local DB so it stays up-to-date over time.

The local DB is expected at /app/db/localcv.db.
"""

import sqlite3
from datetime import date
from os.path import isfile
from re import IGNORECASE, compile
from typing import Any, Dict, List, Optional, Sequence, Union

from backend.base.definitions import (FilenameData, IssueMetadata,
                                      VolumeMetadata)
from backend.base.file_extraction import (extract_issue_number,
                                          extract_volume_number)
from backend.base.helpers import (force_range, normalise_string,
                                  normalise_year)
from backend.base.logging import LOGGER
from backend.implementations.matching import select_best_volume_result_for_file
from backend.internals.db import get_db
from backend.internals.settings import Settings

# Regex matching "Volume X" in the deck field (same as comicvine.py)
_volume_regex = compile(
    r'\b(?:(?:v(?:ol|olume))(?:\.\s|[\.\-\s])?|v)'
    r'(\d+(?:(?:\-|\s\-\s|\.\-\.)\d+)?|(?<!v)I{1,3})',
    IGNORECASE
)

# Default path inside the Docker container
LOCAL_CV_DB_PATH = '/app/db/localcv.db'


def _get_local_cv_conn() -> Optional[sqlite3.Connection]:
    """Open a read-only connection to the local CV database.

    Returns:
        Optional[sqlite3.Connection]: Connection if DB exists, else None.
    """
    if not isfile(LOCAL_CV_DB_PATH):
        return None

    try:
        conn = sqlite3.connect(
            f'file:{LOCAL_CV_DB_PATH}?mode=ro',
            uri=True,
            timeout=5.0
        )
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as e:
        LOGGER.warning('Cannot open local CV database: %s', e)
        return None


def _get_local_cv_conn_rw() -> Optional[sqlite3.Connection]:
    """Open a read-write connection to the local CV database.

    Creates the database and schema if it doesn't exist yet.

    Returns:
        Optional[sqlite3.Connection]: Connection, or None on error.
    """
    try:
        conn = sqlite3.connect(
            LOCAL_CV_DB_PATH,
            timeout=10.0
        )
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        return conn
    except sqlite3.Error as e:
        LOGGER.warning('Cannot open local CV database for writing: %s', e)
        return None


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Ensure the local CV database has all required tables and columns.

    Creates tables if missing and adds new columns (like ``deck``)
    via ALTER TABLE.
    """
    cursor = conn.cursor()

    # Create tables if they don't exist (for fresh databases)
    cursor.executescript('''
        CREATE TABLE IF NOT EXISTS cv_publisher (
            id INTEGER PRIMARY KEY,
            name TEXT,
            image_url TEXT,
            site_detail_url TEXT
        );
        CREATE TABLE IF NOT EXISTS cv_volume (
            id INTEGER PRIMARY KEY,
            name TEXT,
            aliases TEXT,
            start_year TEXT,
            publisher_id INTEGER REFERENCES cv_publisher(id),
            count_of_issues INTEGER,
            description TEXT,
            image_url TEXT,
            site_detail_url TEXT,
            deck TEXT
        );
        CREATE TABLE IF NOT EXISTS cv_issue (
            id INTEGER PRIMARY KEY,
            volume_id INTEGER NOT NULL REFERENCES cv_volume(id),
            name TEXT,
            issue_number TEXT,
            cover_date TEXT,
            store_date TEXT,
            description TEXT,
            image_url TEXT,
            site_detail_url TEXT
        );
    ''')

    # Add `deck` column to existing databases that lack it
    existing_cols = {
        row[1]
        for row in cursor.execute('PRAGMA table_info(cv_volume)').fetchall()
    }
    if 'deck' not in existing_cols:
        cursor.execute('ALTER TABLE cv_volume ADD COLUMN deck TEXT')
        LOGGER.info('Added "deck" column to cv_volume table')

    # Ensure FTS5 index exists
    cursor.execute('''
        CREATE VIRTUAL TABLE IF NOT EXISTS volume_fts
        USING fts5(name, aliases, content='cv_volume', content_rowid='id')
    ''')

    # Populate FTS index from existing data if empty (one-time rebuild)
    fts_count = cursor.execute(
        'SELECT COUNT(*) FROM volume_fts'
    ).fetchone()[0]
    vol_count = cursor.execute(
        'SELECT COUNT(*) FROM cv_volume'
    ).fetchone()[0]
    if fts_count == 0 and vol_count > 0:
        cursor.execute('''
            INSERT INTO volume_fts(rowid, name, aliases)
            SELECT id, name, COALESCE(aliases, '') FROM cv_volume
        ''')
        LOGGER.info(
            'Populated FTS index with %d existing volumes', vol_count
        )

    conn.commit()


# ---- Cache write functions ----

def cache_volumes_from_api(
    raw_volumes: List[Dict[str, Any]],
    conn: Optional[sqlite3.Connection] = None
) -> None:
    """Upsert raw ComicVine API volume data into the local cache.

    Call this after any CV API call that returns volume data
    (``fetch_volume``, ``fetch_volumes``, ``search_volumes``).

    Args:
        raw_volumes: List of raw API result dicts (pre-formatting).
        conn: Optional existing RW connection to reuse. When provided,
            the caller is responsible for closing the connection.
    """
    if not raw_volumes:
        return

    owns_conn = conn is None
    if owns_conn:
        conn = _get_local_cv_conn_rw()
    if conn is None:
        return

    try:
        cursor = conn.cursor()

        # Upsert publishers first
        publishers = {}
        for v in raw_volumes:
            pub = v.get('publisher') or {}
            pub_id = pub.get('id')
            pub_name = pub.get('name')
            if pub_id and pub_name:
                publishers[int(pub_id)] = pub_name

        if publishers:
            cursor.executemany(
                '''INSERT INTO cv_publisher (id, name)
                   VALUES (?, ?)
                   ON CONFLICT(id) DO UPDATE SET name = excluded.name''',
                list(publishers.items())
            )

        # Upsert volumes
        volume_rows = []
        for v in raw_volumes:
            pub = v.get('publisher') or {}
            pub_id = pub.get('id')
            image = v.get('image') or {}
            image_url = image.get('small_url') or image.get('original_url', '')

            volume_rows.append((
                int(v['id']),
                v.get('name', ''),
                v.get('aliases') or '',
                v.get('start_year') or '',
                int(pub_id) if pub_id else None,
                int(v.get('count_of_issues') or 0),
                v.get('description') or '',
                image_url,
                v.get('site_detail_url') or '',
                v.get('deck') or ''
            ))

        cursor.executemany(
            '''INSERT INTO cv_volume
                   (id, name, aliases, start_year, publisher_id,
                    count_of_issues, description, image_url,
                    site_detail_url, deck)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   name = excluded.name,
                   aliases = excluded.aliases,
                   start_year = excluded.start_year,
                   publisher_id = excluded.publisher_id,
                   count_of_issues = excluded.count_of_issues,
                   description = excluded.description,
                   image_url = excluded.image_url,
                   site_detail_url = excluded.site_detail_url,
                   deck = excluded.deck''',
            volume_rows
        )

        # Update FTS index for upserted volumes
        for row in volume_rows:
            vol_id, name, aliases = row[0], row[1], row[2]
            # Delete old FTS entry, then insert new one
            cursor.execute(
                "DELETE FROM volume_fts WHERE rowid = ?", (vol_id,)
            )
            cursor.execute(
                "INSERT INTO volume_fts (rowid, name, aliases) VALUES (?, ?, ?)",
                (vol_id, name, aliases)
            )

        conn.commit()
        LOGGER.debug('Cached %d volumes to local CV DB', len(volume_rows))

    except sqlite3.Error as e:
        LOGGER.warning('Failed to cache volumes: %s', e)

    finally:
        if owns_conn:
            conn.close()


def cache_issues_from_api(
    raw_issues: List[Dict[str, Any]],
    conn: Optional[sqlite3.Connection] = None
) -> None:
    """Upsert raw ComicVine API issue data into the local cache.

    Call this after any CV API call that returns issue data
    (``fetch_issues``).

    Args:
        raw_issues: List of raw API result dicts (pre-formatting).
        conn: Optional existing RW connection to reuse. When provided,
            the caller is responsible for closing the connection.
    """
    if not raw_issues:
        return

    owns_conn = conn is None
    if owns_conn:
        conn = _get_local_cv_conn_rw()
    if conn is None:
        return

    try:
        cursor = conn.cursor()

        issue_rows = []
        for i in raw_issues:
            volume = i.get('volume') or {}
            vol_id = volume.get('id')
            if not vol_id:
                continue

            issue_rows.append((
                int(i['id']),
                int(vol_id),
                i.get('name') or '',
                i.get('issue_number') or '',
                i.get('cover_date') or '',
                i.get('store_date') or '',
                i.get('description') or ''
            ))

        if issue_rows:
            cursor.executemany(
                '''INSERT INTO cv_issue
                       (id, volume_id, name, issue_number,
                        cover_date, store_date, description)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       volume_id = excluded.volume_id,
                       name = excluded.name,
                       issue_number = excluded.issue_number,
                       cover_date = excluded.cover_date,
                       store_date = excluded.store_date,
                       description = excluded.description''',
                issue_rows
            )

        conn.commit()
        LOGGER.debug('Cached %d issues to local CV DB', len(issue_rows))

    except sqlite3.Error as e:
        LOGGER.warning('Failed to cache issues: %s', e)

    finally:
        if owns_conn:
            conn.close()


def _format_local_volume(row: sqlite3.Row,
                         publisher_name: Optional[str] = None
                         ) -> VolumeMetadata:
    """Convert a local DB row into VolumeMetadata format.

    Args:
        row: sqlite3.Row from cv_volume table.
        publisher_name: Pre-fetched publisher name, or None.

    Returns:
        VolumeMetadata compatible dict.
    """
    name = normalise_string(row['name'] or '')
    year = normalise_year(str(row['start_year'] or ''))
    aliases_raw = row['aliases'] or ''
    aliases = [
        a.strip()
        for a in aliases_raw.replace('\r\n', '\n').split('\n')
        if a.strip()
    ]

    # Extract volume number from deck field (same logic as comicvine.py)
    volume_number = 1
    try:
        deck = row['deck'] or ''
    except (IndexError, KeyError):
        deck = ''
    if deck:
        vol_match = _volume_regex.search(deck)
        if vol_match:
            parsed = force_range(extract_volume_number(vol_match.group(1)))[0]
            if parsed is not None:
                volume_number = parsed

    result: VolumeMetadata = {
        'comicvine_id': int(row['id']),
        'title': name,
        'year': year,
        'volume_number': volume_number,
        'cover_link': row['image_url'] or '',
        'cover': None,
        'description': row['description'] or '',
        'site_url': row['site_detail_url'] or '',
        'aliases': aliases,
        'publisher': publisher_name,
        'issue_count': int(row['count_of_issues'] or 0),
        'translated': False,
        'already_added': None,
        'issues': None
    }
    return result


def _format_local_issue(row: sqlite3.Row,
                        date_type: str = 'cover_date'
                        ) -> IssueMetadata:
    """Convert a local DB cv_issue row into IssueMetadata format.

    Args:
        row: sqlite3.Row from cv_issue table.
        date_type: Which date column to use ('cover_date' or 'store_date').

    Returns:
        IssueMetadata compatible dict.
    """
    issue_number = (row['issue_number'] or '').replace('/', '-').strip()
    calculated = force_range(extract_issue_number(issue_number))[0]
    if calculated is None:
        calculated = 0.0

    return {
        'comicvine_id': int(row['id']),
        'volume_id': int(row['volume_id']),
        'issue_number': issue_number,
        'calculated_issue_number': calculated,
        'title': normalise_string(row['name'] or '') or None,
        'date': row[date_type] or None,
        'description': row['description'] or ''
    }


def fetch_volume_local(cv_id: int) -> Optional[VolumeMetadata]:
    """Fetch full volume metadata + issues from the local CV database.

    Returns VolumeMetadata with populated ``issues`` list, suitable for
    passing directly to ``Library.add_from_local()``. No API calls.

    Args:
        cv_id: The numeric ComicVine volume ID.

    Returns:
        VolumeMetadata dict with issues, or None if not found / DB unavailable.
    """
    conn = _get_local_cv_conn()
    if conn is None:
        return None

    try:
        cursor = conn.cursor()

        # Fetch volume row
        cursor.execute(
            '''SELECT id, name, aliases, start_year, publisher_id,
                      count_of_issues, description, image_url,
                      site_detail_url, deck
               FROM cv_volume WHERE id = ?''',
            (cv_id,)
        )
        vol_row = cursor.fetchone()
        if vol_row is None:
            return None

        # Publisher name
        pub_name = None
        if vol_row['publisher_id']:
            cursor.execute(
                'SELECT name FROM cv_publisher WHERE id = ?',
                (vol_row['publisher_id'],)
            )
            pub_row = cursor.fetchone()
            if pub_row:
                pub_name = pub_row['name']

        vd = _format_local_volume(vol_row, pub_name)

        # Fetch issues
        date_type = Settings().sv.date_type.value
        cursor.execute(
            '''SELECT id, volume_id, name, issue_number,
                      cover_date, store_date, description
               FROM cv_issue WHERE volume_id = ?
               ORDER BY id''',
            (cv_id,)
        )
        vd['issues'] = [
            _format_local_issue(r, date_type)
            for r in cursor.fetchall()
        ]

        return vd

    except sqlite3.Error as e:
        LOGGER.warning('fetch_volume_local(%d) failed: %s', cv_id, e)
        return None

    finally:
        conn.close()


def search_volumes_local(query: str,
                         limit: int = 50
                         ) -> Optional[List[VolumeMetadata]]:
    """Search for volumes by title using the local FTS5 index.

    Args:
        query: The series title to search for.
        limit: Maximum results to return.

    Returns:
        List of VolumeMetadata dicts, or None if local DB unavailable.
    """
    conn = _get_local_cv_conn()
    if conn is None:
        return None

    try:
        cursor = conn.cursor()

        # Quote the query to handle special chars (hyphens, etc.)
        quoted = '"' + query.replace('"', '""') + '"'

        cursor.execute('''
            SELECT v.id, v.name, v.aliases, v.start_year,
                   v.publisher_id, v.count_of_issues,
                   v.description, v.image_url, v.site_detail_url,
                   v.deck
            FROM volume_fts f
            JOIN cv_volume v ON f.rowid = v.id
            WHERE volume_fts MATCH ?
            LIMIT ?
        ''', (quoted, limit))

        rows = cursor.fetchall()
        if not rows:
            return []

        # Batch-fetch publisher names
        publisher_ids = {r['publisher_id'] for r in rows if r['publisher_id']}
        publisher_map: Dict[int, str] = {}
        if publisher_ids:
            placeholders = ','.join('?' for _ in publisher_ids)
            cursor.execute(
                f'SELECT id, name FROM cv_publisher '
                f'WHERE id IN ({placeholders})',
                list(publisher_ids)
            )
            publisher_map = {r['id']: r['name'] for r in cursor.fetchall()}

        # Mark which volumes are already added in Kapowarr
        kapowarr_db = get_db()
        cv_ids = [r['id'] for r in rows]
        placeholders = ','.join('?' for _ in cv_ids)
        already_added: Dict[int, int] = dict(kapowarr_db.execute(
            f'SELECT comicvine_id, id FROM volumes '
            f'WHERE comicvine_id IN ({placeholders}) LIMIT ?',
            tuple(cv_ids) + (limit,)
        ))

        results = []
        for row in rows:
            pub_name = publisher_map.get(row['publisher_id'])
            vol = _format_local_volume(row, pub_name)
            vol['already_added'] = already_added.get(vol['comicvine_id'])
            results.append(vol)

        return results

    except sqlite3.Error as e:
        LOGGER.warning('Local CV search failed for "%s": %s', query, e)
        return None

    finally:
        conn.close()


def resolve_issue_ids_local(
    issue_ids: Sequence[int]
) -> Optional[Dict[int, int]]:
    """Resolve CV issue IDs to their parent volume IDs using local DB.

    Args:
        issue_ids: Sequence of numeric CV issue IDs.

    Returns:
        Dict mapping issue_id -> volume_id, or None if local DB unavailable.
        Issues not found in the local DB are omitted from the dict.
    """
    conn = _get_local_cv_conn()
    if conn is None:
        return None

    try:
        cursor = conn.cursor()
        result: Dict[int, int] = {}

        # SQLite has a limit on variables, batch in groups of 500
        batch_size = 500
        ids = list(issue_ids)
        for i in range(0, len(ids), batch_size):
            batch = ids[i:i + batch_size]
            placeholders = ','.join('?' for _ in batch)
            cursor.execute(
                f'SELECT id, volume_id FROM cv_issue '
                f'WHERE id IN ({placeholders})',
                batch
            )
            for row in cursor.fetchall():
                result[row['id']] = row['volume_id']

        return result

    except sqlite3.Error as e:
        LOGGER.warning('Local CV issue ID lookup failed: %s', e)
        return None

    finally:
        conn.close()


def fetch_volumes_local(
    cv_ids: Sequence[int]
) -> Optional[Dict[int, VolumeMetadata]]:
    """Fetch volume metadata from local DB by CV IDs.

    Args:
        cv_ids: Sequence of numeric CV volume IDs.

    Returns:
        Dict mapping volume_id -> VolumeMetadata, or None if DB unavailable.
    """
    conn = _get_local_cv_conn()
    if conn is None:
        return None

    try:
        cursor = conn.cursor()
        result: Dict[int, VolumeMetadata] = {}

        batch_size = 500
        ids = list(cv_ids)
        all_rows = []

        for i in range(0, len(ids), batch_size):
            batch = ids[i:i + batch_size]
            placeholders = ','.join('?' for _ in batch)
            cursor.execute(
                f'SELECT id, name, aliases, start_year, publisher_id, '
                f'count_of_issues, description, image_url, '
                f'site_detail_url, deck '
                f'FROM cv_volume WHERE id IN ({placeholders})',
                batch
            )
            all_rows.extend(cursor.fetchall())

        # Batch-fetch publisher names
        publisher_ids = {r['publisher_id'] for r in all_rows
                         if r['publisher_id']}
        publisher_map: Dict[int, str] = {}
        if publisher_ids:
            placeholders = ','.join('?' for _ in publisher_ids)
            cursor.execute(
                f'SELECT id, name FROM cv_publisher '
                f'WHERE id IN ({placeholders})',
                list(publisher_ids)
            )
            publisher_map = {r['id']: r['name'] for r in cursor.fetchall()}

        for row in all_rows:
            pub_name = publisher_map.get(row['publisher_id'])
            vol = _format_local_volume(row, pub_name)
            result[vol['comicvine_id']] = vol

        return result

    except sqlite3.Error as e:
        LOGGER.warning('Local CV volume fetch failed: %s', e)
        return None

    finally:
        conn.close()


def fetch_issues_local(
    volume_cv_ids: Sequence[int]
) -> Optional[List[IssueMetadata]]:
    """Fetch issue metadata for multiple volumes from the local CV database.

    This is the local equivalent of ``ComicVine.fetch_issues()``.

    Args:
        volume_cv_ids: Sequence of numeric CV volume IDs.

    Returns:
        List of IssueMetadata dicts, or None if local DB unavailable.
    """
    conn = _get_local_cv_conn()
    if conn is None:
        return None

    try:
        cursor = conn.cursor()
        date_type = Settings().sv.date_type.value
        result: List[IssueMetadata] = []

        batch_size = 500
        ids = list(volume_cv_ids)
        for i in range(0, len(ids), batch_size):
            batch = ids[i:i + batch_size]
            placeholders = ','.join('?' for _ in batch)
            cursor.execute(
                f'SELECT id, volume_id, name, issue_number, '
                f'cover_date, store_date, description '
                f'FROM cv_issue WHERE volume_id IN ({placeholders}) '
                f'ORDER BY id',
                batch
            )
            result.extend(
                _format_local_issue(row, date_type)
                for row in cursor.fetchall()
            )

        return result

    except sqlite3.Error as e:
        LOGGER.warning('fetch_issues_local failed: %s', e)
        return None

    finally:
        conn.close()


def local_filenames_to_cvs(
    file_groups: Dict[int, Dict[str, FilenameData]],
    only_english: bool
) -> Dict[int, Dict[str, Any]]:
    """Match groups of filenames to CV volumes using the local database.

    This is the local equivalent of ComicVine.filenames_to_cvs(). Groups
    that cannot be resolved locally are returned with id=None so the
    caller knows to fall back to the remote API.

    Uses a single DB connection for all FTS queries to avoid thousands
    of connection open/close cycles on NAS-mounted storage.

    Args:
        file_groups: Mapping from group number to a mapping of filename
            to FilenameData.
        only_english: Only match to english volumes.

    Returns:
        Dict mapping group number to its CV match dict. Groups that
        could not be matched locally have id=None.
    """
    no_match: Dict[str, Any] = {
        'id': None,
        'title': None,
        'issue_count': None,
        'link': None
    }

    # Collect unique series titles -> group numbers
    titles_to_groups: Dict[str, List[int]] = {}
    for group_number, file_group in file_groups.items():
        series_name = next(iter(file_group.values()))['series'].lower()
        titles_to_groups.setdefault(series_name, []).append(group_number)

    LOGGER.info(
        f'Local CV batch search: {len(titles_to_groups)} unique titles '
        f'from {len(file_groups)} groups'
    )

    conn = _get_local_cv_conn()
    if conn is None:
        return {gn: no_match.copy() for gn in file_groups}

    try:
        cursor = conn.cursor()

        # Batch FTS search for all titles using a single connection
        titles_to_results: Dict[str, List[VolumeMetadata]] = {}
        all_cv_ids: List[int] = []
        all_publisher_ids: set = set()
        # Store raw rows per title for post-processing
        titles_to_raw_rows: Dict[str, list] = {}

        for title in titles_to_groups:
            quoted = '"' + title.replace('"', '""') + '"'
            try:
                cursor.execute('''
                    SELECT v.id, v.name, v.aliases, v.start_year,
                           v.publisher_id, v.count_of_issues,
                           v.description, v.image_url,
                           v.site_detail_url, v.deck
                    FROM volume_fts f
                    JOIN cv_volume v ON f.rowid = v.id
                    WHERE volume_fts MATCH ?
                    LIMIT 50
                ''', (quoted,))
                rows = cursor.fetchall()
            except sqlite3.Error:
                rows = []

            titles_to_raw_rows[title] = rows
            for row in rows:
                all_cv_ids.append(row['id'])
                if row['publisher_id']:
                    all_publisher_ids.add(row['publisher_id'])

        # Batch-fetch all publishers in one query
        publisher_map: Dict[int, str] = {}
        if all_publisher_ids:
            pub_ids = list(all_publisher_ids)
            for i in range(0, len(pub_ids), 500):
                batch = pub_ids[i:i + 500]
                placeholders = ','.join('?' for _ in batch)
                cursor.execute(
                    f'SELECT id, name FROM cv_publisher '
                    f'WHERE id IN ({placeholders})',
                    batch
                )
                for row in cursor.fetchall():
                    publisher_map[row['id']] = row['name']

        # Batch-check already_added in Kapowarr DB
        already_added: Dict[int, int] = {}
        if all_cv_ids:
            kapowarr_db = get_db()
            for i in range(0, len(all_cv_ids), 500):
                batch = all_cv_ids[i:i + 500]
                placeholders = ','.join('?' for _ in batch)
                rows = kapowarr_db.execute(
                    f'SELECT comicvine_id, id FROM volumes '
                    f'WHERE comicvine_id IN ({placeholders})',
                    tuple(batch)
                )
                already_added.update(dict(rows))

        # Build VolumeMetadata results from raw rows
        for title, raw_rows in titles_to_raw_rows.items():
            results = []
            for row in raw_rows:
                pub_name = publisher_map.get(row['publisher_id'])
                vol = _format_local_volume(row, pub_name)
                vol['already_added'] = already_added.get(vol['comicvine_id'])
                results.append(vol)
            titles_to_results[title] = results

        # Match groups to best result
        matches: Dict[int, Dict[str, Any]] = {}
        for title, group_numbers in titles_to_groups.items():
            search_results = titles_to_results.get(title, [])

            for group_number in group_numbers:
                if not search_results:
                    matches[group_number] = no_match.copy()
                    continue

                result = select_best_volume_result_for_file(
                    file_groups[group_number],
                    search_results,
                    only_english=only_english
                )

                if result is None:
                    matches[group_number] = no_match.copy()
                else:
                    matches[group_number] = {
                        'id': result['comicvine_id'],
                        'title': f"{result['title']} ({result['year']})",
                        'issue_count': result['issue_count'],
                        'link': result['site_url']
                    }

        return matches

    except sqlite3.Error as e:
        LOGGER.warning('Local CV batch search failed: %s', e)
        return {gn: no_match.copy() for gn in file_groups}

    finally:
        conn.close()


def local_resolve_groups_by_issue_ids(
    groups_to_issue_ids: Dict[int, int],
    only_english: bool
) -> Dict[int, Dict[str, Any]]:
    """Resolve file groups via CV issue IDs using the local database.

    This is the local equivalent of ComicVine.resolve_groups_by_issue_ids().

    Args:
        groups_to_issue_ids: Mapping from group number to CV issue ID.
        only_english: Only match to english volumes.

    Returns:
        Dict mapping group number to its CV match dict.
    """
    no_match: Dict[str, Any] = {
        'id': None,
        'title': None,
        'issue_count': None,
        'link': None
    }

    unique_issue_ids = list(set(groups_to_issue_ids.values()))

    # Resolve issue IDs to volume IDs
    issue_to_volume = resolve_issue_ids_local(unique_issue_ids)
    if issue_to_volume is None:
        # Local DB unavailable — return all as unmatched
        return {gn: no_match.copy() for gn in groups_to_issue_ids}

    # Fetch volume metadata
    unique_volume_ids = list(set(issue_to_volume.values()))
    volume_metadata = fetch_volumes_local(unique_volume_ids)
    if volume_metadata is None:
        return {gn: no_match.copy() for gn in groups_to_issue_ids}

    # Build results
    matches: Dict[int, Dict[str, Any]] = {}
    for group_number, issue_id in groups_to_issue_ids.items():
        volume_id = issue_to_volume.get(issue_id)
        if volume_id is None:
            matches[group_number] = no_match.copy()
            continue

        vol = volume_metadata.get(volume_id)
        if vol is None:
            matches[group_number] = no_match.copy()
            continue

        # Filter non-english if requested
        if only_english and vol.get('translated', False):
            matches[group_number] = no_match.copy()
            continue

        matches[group_number] = {
            'id': vol['comicvine_id'],
            'title': f"{vol['title']} ({vol['year']})",
            'issue_count': vol['issue_count'],
            'link': vol['site_url']
        }

    return matches


def fetch_issues_by_date_range_local(
    start_date: str,
    end_date: str
) -> Optional[List[Dict[str, Any]]]:
    """Fetch raw issue data from local cache for a store_date range.

    Queries only by store_date (the actual shelf date).

    Args:
        start_date: Start of range in YYYY-MM-DD format (inclusive).
        end_date: End of range in YYYY-MM-DD format (inclusive).

    Returns:
        List of raw-style dicts suitable for ``_enrich_with_publishers``,
        or None if the local DB is unavailable.
    """
    conn = _get_local_cv_conn()
    if conn is None:
        return None

    try:
        cursor = conn.cursor()

        # Query issues by store_date only
        cursor.execute('''
            SELECT i.id, i.volume_id, i.name, i.issue_number,
                   i.cover_date, i.store_date, i.description,
                   i.image_url, i.site_detail_url
            FROM cv_issue i
            WHERE i.store_date BETWEEN ? AND ?
            ORDER BY i.store_date ASC
        ''', (start_date, end_date))

        rows = cursor.fetchall()
        if not rows:
            return []

        # Fetch volume info for all matched issues
        vol_ids = list({r['volume_id'] for r in rows})
        vol_map: Dict[int, Dict[str, Any]] = {}
        for i in range(0, len(vol_ids), 500):
            batch = vol_ids[i:i + 500]
            placeholders = ','.join('?' for _ in batch)
            cursor.execute(
                f'SELECT id, name FROM cv_volume WHERE id IN ({placeholders})',
                batch
            )
            for vr in cursor.fetchall():
                vol_map[vr['id']] = {'id': vr['id'], 'name': vr['name']}

        # Build raw-style dicts matching the API format expected by
        # calendar_cv._enrich_with_publishers()
        result: List[Dict[str, Any]] = []
        seen_ids: set[int] = set()
        for row in rows:
            iid = int(row['id'])
            if iid in seen_ids:
                continue
            seen_ids.add(iid)

            vol_info = vol_map.get(row['volume_id'], {})
            result.append({
                'id': iid,
                'volume': {
                    'id': row['volume_id'],
                    'name': vol_info.get('name', '')
                },
                'issue_number': row['issue_number'] or '',
                'name': row['name'] or None,
                'cover_date': row['cover_date'] or None,
                'store_date': row['store_date'] or None,
                'description': row['description'] or None,
                'person_credits': [],  # Not stored locally
                'image': {
                    'small_url': row['image_url'] or ''
                },
                'site_detail_url': row['site_detail_url'] or ''
            })

        return result

    except sqlite3.Error as e:
        LOGGER.warning('fetch_issues_by_date_range_local failed: %s', e)
        return None

    finally:
        conn.close()


def is_date_range_cache_complete(
    start_date: str,
    end_date: str,
    n_sigma: float = 2.0
) -> bool:
    """Return True if all months in the date range appear fully cached locally.

    Computes the mean and standard deviation of per-month issue counts from
    well-established historical months (at least 2 months old, sane dates),
    then checks that every month overlapping the requested range has a count
    at or above ``mean - n_sigma * stdev``.  Months below that threshold are
    still filling in and should be fetched from the API instead.

    Args:
        start_date: Range start in YYYY-MM-DD format.
        end_date:   Range end in YYYY-MM-DD format.
        n_sigma:    How many standard deviations below the mean is still
                    considered complete.  Default 2.0.

    Returns:
        True if every month in the range is confidently complete.
    """
    conn = _get_local_cv_conn()
    if conn is None:
        return False

    try:
        cursor = conn.cursor()
        # Fetch per-month counts from stable historical months only:
        # - at least 2 months old (recent months are still filling in)
        # - sane date range (exclude CV data-entry errors like year 2121)
        rows = cursor.execute("""
            SELECT substr(store_date, 1, 7) AS month, COUNT(*) AS cnt
            FROM cv_issue
            WHERE store_date IS NOT NULL
              AND store_date != ''
              AND store_date < date('now', '-2 months')
              AND store_date > '2000-01-01'
              AND store_date < '2030-01-01'
            GROUP BY month
        """).fetchall()

        if len(rows) < 6:
            # Not enough historical data to make a reliable estimate
            return False

        counts = [r['cnt'] for r in rows]
        mean = sum(counts) / len(counts)
        variance = sum((c - mean) ** 2 for c in counts) / len(counts)
        stdev = variance ** 0.5
        threshold = mean - n_sigma * stdev

        month_counts = {r['month']: r['cnt'] for r in rows}

        # Walk every calendar month that overlaps the requested range
        try:
            start = date.fromisoformat(start_date)
            end = date.fromisoformat(end_date)
        except ValueError:
            return False

        current = date(start.year, start.month, 1)
        while current <= end:
            month_str = current.strftime('%Y-%m')
            count = month_counts.get(month_str, 0)
            if count < threshold:
                LOGGER.debug(
                    'Cache completeness: %s has %d issues (threshold %.0f) '
                    '— treating as incomplete',
                    month_str, count, threshold
                )
                return False
            # Advance to next month
            if current.month == 12:
                current = date(current.year + 1, 1, 1)
            else:
                current = date(current.year, current.month + 1, 1)

        LOGGER.debug(
            'Cache completeness: all months in %s–%s are complete '
            '(mean=%.0f, stdev=%.0f, threshold=%.0f)',
            start_date, end_date, mean, stdev, threshold
        )
        return True

    except sqlite3.Error as e:
        LOGGER.warning('is_date_range_cache_complete failed: %s', e)
        return False

    finally:
        conn.close()


def is_local_cv_available() -> bool:
    """Check if the local ComicVine database is available.

    Returns:
        bool: True if the database file exists and is readable.
    """
    return isfile(LOCAL_CV_DB_PATH)
