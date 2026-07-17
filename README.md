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
3. After installing it, go to **Android Settings → Apps → Termux:API → Permissions** and enable **SMS** and **Contacts**.

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

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Status dot is red / "Termux:API no responde" | Termux:API app missing, or no SMS permission |
| `termux-sms-list` hangs | Termux and Termux:API installed from different sources (Play vs F-Droid) — both must be from F-Droid |
| Page won't load from the PC | Check Tailscale is up on both devices and `start.sh` is running |
| Sent messages don't show up | Wait a few seconds; history is read from Android's SMS log |

## Configuration

Environment variables (optional): `PORT` (default 8080), `SMS_LIMIT` (how many SMS to read, default 2000), `DB_PATH` (SQLite location).

Note: the dashboard UI is in Spanish.
