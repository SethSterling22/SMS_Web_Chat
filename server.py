#!/usr/bin/env python3
"""
SMS Dashboard - Server for Termux
Exposes a web interface to send/read the phone's SMS using Termux:API.
Run inside Termux:  python server.py
Access from your PC: http://<phone-tailscale-ip>:8080

Notes:
- Android only lets the *default* SMS app write to the system SMS store, so
  messages sent with termux-sms-send often never show up in termux-sms-list.
  We keep our own copy of sent messages in SQLite and merge both sources.
- Old/new versions of termux-sms-list need different flags (-d for dates,
  -n for phone numbers); we probe several variants and remember the one
  that works.
"""

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
SMS_LIMIT = int(os.environ.get("SMS_LIMIT", "2000"))  # how many SMS to read from the phone
SIM_SLOT = os.environ.get("SIM_SLOT", "").strip()  # dual-SIM: set to 0 or 1 if sending fails
CACHE_TTL = 5  # seconds of cache for termux-sms-list (avoids repeated calls)

app = Flask(__name__, static_folder=None)

# ---------------------------------------------------------------------------
# Database (templates, external contacts with notes, locally-recorded sent SMS)
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
"""


def get_db():
    db = getattr(g, "_db", None)
    if db is None:
        db = g._db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
    return db


@app.teardown_appcontext
def close_db(_exc):
    db = getattr(g, "_db", None)
    if db is not None:
        db.close()


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# Termux helpers
# ---------------------------------------------------------------------------

def run_termux(cmd, timeout=30):
    """Runs a termux-* command and returns (ok, output)."""
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
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


# --- termux-sms-list flag variants (older versions lack -d/-n or -t all) ---
_LIST_VARIANTS = [
    ["-t", "all", "-d", "-n"],  # preferred: all types, with dates and numbers
    ["-t", "all"],
    ["-d", "-n"],
    [],
]
_variant_state = {"idx": None}

_sms_cache = {"time": 0.0, "data": None}
_sms_lock = threading.Lock()


def fetch_sms_list():
    """Tries flag variants until one returns valid JSON; remembers the winner."""
    if _variant_state["idx"] is not None:
        order = [_variant_state["idx"]] + [
            i for i in range(len(_LIST_VARIANTS)) if i != _variant_state["idx"]
        ]
    else:
        order = list(range(len(_LIST_VARIANTS)))
    last_err = "termux-sms-list no respondió"
    for i in order:
        cmd = ["termux-sms-list"] + _LIST_VARIANTS[i] + ["-l", str(SMS_LIMIT)]
        ok, out = run_termux(cmd)
        if not ok:
            last_err = out
            continue
        try:
            data = json.loads(out) if out.strip() else []
        except json.JSONDecodeError:
            last_err = "Respuesta inválida de termux-sms-list"
            continue
        _variant_state["idx"] = i
        return True, data
    return False, last_err


def get_all_sms(force=False):
    """Reads the phone's SMS (with a short cache)."""
    with _sms_lock:
        now = time.time()
        if not force and _sms_cache["data"] is not None and now - _sms_cache["time"] < CACHE_TTL:
            return True, _sms_cache["data"]
        ok, data = fetch_sms_list()
        if not ok:
            return False, data
        _sms_cache["time"] = now
        _sms_cache["data"] = data
        return True, data


def invalidate_sms_cache():
    with _sms_lock:
        _sms_cache["data"] = None


def contact_names():
    """Map of normalized_number -> name, from the external contacts database."""
    db = get_db()
    rows = db.execute("SELECT name, number FROM contacts").fetchall()
    return {normalize_number(r["number"]): r["name"] for r in rows}


def get_merged_messages():
    """Phone SMS merged with locally-recorded sent messages.

    Local copies are skipped when the phone's SMS store already has a sent
    message with the same number and text (some devices do record them).
    Returns (ok, list of {number, key, body, type, date, read}).
    """
    ok, data = get_all_sms()
    if not ok:
        return False, data
    msgs = []
    phone_sent = set()
    for m in data:
        num = m.get("number") or m.get("sender") or ""
        key = normalize_number(num)
        if not key:
            continue
        mtype = m.get("type", "inbox")
        body = m.get("body") or ""
        if mtype == "sent":
            phone_sent.add((key, body.strip()))
        msgs.append({
            "number": num,
            "key": key,
            "body": body,
            "type": mtype,
            "date": m.get("received") or m.get("date") or "",
            "read": m.get("read", True),
        })
    db = get_db()
    for s in db.execute("SELECT number, number_key, body, sent_at FROM sent_messages"):
        if (s["number_key"], s["body"].strip()) in phone_sent:
            continue
        msgs.append({
            "number": s["number"],
            "key": s["number_key"],
            "body": s["body"],
            "type": "sent",
            "date": s["sent_at"],
            "read": True,
        })
    return True, msgs


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


