# Notification Field Selection — Design Spec

**Date:** 2026-05-18
**Branch:** feature/calendar+connections
**Status:** Approved

---

## Background

Kapowarr's notification system (Discord, Webhook, Custom Script, Apprise, Prowl) currently
sends fixed payloads — all providers use a plain title + body template with no user control
over what data appears. Radarr/Sonarr offer per-connection, per-event-type field selection
for Discord (rich embeds) and webhooks. This feature brings the same capability to Kapowarr,
extended to all providers.

---

## Goals

1. Per-connection, per-event-type field selection — each connection stores its own field
   config in its existing `settings` JSON blob (no DB migration required).
2. Discord gets a full rewrite: drops Apprise, calls the Discord webhook API directly to
   send rich embeds (thumbnail, named fields, links).
3. All other providers respect field selection: webhook trims payload keys, custom script
   omits env vars, Apprise/Prowl format body text from selected fields only.
4. Writer/Artist/Cover are best-effort CV-enriched fields — fetched from CVProxy at
   notification time if selected and `issue_comicvine_id` is available; omitted silently
   if CVProxy is unreachable or the field is not selected.
5. Advanced UI toggle (Radarr-style) shows/hides all field selector rows in the
   notification edit modal.

---

## Field Definitions

Defined in `notifications.py` as `FIELDS_BY_EVENT: Dict[str, List[FieldDef]]`.
Each `FieldDef` is a `TypedDict` with:
- `key: str` — machine key stored in settings (e.g. `"publisher"`)
- `label: str` — UI display name (e.g. `"Publisher"`)
- `requires_cv: bool` — True if the field needs a CVProxy lookup at dispatch time

### `download` fields

| key | label | requires_cv | Default on |
|---|---|---|---|
| `series` | Series | no | yes |
| `issue_number` | Issue Number | no | yes |
| `issue_title` | Issue Title | no | yes |
| `year` | Year | no | yes |
| `publisher` | Publisher | no | yes |
| `writer` | Writer | **yes** | no |
| `artist` | Artist | **yes** | no |
| `cover` | Cover | **yes** | no |
| `links` | Links | no | yes |
| `download_source` | Download Source | no | yes |
| `file_name` | File Name | no | no |
| `is_upgrade` | Is Upgrade | no | no |

### `volume_add` fields

| key | label | requires_cv | Default on |
|---|---|---|---|
| `series` | Series | no | yes |
| `year` | Year | no | yes |
| `publisher` | Publisher | no | yes |
| `cover` | Cover | **yes** | no |
| `links` | Links | no | yes |

### `health_check` fields

| key | label | requires_cv | Default on |
|---|---|---|---|
| `level` | Level | no | yes |
| `check_type` | Check Type | no | yes |
| `message` | Message | no | yes |

### `app_update` fields

| key | label | requires_cv | Default on |
|---|---|---|---|
| `previous_version` | Previous Version | no | yes |
| `new_version` | New Version | no | yes |
| `message` | Message | no | yes |

---

## Storage

No DB migration required. Field selections live inside each connection's existing
`settings` JSON column. New keys added to the settings dict:

```json
{
  "webhook_url": "https://discord.com/api/webhooks/...",
  "download_fields": ["series", "issue_number", "year", "publisher", "links"],
  "volume_add_fields": ["series", "year", "publisher", "links"],
  "health_check_fields": ["level", "check_type", "message"],
  "app_update_fields": ["previous_version", "new_version", "message"]
}
```

If any `*_fields` key is absent (existing connections), the provider falls back to
all non-CV default fields for that event type. This makes the change fully backwards
compatible with existing stored connections.

---

## CV Enrichment

New module: `backend/features/notifications_cv.py`

Single public function:

```python
def fetch_notification_cv_data(issue_comicvine_id: int) -> Dict[str, Any]:
    """Fetch writer, artist, cover URL for a CV issue.

    Returns dict with keys: writer, artist, cover_url.
    Any key may be None if unavailable.
    Returns all-None dict on any error (best-effort).
    """
```

