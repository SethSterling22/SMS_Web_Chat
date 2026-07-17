#!/usr/bin/env python3
"""
SMS Dashboard - Server for Termux
Exposes a web interface to send/read the phone's SMS using Termux:API.
Run inside Termux:  python server.py
Access from your PC: http://<phone-tailscale-ip>:8080

Architecture:
- A background sync worker copies the phone's SMS into a local SQLite cache:
  * Backfill: on first run it pages through the ENTIRE history in chunks
    (termux-sms-list -o offset), so old conversations are complete.
  * Incremental: every SYNC_INTERVAL seconds it fetches only the most
    recent messages and upserts them (cheap for the phone).
- All API endpoints read from SQLite only — they never call termux-api
  directly, so the UI is fast and the phone isn't hammered by requests.
- Android only lets the *default* SMS app write to the system SMS store, so
  messages sent with termux-sms-send often never show up in termux-sms-list.
  We record sent messages ourselves and merge them into chats.
- Old/new versions of termux-sms-list need different flags (-d/-n, -t all);
  we probe variants and remember the one that works.
"""

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import unicodedata
from datetime import datetime

from flask import Flask, g, jsonify, request, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "dashboard.db"))
PORT = int(os.environ.get("PORT", "8080"))
SIM_SLOT = os.environ.get("SIM_SLOT", "").strip()  # dual-SIM: set to 0 or 1 if sending fails
SYNC_INTERVAL = int(os.environ.get("SYNC_INTERVAL", "10"))  # seconds between incremental syncs
RECENT_LIMIT = int(os.environ.get("RECENT_LIMIT", "100"))  # messages fetched per incremental sync
BACKFILL_CHUNK = int(os.environ.get("BACKFILL_CHUNK", "400"))  # messages per backfill page
API_KEY = os.environ.get("API_KEY", "").strip()  # if set, /api requires Authorization: Bearer <key>

app = Flask(__name__, static_folder=None)


# ---------------------------------------------------------------------------
# Auth (optional) + CORS, so external apps can integrate with the API
# ---------------------------------------------------------------------------

@app.before_request
def check_auth():
    if request.method == "OPTIONS":  # CORS preflight must pass
        return None
    if not API_KEY or not request.path.startswith("/api"):
        return None
    auth = request.headers.get("Authorization", "")
    if auth == f"Bearer {API_KEY}" or request.args.get("api_key") == API_KEY:
        return None
    return jsonify({"error": "Unauthorized: send 'Authorization: Bearer <API_KEY>'"}), 401


@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    return resp


@app.route("/api/<path:_sub>", methods=["OPTIONS"])
def cors_preflight(_sub):
    return ("", 204)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    number TEXT NOT NULL,
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_contacts_number ON contacts(number);
CREATE TABLE IF NOT EXISTS sent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    number TEXT NOT NULL,
    number_key TEXT NOT NULL,
    body TEXT NOT NULL,
    sent_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_sent_key ON sent_messages(number_key);
-- local cache of the phone's SMS store, filled by the sync worker
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_key TEXT UNIQUE NOT NULL,      -- dedupe hash (type|number|date|body)
    number TEXT NOT NULL,
    number_key TEXT NOT NULL,
    body TEXT NOT NULL,
    type TEXT NOT NULL,                -- inbox | sent | draft | outbox
    date TEXT NOT NULL,
    read INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_messages_key ON messages(number_key);
CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(date);
CREATE TABLE IF NOT EXISTS sync_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def open_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")  # web thread reads while sync thread writes
    return con


def get_db():
    db = getattr(g, "_db", None)
    if db is None:
        db = g._db = open_db()
    return db


@app.teardown_appcontext
def close_db(_exc):
    db = getattr(g, "_db", None)
    if db is not None:
        db.close()


def init_db():
    con = open_db()
    con.executescript(SCHEMA)
    con.commit()
    con.close()


