#!/data/data/com.termux/files/usr/bin/bash
# SMS Dashboard installation for Termux
set -e

echo "==> Updating packages…"
pkg update -y

echo "==> Installing dependencies (python, termux-api)…"
pkg install -y python termux-api

echo "==> Installing Flask…"
pip install flask

echo ""
echo "==> Testing SMS access (Android will ask for permissions the first time)…"
if termux-sms-list -l 1 > /dev/null 2>&1; then
  echo "    ✔ SMS read OK"
else
  echo "    ✖ Could not read SMS."
  echo "      1. Make sure the Termux:API APP is installed (from F-Droid)."
  echo "      2. Go to Android Settings > Apps > Termux:API > Permissions and grant"
  echo "         FULL permissions: SMS, Contacts AND Phone (calls). All three are"
  echo "         required before using the app — sending fails silently otherwise."
  echo "      3. Run again: bash install.sh"
fi

echo ""
echo "Installation done. Start the server with:  bash start.sh"
