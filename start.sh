#!/data/data/com.termux/files/usr/bin/bash
# Starts the SMS Dashboard and prevents Android from putting the process to sleep
cd "$(dirname "$0")"
termux-wake-lock
echo "IPs of this phone (use the Tailscale one, 100.x.x.x, from your PC):"
ifconfig 2>/dev/null | grep -Eo 'inet (100|192)\.[0-9.]+' | awk '{print "  http://" $2 ":8080"}' || true
python server.py
