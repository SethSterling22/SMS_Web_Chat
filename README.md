# SMS Web Chat (Termux SMS Dashboard)

Send and read your phone's SMS from your computer's browser, over Tailscale or your local network. Includes chat history, message templates, a contacts database with notes, and global search.

The server runs entirely on the phone inside Termux — nothing to install on the PC:

```
PC (browser) ──Tailscale/LAN──▶ Phone (Termux)
                                ├─ Web server (Python/Flask, port 8080)
                                ├─ Termux:API → send SMS, read history, contacts
                                └─ SQLite → templates, external contacts, notes
```

## Phone requirements

1. **Termux** installed from F-Droid (the Play Store build is outdated and incompatible).
2. **Termux:API** (the app, not just the package): install from F-Droid → https://f-droid.org/packages/com.termux.api/
3. **IMPORTANT — before using the app:** go to **Android Settings → Apps → Termux:API → Permissions** and grant FULL permissions for **SMS**, **Contacts** and **Phone (calls)**. Sending will silently fail without all three; the Phone permission is required on many devices even though the app only sends SMS.

## Installation

Copy this repository to the phone. Two options:

**Option A (recommended):** clone directly in Termux:

```bash
pkg install -y git
git clone <this-repo-url> ~/sms-dashboard
cd ~/sms-dashboard
```

**Option B:** transfer a zip to the phone's Downloads folder, then in Termux:

```bash
termux-setup-storage        # grants access to Downloads (first time only)
cd ~
unzip ~/storage/downloads/sms-dashboard.zip
cd sms-dashboard
```

Then:

```bash
bash install.sh
```

The script installs Python, Flask and termux-api, and runs an SMS read test. Android will ask for permissions the first time.

## Usage

In Termux:

```bash
bash start.sh
```

On your PC, open the browser at:

```
http://<phone-tailscale-ip>:8080
```

The phone's Tailscale IP (starts with `100.`) is shown by `tailscale ip` or in the Tailscale app. `start.sh` also prints it on startup.

## Features

- **Chats**: conversation list sorted by date, with unread counts. Auto-refreshes every 8 seconds.
- **Send**: Enter sends, Shift+Enter inserts a newline. A ⧉ button on each message copies it to the clipboard.
- **Templates**: "Templates" button to create/edit them. In a chat, the 📋 button inserts one. The `{nombre}` variable is automatically replaced with the contact's name; other variables (`{fecha}`, etc.) are edited before sending (you get a warning if any is left unreplaced).
- **New chat**: "✚ Nuevo chat" button — type a number and optionally save it as a contact.
- **Contacts**: dedicated tab, with per-contact notes. "Importar del teléfono" pulls the Android contacts. Stored in `dashboard.db` (SQLite) inside the folder.
- **Search**: the top search box looks through message text, names, numbers and notes.

## Keeping it running

- `start.sh` already runs `termux-wake-lock` so Android doesn't kill the process.
- Disable battery optimization for Termux: Settings → Apps → Termux → Battery → Unrestricted.
- Optional: install the **Termux:Boot** app (F-Droid) so it starts automatically on phone reboot — create `~/.termux/boot/start-sms.sh` with:

  ```bash
  #!/data/data/com.termux/files/usr/bin/bash
  bash ~/sms-dashboard/start.sh
  ```

## Security

The server listens on all of the phone's interfaces. With Tailscale this is safe (only devices in your tailnet can reach it), but avoid using it on public WiFi without Tailscale. Nothing leaves the phone: SMS, contacts and notes stay local.

## How syncing works

A background worker copies the phone's SMS into a local SQLite cache (`dashboard.db`):

- **Backfill**: on first run it pages through the entire SMS history in chunks, so old conversations are complete. Progress is saved — if you restart the server, it resumes where it left off.
- **Incremental**: every 10 seconds it fetches only the most recent messages, which is cheap for the phone.

All dashboard requests read from the cache only — they never hit termux-api directly — so the UI stays fast regardless of how large your history is.

## How sent messages are stored

Android only allows the *default* SMS app to write to the system SMS store, so messages sent with `termux-sms-send` usually never appear in `termux-sms-list`. The server records every message you send in its own SQLite database (`dashboard.db`) and merges both sources when showing a chat, deduplicating when the phone did record the message.

Conversations are grouped by the **last 10 digits** of the number, so `+1 787 555 1234` and `787-555-1234` show as a single chat.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Status dot is red / "Termux:API no responde" | Termux:API app missing, or no SMS permission |
| `termux-sms-list` hangs | Termux and Termux:API installed from different sources (Play vs F-Droid) — both must be from F-Droid |
| Page won't load from the PC | Check Tailscale is up on both devices and `start.sh` is running |
| Sending fails or nothing arrives (dual-SIM phones) | Set the SIM slot: `SIM_SLOT=0 bash start.sh` (or `SIM_SLOT=1`) |
| Sending silently does nothing | Grant Termux:API full SMS, Contacts AND Phone permissions in Android settings |
| Old chats incomplete right after starting | The initial backfill is still running; check `backfill_done` in `/api/status` |

Visit `/api/status` for diagnostics (cache size, backfill progress, last sync, errors).

## Configuration

Environment variables (optional): `PORT` (default 8080), `DB_PATH` (SQLite location), `SIM_SLOT` (SIM slot for dual-SIM phones, e.g. `0` or `1`), `SYNC_INTERVAL` (seconds between syncs, default 10), `RECENT_LIMIT` (messages per incremental sync, default 100), `BACKFILL_CHUNK` (messages per backfill page, default 400).

Note: the dashboard UI is in Spanish.