def get_state(con, key, default=None):
    row = con.execute("SELECT value FROM sync_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(con, key, value):
    con.execute(
        "INSERT INTO sync_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_termux(cmd, timeout=60):
    """Runs a termux-* command and returns (ok, output)."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if out.returncode != 0:
            return False, out.stderr.strip() or out.stdout.strip()
        return True, out.stdout
    except FileNotFoundError:
        return False, f"Comando no encontrado: {cmd[0]}. ¿Instalaste 'pkg install termux-api'?"
    except subprocess.TimeoutExpired:
        return False, f"Timeout ejecutando {cmd[0]}. ¿Está instalada la app Termux:API y tiene permisos?"


def normalize_number(number):
    """Canonical key for grouping conversations: the last 10 digits.

    This merges the same number written in different formats,
    e.g. '+1 787 555 1234', '17875551234' and '787-555-1234'.
    """
    if not number:
        return ""
    d = re.sub(r"\D", "", str(number))
    return d[-10:] if len(d) >= 10 else d


def fold(s):
    """Lowercase + strip accents, for accent-insensitive search."""
    return unicodedata.normalize("NFD", str(s or "")).encode("ascii", "ignore").decode().lower()


# ---------------------------------------------------------------------------
# Sync worker: phone SMS -> SQLite cache
# ---------------------------------------------------------------------------

# flag variants for termux-sms-list, from newest to oldest style:
#   0: -t all, with -d -n     2: split inbox/sent calls, with -d -n
#   1: -t all, plain          3: split inbox/sent calls, plain
_caps = {"idx": None}
_sync_status = {"last_sync": None, "last_error": None, "backfill_done": False,
                "backfill_offset": 0, "cached": 0}


def _sms_list_call(extra, limit, offset):
    cmd = ["termux-sms-list"] + extra + ["-l", str(limit), "-o", str(offset)]
    ok, out = run_termux(cmd, timeout=120)
    if not ok:
        return False, out
    try:
        return True, (json.loads(out) if out.strip() else [])
    except json.JSONDecodeError:
        return False, "Respuesta inválida de termux-sms-list"


def fetch_chunk(limit, offset):
    """Fetches one page of SMS. offset 0 = most recent messages.

    Probes flag variants on first use and remembers the working one.
    """
    if _caps["idx"] is not None:
        order = [_caps["idx"]] + [i for i in range(4) if i != _caps["idx"]]
    else:
        order = [0, 1, 2, 3]
    last_err = "termux-sms-list no respondió"
    for i in order:
        dn = ["-d", "-n"] if i in (0, 2) else []
        if i in (0, 1):
            ok, data = _sms_list_call(dn + ["-t", "all"], limit, offset)
            if ok:
                _caps["idx"] = i
                return True, data
            last_err = data
        else:
            merged, failed = [], False
            for tt in ("inbox", "sent"):
                ok, data = _sms_list_call(dn + ["-t", tt], limit, offset)
                if not ok:
                    if tt == "inbox":  # inbox failing means these flags don't work
                        failed, last_err = True, data
                        break
                    data = []  # tolerate versions without a 'sent' box
                for m in data:
                    m.setdefault("type", tt)
                merged += data
            if not failed:
                _caps["idx"] = i
                return True, merged
    return False, last_err


def upsert_messages(con, items):
    """Inserts phone messages into the cache; updates read status on dupes."""
    for m in items:
        num = m.get("number") or m.get("sender") or ""
        key = normalize_number(num)
        if not key:
            continue
        body = m.get("body") or ""
        mtype = m.get("type", "inbox")
        date = m.get("received") or m.get("date") or ""
        read = 1 if m.get("read", True) else 0
        mk = hashlib.sha1(f"{mtype}|{key}|{date}|{body}".encode()).hexdigest()
        con.execute(
            "INSERT INTO messages (msg_key, number, number_key, body, type, date, read) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(msg_key) DO UPDATE SET read=excluded.read",
            (mk, num, key, body, mtype, date, read),
        )


def sync_worker():
    """Background loop: incremental sync of recent SMS + one-time backfill."""
    con = open_db()
    _sync_status["backfill_done"] = get_state(con, "backfill_done") == "1"
    _sync_status["backfill_offset"] = int(get_state(con, "backfill_offset") or 0)
    while True:
        try:
            # 1) incremental: newest messages (picks up replies and read status)
            ok, data = fetch_chunk(RECENT_LIMIT, 0)
            if ok:
                upsert_messages(con, data)
                con.commit()
                _sync_status["last_sync"] = datetime.now().isoformat(timespec="seconds")
                _sync_status["last_error"] = None
            else:
                _sync_status["last_error"] = data

            # 2) backfill: page through older history until exhausted (one page
            #    per cycle, so the phone never does heavy work in a burst)
            if ok and not _sync_status["backfill_done"]:
                off = _sync_status["backfill_offset"]
                ok2, older = fetch_chunk(BACKFILL_CHUNK, off)
                if ok2:
                    upsert_messages(con, older)
                    if len(older) < BACKFILL_CHUNK:
                        _sync_status["backfill_done"] = True
                        set_state(con, "backfill_done", "1")
                    else:
                        _sync_status["backfill_offset"] = off + BACKFILL_CHUNK
                        set_state(con, "backfill_offset", off + BACKFILL_CHUNK)
                    con.commit()
            _sync_status["cached"] = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        except Exception as e:  # never let the worker die
            _sync_status["last_error"] = str(e)
        time.sleep(SYNC_INTERVAL)


# ---------------------------------------------------------------------------
# Merged view: cached phone SMS + locally recorded sent messages
# ---------------------------------------------------------------------------

def contact_names():
    db = get_db()
    rows = db.execute("SELECT name, number FROM contacts").fetchall()
    return {normalize_number(r["number"]): r["name"] for r in rows}


def get_merged_messages():
    """All messages from the SQLite cache, merged with local sent copies.

    Local copies are skipped when the phone's SMS store already has a sent
    message with the same number and text (some devices do record them).
    """
    db = get_db()
    msgs = []
    phone_sent = set()
    for r in db.execute("SELECT number, number_key, body, type, date, read FROM messages"):
        if r["type"] == "sent":
            phone_sent.add((r["number_key"], r["body"].strip()))
        msgs.append({
            "number": r["number"], "key": r["number_key"], "body": r["body"],
            "type": r["type"], "date": r["date"], "read": bool(r["read"]),
            "id": None, "source": "phone",  # phone SMS can't be deleted via the API
        })
    for s in db.execute("SELECT id, number, number_key, body, sent_at FROM sent_messages"):
        if (s["number_key"], s["body"].strip()) in phone_sent:
            continue
        msgs.append({
            "number": s["number"], "key": s["number_key"], "body": s["body"],
            "type": "sent", "date": s["sent_at"], "read": True,
            "id": s["id"], "source": "local",
        })
    return msgs


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


# ---------------------------------------------------------------------------
# API: conversations and messages (read from cache only — fast)
# ---------------------------------------------------------------------------

CONV_LIMIT = int(os.environ.get("CONV_LIMIT", "25"))  # most-recent conversations shown


@app.route("/api/conversations")
@app.route("/api/v1/conversations")
def conversations():
    """Most recent conversations (default: last CONV_LIMIT contacts talked to).

    Pass ?limit=0 for all conversations. Search still covers everything.
    """
    try:
        limit = int(request.args.get("limit", CONV_LIMIT))
    except ValueError:
        limit = CONV_LIMIT
    names = contact_names()
    convs = {}
    for m in get_merged_messages():
        key = m["key"]
        c = convs.setdefault(key, {
            "number": m["number"], "key": key, "name": names.get(key),
            "last_message": "", "last_date": "", "unread": 0, "count": 0,
        })
        c["count"] += 1
        # prefer showing the number in international format when available
        if str(m["number"]).startswith("+"):
            c["number"] = m["number"]
        if m["date"] >= c["last_date"]:
            c["last_date"] = m["date"]
            c["last_message"] = (m["body"] or "")[:120]
        if m["type"] == "inbox" and not m["read"]:
            c["unread"] += 1
    result = sorted(convs.values(), key=lambda c: c["last_date"], reverse=True)
    if limit > 0:
        result = result[:limit]
    return jsonify(result)


def _chat_messages(number):
    """Paginated chat history. Returns the newest `limit` messages by
    default; `offset` counts backwards from the newest (offset=20 skips
    the 20 most recent), so clients can load older pages while scrolling up.
    """
    key = normalize_number(number)
    if not key:
        return jsonify({"error": "Falta el parámetro number"}), 400
    try:
        limit = min(max(int(request.args.get("limit", 20)), 1), 500)
        offset = max(int(request.args.get("offset", 0)), 0)
    except ValueError:
        limit, offset = 20, 0
    msgs = [
        {"id": m["id"], "source": m["source"], "body": m["body"],
         "type": m["type"], "date": m["date"], "read": m["read"]}
        for m in get_merged_messages() if m["key"] == key
    ]
    msgs.sort(key=lambda m: m["date"])
    total = len(msgs)
    end = max(total - offset, 0)
    start = max(end - limit, 0)
    return jsonify({
        "messages": msgs[start:end],
        "total": total,
        "has_more": start > 0,
        "limit": limit,
        "offset": offset,
    })


@app.route("/api/messages")
def messages_legacy():
    return _chat_messages(request.args.get("number", ""))


@app.route("/api/v1/conversations/<path:number>/messages")
def chat_messages_v1(number):
    return _chat_messages(number)


@app.route("/api/send", methods=["POST"])
@app.route("/api/v1/messages", methods=["POST"])
def send_sms():
    """Sends an SMS. Body: {"to": "+1787...", "body": "text"}
    (legacy keys "number"/"message" also accepted)."""
    payload = request.get_json(force=True, silent=True) or {}
    number = (payload.get("to") or payload.get("number") or "").strip()
    message = (payload.get("body") or payload.get("message") or "").strip()
    if not number or not message:
        return jsonify({"error": "Se requiere 'to' (número) y 'body' (mensaje)"}), 400
    cmd = ["termux-sms-send", "-n", number]
    if SIM_SLOT:
        cmd += ["-s", SIM_SLOT]
    cmd.append(message)
    ok, out = run_termux(cmd, timeout=60)
    if not ok:
        return jsonify({"error": f"termux-sms-send falló: {out}"}), 500
    # record the sent message locally: Android usually won't let us write to
    # the system SMS store, so termux-sms-list may never show it
    db = get_db()
    cur = db.execute(
        "INSERT INTO sent_messages (number, number_key, body) VALUES (?, ?, ?)",
        (number, normalize_number(number), message),
    )
    db.commit()
    return jsonify({"ok": True, "id": cur.lastrowid, "to": number}), 201


@app.route("/api/v1/messages/<int:mid>", methods=["DELETE"])
def delete_message(mid):
    """Deletes a locally-recorded sent message (source='local' only).

    Phone SMS cannot be deleted through Termux — Android restricts SMS
    store writes to the default SMS app.
    """
    db = get_db()
    cur = db.execute("DELETE FROM sent_messages WHERE id=?", (mid,))
    db.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "No existe, o es un SMS del teléfono (source='phone') que no se puede borrar vía API"}), 404
    return jsonify({"ok": True})


@app.route("/api/search")
@app.route("/api/v1/search")
def search():
    q = (request.args.get("q") or "").strip()
    terms = [fold(t) for t in q.split() if t.strip()]
    if not terms:
        return jsonify({"messages": [], "contacts": []})

    def matches(haystack):
        h = fold(haystack)
        return all(t in h for t in terms)

    names = contact_names()
    found_msgs = []
    for m in get_merged_messages():
        cname = names.get(m["key"]) or ""
        if matches(f'{m["body"]} {m["number"]} {cname}'):
            found_msgs.append({
                "number": m["number"], "name": names.get(m["key"]),
                "body": m["body"], "type": m["type"], "date": m["date"],
            })
    found_msgs.sort(key=lambda m: m["date"], reverse=True)
    found_msgs = found_msgs[:50]
    db = get_db()
    found_contacts = [
        dict(r) for r in db.execute("SELECT * FROM contacts ORDER BY name")
        if matches(f'{r["name"]} {r["number"]} {r["notes"]}')
    ]
    return jsonify({"messages": found_msgs, "contacts": found_contacts})


# ---------------------------------------------------------------------------
# API: templates
# ---------------------------------------------------------------------------

@app.route("/api/templates", methods=["GET", "POST"])
@app.route("/api/v1/templates", methods=["GET", "POST"])
def templates():
    db = get_db()
    if request.method == "GET":
        rows = db.execute("SELECT * FROM templates ORDER BY name").fetchall()
        return jsonify([dict(r) for r in rows])
    payload = request.get_json(force=True, silent=True) or {}
    name = (payload.get("name") or "").strip()
    body = (payload.get("body") or "").strip()
    if not name or not body:
        return jsonify({"error": "Se requiere nombre y texto"}), 400
    cur = db.execute("INSERT INTO templates (name, body) VALUES (?, ?)", (name, body))
    db.commit()
    return jsonify({"ok": True, "id": cur.lastrowid})


@app.route("/api/templates/<int:tid>", methods=["GET", "PUT", "DELETE"])
@app.route("/api/v1/templates/<int:tid>", methods=["GET", "PUT", "DELETE"])
def template_item(tid):
    db = get_db()
    if request.method == "GET":
        row = db.execute("SELECT * FROM templates WHERE id=?", (tid,)).fetchone()
        if not row:
            return jsonify({"error": "Template no encontrado"}), 404
        return jsonify(dict(row))
    if request.method == "DELETE":
        db.execute("DELETE FROM templates WHERE id=?", (tid,))
        db.commit()
        return jsonify({"ok": True})
    payload = request.get_json(force=True, silent=True) or {}
    db.execute(
        "UPDATE templates SET name=?, body=? WHERE id=?",
        (payload.get("name", ""), payload.get("body", ""), tid),
    )
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API: external contacts (with notes)
# ---------------------------------------------------------------------------

@app.route("/api/contacts", methods=["GET", "POST"])
@app.route("/api/v1/contacts", methods=["GET", "POST"])
def contacts():
    db = get_db()
    if request.method == "GET":
        rows = db.execute("SELECT * FROM contacts ORDER BY name").fetchall()
        return jsonify([dict(r) for r in rows])
    payload = request.get_json(force=True, silent=True) or {}
    name = (payload.get("name") or "").strip()
    number = (payload.get("number") or "").strip()
    notes = (payload.get("notes") or "").strip()
    if not name or not number:
        return jsonify({"error": "Se requiere nombre y número"}), 400
    cur = db.execute(
        "INSERT INTO contacts (name, number, notes) VALUES (?, ?, ?)",
        (name, number, notes),
    )
    db.commit()
    return jsonify({"ok": True, "id": cur.lastrowid})


@app.route("/api/contacts/<int:cid>", methods=["GET", "PUT", "DELETE"])
@app.route("/api/v1/contacts/<int:cid>", methods=["GET", "PUT", "DELETE"])
def contact_item(cid):
    db = get_db()
    if request.method == "GET":
        row = db.execute("SELECT * FROM contacts WHERE id=?", (cid,)).fetchone()
        if not row:
            return jsonify({"error": "Contacto no encontrado"}), 404
        return jsonify(dict(row))
    if request.method == "DELETE":
        db.execute("DELETE FROM contacts WHERE id=?", (cid,))
        db.commit()
        return jsonify({"ok": True})
    payload = request.get_json(force=True, silent=True) or {}
    db.execute(
        "UPDATE contacts SET name=?, number=?, notes=?, updated_at=datetime('now','localtime') WHERE id=?",
        (payload.get("name", ""), payload.get("number", ""), payload.get("notes", ""), cid),
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/contacts/import", methods=["POST"])
@app.route("/api/v1/contacts/import", methods=["POST"])
def import_contacts():
    """Imports phone contacts (termux-contact-list) that don't exist yet.

    Deduplicates by normalized number (last 10 digits), so '+1 787 555 1234'
    and '787-555-1234' count as the same contact.
    """
    ok, out = run_termux(["termux-contact-list"])
    if not ok:
        return jsonify({"error": out}), 500
    try:
        phone_contacts = json.loads(out) if out.strip() else []
    except json.JSONDecodeError:
        return jsonify({"error": "Respuesta inválida de termux-contact-list"}), 500
    db = get_db()
    existing = {normalize_number(r["number"]) for r in db.execute("SELECT number FROM contacts")}
    added = 0
    for c in phone_contacts:
        name = (c.get("name") or "").strip()
        number = (c.get("number") or "").strip()
        if not number or normalize_number(number) in existing:
            continue
        db.execute(
            "INSERT INTO contacts (name, number) VALUES (?, ?)",
            (name or number, number),
        )
        existing.add(normalize_number(number))
        added += 1
    db.commit()
    return jsonify({"ok": True, "imported": added})


@app.route("/api/debug")
@app.route("/api/v1/debug")
def debug():
    """Live diagnostics for sync problems.

    Compares what termux-sms-list returns RIGHT NOW against the newest
    cached messages. If a message you just received does not appear in
    'live_sample', it is not in Android's SMS store at all — almost always
    because the conversation uses RCS ('chat features'), which no
    third-party app can read. That is a phone setting, not a sync bug.
    """
    ok, raw = fetch_chunk(10, 0)
    db = get_db()
    newest = db.execute(
        "SELECT date, type, number, substr(body, 1, 60) AS body FROM messages ORDER BY date DESC LIMIT 5"
    ).fetchall()
    return jsonify({
        "live_ok": ok,
        "live_sample_newest_10": raw if ok else None,
        "live_error": None if ok else raw,
        "variant_flags": _caps["idx"],
        "sync": dict(_sync_status),
        "newest_5_cached": [dict(r) for r in newest],
    })


@app.route("/api/status")
@app.route("/api/v1/status")
def status():
    """Diagnostics: sync health, cache size, backfill progress."""
    db = get_db()
    sent_count = db.execute("SELECT COUNT(*) FROM sent_messages").fetchone()[0]
    healthy = _sync_status["last_error"] is None
    return jsonify({
        "server": "ok",
        "termux_api": "ok" if healthy else "error",
        "detail": _sync_status["last_error"],
        "sms_count": _sync_status["cached"],
        "local_sent_count": sent_count,
        "last_sync": _sync_status["last_sync"],
        "backfill_done": _sync_status["backfill_done"],
        "backfill_offset": _sync_status["backfill_offset"],
        "sync_interval": SYNC_INTERVAL,
        "sim_slot": SIM_SLOT or None,
        "time": datetime.now().isoformat(timespec="seconds"),
    })


if __name__ == "__main__":
    init_db()
    threading.Thread(target=sync_worker, daemon=True).start()
    print(f"\n  SMS Dashboard running on port {PORT}")
    print(f"  From your PC open:  http://<phone-tailscale-ip>:{PORT}")
    print("  First run: full SMS history sync happens in the background;")
    print("  old conversations fill in progressively.\n")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