Implementation:
- Calls `ComicVine().fetch_issue_detail(issue_comicvine_id)` — a new async method on
  the ComicVine class that GETs `/issue/4000-{id}` with
  `field_list=person_credits,image`.
- Parses `person_credits` list: finds first entry with role containing `"Writer"` →
  `writer`, first with `"Penciler"` or `"Artist"` → `artist`.
- `cover_url` = `image.small_url` or `image.medium_url`.
- Any exception → returns `{'writer': None, 'artist': None, 'cover_url': None}`.

Called from providers only when at least one CV field (`writer`, `artist`, `cover`) is
in the connection's selected fields and `issue_comicvine_id` is not None.

---

## Architecture

### `notifications.py` additions

```python
class FieldDef(TypedDict):
    key: str
    label: str
    requires_cv: bool

FIELDS_BY_EVENT: Dict[str, List[FieldDef]] = {
    'download': [...],
    'volume_add': [...],
    'health_check': [...],
    'app_update': [...],
}

DEFAULT_FIELDS: Dict[str, List[str]] = {
    'download': ['series', 'issue_number', 'issue_title', 'year',
                 'publisher', 'links', 'download_source'],
    'volume_add': ['series', 'year', 'publisher', 'links'],
    'health_check': ['level', 'check_type', 'message'],
    'app_update': ['previous_version', 'new_version', 'message'],
}

def get_selected_fields(settings: Dict, event_key: str) -> List[str]:
    """Return selected field keys for an event, falling back to defaults."""
    key = f'{event_key}_fields'
    return settings.get(key) or DEFAULT_FIELDS[event_key]
```

The `NotificationProvider` ABC gains no new abstract methods — providers continue to
implement `on_download`, `on_volume_add`, etc. The field selection is passed into
those methods via the existing `settings` dict, which providers read using
`get_selected_fields()`.

### Discord provider rewrite (`discord_apprise.py` → `discord.py`)

- No longer uses Apprise. Calls Discord webhook API directly via `requests`.
- Builds a Discord embed dict per event type using only the selected fields.
- Field rendering map per event type:
  - `series` → embed title (with CV `site_detail_url` as title URL if `links` selected)
  - `issue_number`, `issue_title`, `year`, `publisher`, `download_source`, `is_upgrade`,
    `file_name` → inline embed field (name: label, value: data, inline: True)
  - `writer`, `artist` → inline embed field (fetched from CV enrichment)
  - `cover` → `embed.thumbnail.url` (not a named field, just sets the thumbnail image)
  - `links` → single embed field "Links" with markdown hyperlinks to CV page
- Embed colour: blue (`0x1E90FF`) for downloads/adds, yellow (`0xFFAA00`) for health
  warnings, red (`0xFF4444`) for health errors, grey (`0x888888`) for app updates.
- Provider key remains `'discord'` in `provider_registry`.

### Webhook provider (`webhook.py`)

`on_download` builds payload from selected fields only:

```python
selected = get_selected_fields(settings, 'download')
payload = {**self._common_base('Download')}
if 'series' in selected:
    payload['seriesTitle'] = event.volume_title
if 'publisher' in selected:
    payload['publisher'] = event.publisher  # from DownloadEvent
# ... etc
```

CV fields included only if selected and cv_data available.

### Custom script provider (`custom_script.py`)

Same pattern — only emit `kapowarr_*` env vars for selected keys.

### Apprise / Prowl providers

`AppriseNotificationProvider._send_body()` helper assembles a multiline text body
from selected fields. Subclasses call this instead of `format_*_notification()`.
The old standalone `format_*_notification()` functions in `notifications.py` are
removed (they are only called by Apprise-based providers).

---

## DownloadEvent enrichment

`DownloadEvent` currently lacks `publisher`. The post-processing code that fires
`DownloadEvent` has the volume's local DB row available. Add `publisher: str` field
to `DownloadEvent` and populate it from the `volumes` table at dispatch time.

