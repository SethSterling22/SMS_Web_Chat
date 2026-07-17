# SMS Web Chat — REST API (v1)

Standardized HTTP API so any external application can read chat history, manage contacts/templates, and send SMS through the phone.

Base URL: `http://<phone-ip>:8080/api/v1`

All requests and responses use JSON (`Content-Type: application/json`). An OpenAPI 3.0 spec is available in [`openapi.yaml`](openapi.yaml) — import it into Postman, Insomnia, or a code generator to build a client automatically.

> Legacy note: the old routes (`/api/send`, `/api/messages?number=`) still work, but new integrations should use `/api/v1`.

## Authentication

Optional. If the server was started with an `API_KEY` environment variable:

```bash
API_KEY=my-secret-key bash start.sh
```

then every `/api` request must include one of:

```
Authorization: Bearer my-secret-key        # preferred
?api_key=my-secret-key                     # query param fallback
```

Requests without a valid key get `401 {"error": "Unauthorized..."}`. If `API_KEY` is not set, the API is open (rely on Tailscale for network isolation).

## Conventions

- **Phone numbers**: any format is accepted (`+17875551234`, `787-555-1234`, …). Conversations are matched by the **last 10 digits**, so all formats of the same number map to the same chat.
- **Dates**: local time strings, `YYYY-MM-DD HH:MM[:SS]`, sortable lexicographically.
- **Message `source`**: `"phone"` = read from Android's SMS store; `"local"` = sent through this API and recorded by the server (Android doesn't let non-default SMS apps write to the SMS store). Only `local` messages have an `id` and can be deleted.
- **Errors**: non-2xx responses return `{"error": "<description>"}`.

---

## Messages

### Send an SMS

```
POST /api/v1/messages
{"to": "+17875551234", "body": "Your study is ready"}
```

`201 Created` → `{"ok": true, "id": 42, "to": "+17875551234"}`

The message is sent via the phone's SIM (`termux-sms-send`) and recorded locally. Legacy keys `number`/`message` are also accepted. Env `SIM_SLOT` selects the SIM on dual-SIM phones.

```bash
curl -X POST http://phone:8080/api/v1/messages \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"to": "+17875551234", "body": "Hola"}'
```

### Get a chat's messages (paginated)

```
GET /api/v1/conversations/{number}/messages?limit=20&offset=0
```

Returns the **newest** `limit` messages of that conversation (sent + received merged, oldest→newest within the page). `offset` counts backwards from the newest message: `offset=20` returns the 20 messages before the latest 20 — ideal for infinite scroll.

```json
{
  "messages": [
    {"id": null, "source": "phone", "type": "inbox", "body": "Hola doctor",
     "date": "2026-07-17 09:15", "read": true},
    {"id": 42, "source": "local", "type": "sent", "body": "Su estudio está listo",
     "date": "2026-07-17 09:20", "read": true}
  ],
  "total": 418, "has_more": true, "limit": 20, "offset": 0
}
```

`limit` max is 500. `type` is `inbox` (received) or `sent`.

### Delete a message

```
DELETE /api/v1/messages/{id}
```

`200` → `{"ok": true}` · `404` if it doesn't exist or is a phone SMS (`source: "phone"` — Android does not allow third-party apps to delete SMS).

## Conversations

```
GET /api/v1/conversations?limit=25
```

Most recent conversations, newest first. `limit=0` returns all.

```json
[
  {"number": "+17875551234", "key": "7875551234", "name": "Dr. García",
   "last_message": "Su estudio está listo", "last_date": "2026-07-17 09:20",
   "unread": 1, "count": 418}
]
```

`name` comes from the contacts database (`null` if unknown). Use `key` or `number` for the messages endpoint.

## Contacts

Full CRUD. A contact: `{"id", "name", "number", "notes", "created_at", "updated_at"}`.

| Method & path | Action |
|---|---|
| `GET /api/v1/contacts` | List all (sorted by name) |
| `GET /api/v1/contacts/{id}` | Get one (404 if missing) |
| `POST /api/v1/contacts` | Create — body `{"name", "number", "notes"?}` → `{"ok": true, "id"}` |
| `PUT /api/v1/contacts/{id}` | Update — body `{"name", "number", "notes"}` |
| `DELETE /api/v1/contacts/{id}` | Delete (does not delete messages) |
| `POST /api/v1/contacts/import` | Import the phone's contacts (deduped by number) → `{"ok": true, "imported": n}` |

## Templates

Full CRUD. A template: `{"id", "name", "body", "created_at"}`. Bodies may contain variables like `{nombre}` — substitution is the client's responsibility.

| Method & path | Action |
|---|---|
| `GET /api/v1/templates` | List all |
| `GET /api/v1/templates/{id}` | Get one |
| `POST /api/v1/templates` | Create — body `{"name", "body"}` |
| `PUT /api/v1/templates/{id}` | Update |
| `DELETE /api/v1/templates/{id}` | Delete |

## Search

```
GET /api/v1/search?q=garcia+estudio
```

Accent-insensitive; all terms must match. Searches message bodies, numbers, contact names and notes across the **entire** history.

```json
{"messages": [{"number", "name", "body", "type", "date"}, ...],
 "contacts": [{...contact}, ...]}
```

## Diagnostics

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/status` | Sync health: `sms_count` (cache size), `backfill_done`, `last_sync`, `termux_api` (`ok`/`error` + `detail`) |
| `GET /api/v1/debug` | Live `termux-sms-list` sample vs newest cached messages — distinguishes RCS-invisible messages from sync bugs |

## CORS

All endpoints send `Access-Control-Allow-Origin: *` and handle `OPTIONS` preflight, so browser-based apps can call the API directly.

## Integration notes

- **Polling**: the server caches the phone's SMS in SQLite and refreshes every `SYNC_INTERVAL` (10 s default). Polling the API more often than that returns the same data; every 5–10 s is plenty.
- **New-message detection**: poll `GET /conversations` and compare `last_date`/`unread`, or poll a chat's messages endpoint and compare `total`.
- **RCS limitation**: messages exchanged as RCS ("chat features") never enter Android's SMS store and cannot be served by this API. Disable RCS on the phone for full coverage (see README).
