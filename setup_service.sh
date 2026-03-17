#!/bin/bash
# Start connect-on-device web server with auto-restart
# Called from /data/continue.sh on every boot

DIR="$(cd "$(dirname "$0")" && pwd)"

# Redirect port 80 → 8082 so http://<device-ip>/ works without typing the port
# Use iptables-legacy on C3/AGNOS; fall back to iptables on other platforms
_ipt=$(command -v iptables-legacy 2>/dev/null || command -v iptables 2>/dev/null)
if [[ -n "$_ipt" ]]; then
  sudo "$_ipt" -t nat -C PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 8082 2>/dev/null || \
    sudo "$_ipt" -t nat -A PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 8082
  sudo "$_ipt" -t nat -C OUTPUT -p tcp --dport 80 -d 127.0.0.1 -j REDIRECT --to-port 8082 2>/dev/null || \
    sudo "$_ipt" -t nat -A OUTPUT -p tcp --dport 80 -d 127.0.0.1 -j REDIRECT --to-port 8082
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
