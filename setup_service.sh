#!/bin/bash
# Start connect-on-device web server with auto-restart
# Called from /data/continue.sh on every boot

DIR="$(cd "$(dirname "$0")" && pwd)"

# Redirect port 80 → 8082 so http://<device-ip>/ works without typing the port
# Applied with -C check first to avoid duplicate rules across restarts
if command -v iptables &>/dev/null; then
  iptables -t nat -C PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 8082 2>/dev/null || \
    iptables -t nat -A PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 8082
  iptables -t nat -C OUTPUT -p tcp --dport 80 -d 127.0.0.1 -j REDIRECT --to-port 8082 2>/dev/null || \
    iptables -t nat -A OUTPUT -p tcp --dport 80 -d 127.0.0.1 -j REDIRECT --to-port 8082
fi

# Kill any leftover server processes
pkill -9 -f 'python.*server\.py' 2>/dev/null || true

# Start server with auto-restart loop (nohup survives exec)
nohup bash -c "
  cd $DIR
  while true; do
    /usr/local/venv/bin/python -u server.py >> /tmp/connect.log 2>&1
    sleep 3
  done
" &>/dev/null &
