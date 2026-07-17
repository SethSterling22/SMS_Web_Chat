#!/usr/bin/env python3
"""
SMS Dashboard - Server for Termux
Exposes a web interface to send/read the phone's SMS using Termux:API.
Run inside Termux:  python server.py
Access from your PC: http://<phone-tailscale-ip>:8080
"""

import json
import os
import re
import sqlite3
import subprocess
import threading
import time
from datetime import datetime

from flask import Flask, g, jsonify, request, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "dashboard.db"))
PORT = int(os.environ.get("PORT", "8080"))
SMS_LIMIT = int(os.environ.get("SMS_LIMIT", "2000"))  # how many SMS to read from the phone
CACHE_TTL = 5  # seconds of cache for termux-sms-list (avoids repeated calls)

app = Flask(__name__, static_folder=None)

# ---------------------------------------------------------------------------
# Database (templates, external contacts with notes)
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
    """Normalizes a phone number to group conversations (strips spaces, dashes)."""
    if not number:
        return ""
    n = re.sub(r"[^\d+]", "", str(number))
    return n


_sms_cache = {"time": 0.0, "data": None}
_sms_lock = threading.Lock()


def get_all_sms(force=False):
    """Reads the phone's SMS (with a short cache)."""
    with _sms_lock:
        now = time.time()
        if not force and _sms_cache["data"] is not None and now - _sms_cache["time"] < CACHE_TTL:
            return True, _sms_cache["data"]
        ok, out = run_termux(["termux-sms-list", "-t", "all", "-l", str(SMS_LIMIT)])
        if not ok:
            return False, out
        try:
            data = json.loads(out) if out.strip() else []
        except json.JSONDecodeError:
            return False, "Respuesta inválida de termux-sms-list"
        _sms_cache["time"] = now
        _sms_cache["data"] = data
        return True, data


def contact_names():
    """Map of normalized_number -> name, from the external contacts database."""
    db = get_db()
    rows = db.execute("SELECT name, number FROM contacts").fetchall()
    return {normalize_number(r["number"]): r["name"] for r in rows}


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
    ok, data = get_all_sms()
    if not ok:
        return jsonify({"error": data}), 500
    names = contact_names()
    convs = {}
    for m in data:
        num = normalize_number(m.get("number") or m.get("sender") or "")
        if not num:
            continue
        c = convs.setdefault(num, {
            "number": m.get("number"),
            "key": num,
            "name": names.get(num),
            "last_message": "",
            "last_date": "",
            "unread": 0,
            "count": 0,
        })
        c["count"] += 1
        date = m.get("received") or m.get("date") or ""
        if date >= c["last_date"]:
            c["last_date"] = date
            c["last_message"] = (m.get("body") or "")[:120]
        if m.get("type") == "inbox" and not m.get("read", True):
            c["unread"] += 1
    result = sorted(convs.values(), key=lambda c: c["last_date"], reverse=True)
    return jsonify(result)


@app.route("/api/messages")
def messages():
    number = request.args.get("number", "")
    key = normalize_number(number)
    if not key:
        return jsonify({"error": "Falta el parámetro number"}), 400
    ok, data = get_all_sms()
    if not ok:
        return jsonify({"error": data}), 500
    msgs = []
    for m in data:
        if normalize_number(m.get("number") or m.get("sender") or "") == key:
            msgs.append({
                "body": m.get("body", ""),
                "type": m.get("type", "inbox"),  # inbox = received, sent = sent
                "date": m.get("received") or m.get("date") or "",
                "read": m.get("read", True),
            })
    msgs.sort(key=lambda m: m["date"])
    return jsonify(msgs)


@app.route("/api/send", methods=["POST"])
def send_sms():
    payload = request.get_json(force=True, silent=True) or {}
    number = (payload.get("number") or "").strip()
    message = (payload.get("message") or "").strip()
    if not number or not message:
        return jsonify({"error": "Se requiere número y mensaje"}), 400
    ok, out = run_termux(["termux-sms-send", "-n", number, message], timeout=60)
    if not ok:
        return jsonify({"error": out}), 500
    # invalidate the cache so the sent message shows up quickly
    with _sms_lock:
        _sms_cache["data"] = None
    return jsonify({"ok": True})


@app.route("/api/search")
def search():
    q = (request.args.get("q") or "").strip().lower()
    if not q:
        return jsonify({"messages": [], "contacts": []})
    names = contact_names()
    # search in messages
    found_msgs = []
    ok, data = get_all_sms()
    if ok:
        for m in data:
            body = (m.get("body") or "")
            num = m.get("number") or m.get("sender") or ""
            if q in body.lower() or q in str(num).lower():
                found_msgs.append({
                    "number": num,
                    "name": names.get(normalize_number(num)),
                    "body": body,
                    "type": m.get("type", "inbox"),
                    "date": m.get("received") or m.get("date") or "",
                })
        found_msgs.sort(key=lambda m: m["date"], reverse=True)
        found_msgs = found_msgs[:50]
    # search in contacts (name, number, notes)
    db = get_db()
    like = f"%{q}%"
    rows = db.execute(
        "SELECT * FROM contacts WHERE lower(name) LIKE ? OR number LIKE ? OR lower(notes) LIKE ? ORDER BY name",
        (like, like, like),
    ).fetchall()
    found_contacts = [dict(r) for r in rows]
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
    """Imports phone contacts (termux-contact-list) that don't exist yet."""
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
    return jsonify({
        "server": "ok",
        "termux_api": "ok" if ok else "error",
        "detail": None if ok else out,
        "sms_count": len(out) if ok else 0,
        "time": datetime.now().isoformat(timespec="seconds"),
    })


if __name__ == "__main__":
    init_db()
    print(f"\n  SMS Dashboard running on port {PORT}")
    print(f"  From your PC open:  http://<phone-tailscale-ip>:{PORT}\n")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