# ---------------------------------------------------------------------------
# API: conversations and messages
# ---------------------------------------------------------------------------

@app.route("/api/conversations")
def conversations():
    ok, data = get_merged_messages()
    if not ok:
        return jsonify({"error": data}), 500
    names = contact_names()
    convs = {}
    for m in data:
        key = m["key"]
        c = convs.setdefault(key, {
            "number": m["number"],
            "key": key,
            "name": names.get(key),
            "last_message": "",
            "last_date": "",
            "unread": 0,
            "count": 0,
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
    return jsonify(result)


@app.route("/api/messages")
def messages():
    number = request.args.get("number", "")
    key = normalize_number(number)
    if not key:
        return jsonify({"error": "Falta el parámetro number"}), 400
    ok, data = get_merged_messages()
    if not ok:
        return jsonify({"error": data}), 500
    msgs = [
        {"body": m["body"], "type": m["type"], "date": m["date"], "read": m["read"]}
        for m in data if m["key"] == key
    ]
    msgs.sort(key=lambda m: m["date"])
    return jsonify(msgs)


@app.route("/api/send", methods=["POST"])
def send_sms():
    payload = request.get_json(force=True, silent=True) or {}
    number = (payload.get("number") or "").strip()
    message = (payload.get("message") or "").strip()
    if not number or not message:
        return jsonify({"error": "Se requiere número y mensaje"}), 400
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
    db.execute(
        "INSERT INTO sent_messages (number, number_key, body) VALUES (?, ?, ?)",
        (number, normalize_number(number), message),
    )
    db.commit()
    invalidate_sms_cache()
    return jsonify({"ok": True})


@app.route("/api/search")
def search():
    q = (request.args.get("q") or "").strip()
    terms = [fold(t) for t in q.split() if t.strip()]
    if not terms:
        return jsonify({"messages": [], "contacts": []})

    def matches(haystack):
        h = fold(haystack)
        return all(t in h for t in terms)

    names = contact_names()
    # search in messages (accent-insensitive, all terms must match)
    found_msgs = []
    ok, data = get_merged_messages()
    if ok:
        for m in data:
            cname = names.get(m["key"]) or ""
            if matches(f'{m["body"]} {m["number"]} {cname}'):
                found_msgs.append({
                    "number": m["number"],
                    "name": names.get(m["key"]),
                    "body": m["body"],
                    "type": m["type"],
                    "date": m["date"],
                })
        found_msgs.sort(key=lambda m: m["date"], reverse=True)
        found_msgs = found_msgs[:50]
    # search in contacts (name, number, notes)
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


@app.route("/api/templates/<int:tid>", methods=["PUT", "DELETE"])
def template_item(tid):
    db = get_db()
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


@app.route("/api/contacts/<int:cid>", methods=["PUT", "DELETE"])
def contact_item(cid):
    db = get_db()
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


@app.route("/api/status")
def status():
    """Quick diagnostics: checks that termux-api responds."""
    ok, out = get_all_sms()
    variant = _variant_state["idx"]
    db = get_db()
    sent_count = db.execute("SELECT COUNT(*) FROM sent_messages").fetchone()[0]
    return jsonify({
        "server": "ok",
        "termux_api": "ok" if ok else "error",
        "detail": None if ok else out,
        "sms_count": len(out) if ok else 0,
        "local_sent_count": sent_count,
        "sms_list_flags": " ".join(_LIST_VARIANTS[variant]) if variant is not None else None,
        "sim_slot": SIM_SLOT or None,
        "time": datetime.now().isoformat(timespec="seconds"),
    })


if __name__ == "__main__":
    init_db()
    print(f"\n  SMS Dashboard running on port {PORT}")
    print(f"  From your PC open:  http://<phone-tailscale-ip>:{PORT}\n")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