---

## Frontend

### `settings_notifications.js`

- On modal open, fetch `FIELDS_BY_EVENT` from a new API endpoint
  `GET /api/notifications/fields` (returns the Python dict serialised as JSON).
- Render an "Advanced" toggle button in the modal footer area (left of Test/Cancel/Save).
- When toggled on: show one multi-select row per event type that the connection has enabled
  (`on_download`, `on_volume_add`, etc.), labelled in orange.
- Multi-select widget: a row of chips showing selected field labels, clicking opens a
  dropdown checkbox list. "Select All" / "Deselect All" buttons in the dropdown.
- CV-enriched fields (`requires_cv: true`) shown with a `*` suffix in the dropdown label
  and a footnote: `* Requires CVProxy lookup at notification time`.
- On Save: selected keys written into `settings.download_fields`,
  `settings.volume_add_fields`, etc.

### `settings_notifications.html`

Minor: add Advanced toggle button markup.

### `frontend/api.py`

New endpoint:
```python
@api.route('/notifications/fields', methods=['GET'])
@auth
def get_notification_fields():
    from backend.features.notifications import FIELDS_BY_EVENT
    return jsonify({'result': FIELDS_BY_EVENT, 'code': 200})
```

---

## Files Modified / Created

| File | Change |
|---|---|
| `backend/features/notifications.py` | Add `FieldDef`, `FIELDS_BY_EVENT`, `DEFAULT_FIELDS`, `get_selected_fields()` |
| `backend/features/notifications_cv.py` | **New** — CV enrichment helper |
| `backend/implementations/comicvine.py` | Add `fetch_issue_detail(issue_cv_id)` async method |
| `backend/implementations/notification_providers/discord.py` | **New** — full Discord rewrite (replaces `discord_apprise.py`) |
| `backend/implementations/notification_providers/discord_apprise.py` | Deleted |
| `backend/implementations/notification_providers/webhook.py` | Add field-filtered payload building |
| `backend/implementations/notification_providers/custom_script.py` | Add field-filtered env var building |
| `backend/implementations/notification_providers/apprise_generic.py` | Use field-selected body |
| `backend/implementations/notification_providers/prowl_apprise.py` | Use field-selected body |
| `backend/features/post_processing.py` | Add `publisher` to `DownloadEvent` construction |
| `backend/features/notifications.py` | Add `publisher: str` to `DownloadEvent`; remove old `format_*` fns |
| `frontend/api.py` | Add `GET /api/notifications/fields` endpoint |
| `frontend/static/js/settings_notifications.js` | Advanced toggle + multi-select field UI |
| `frontend/templates/settings_notifications.html` | Advanced toggle button markup |
| `tests/Tbackend/test_notification_fields.py` | **New** — unit tests |

---

## Testing

| Test | Method |
|---|---|
| `get_selected_fields` returns defaults when key absent | Unit — pass empty settings dict |
| `get_selected_fields` returns stored list when present | Unit — pass settings with `download_fields` |
| Discord embed only contains selected fields | Unit — mock `requests.post`, assert embed.fields keys |
| Discord embed thumbnail set when `cover` selected | Unit — assert `thumbnail.url` in payload |
| Discord embed thumbnail absent when `cover` not selected | Unit |
| Webhook payload omits non-selected keys | Unit — assert key absent from posted JSON |
| Custom script omits non-selected env vars | Unit — assert env var absent from `subprocess.run` call |
| `fetch_notification_cv_data` parses writer/artist/cover correctly | Unit — mock CV response |
| `fetch_notification_cv_data` returns all-None on exception | Unit — mock CV to raise |
| `DownloadEvent` has `publisher` field | Unit — construct event, assert attribute exists |

---

## Out of Scope

- Field selection for the Test event (always sends all fields)
- Persisting the "Advanced toggle" open/closed state across modal opens
- Adding new event types (OnRename, OnDelete)
- Storing cover URL in the volumes table
