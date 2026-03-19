#!/bin/bash
# Start connect-on-device web server with auto-restart
# Called from /data/continue.sh on every boot

DIR="$(cd "$(dirname "$0")" && pwd)"

# Advertise only A (IPv4) records on IPv4 networks so cateye.local resolves to
# the IPv4 address.  This prevents iOS from resolving to IPv6 link-local, which
# causes WebRTC ICE candidate family mismatch and ~20s connection delays.
_avahi_conf=/etc/avahi/avahi-daemon.conf
if grep -q 'publish-aaaa-on-ipv4=yes\|#publish-aaaa-on-ipv4\|use-ipv6=no' "$_avahi_conf" 2>/dev/null; then
  sudo mount -o remount,rw / 2>/dev/null || true
  sudo sed -i 's/#\?publish-aaaa-on-ipv4=.*/publish-aaaa-on-ipv4=no/; s/use-ipv6=no/use-ipv6=yes/' "$_avahi_conf"
  sudo systemctl restart avahi-daemon 2>/dev/null || true
fi

# Set mDNS hostname from config.py so the device is reachable as http://cateye.local
# Done here (from /data) so it survives OTA updates that reset /etc/hostname.
_hostname=$(cd "$DIR" && python3 -c "from config import DEVICE_HOSTNAME; print(DEVICE_HOSTNAME)" 2>/dev/null || echo "cateye")
sudo hostnamectl set-hostname "$_hostname" 2>/dev/null || true

# Grant Python the capability to bind port 80 directly (works for both IPv4 and
# IPv6, no iptables NAT needed).  The root filesystem may be read-only until
# remounted; retry once after remounting.
_py=$(readlink -f /usr/local/venv/bin/python)
sudo setcap cap_net_bind_service=+ep "$_py" 2>/dev/null || {
  sudo mount -o remount,rw / 2>/dev/null
  sudo setcap cap_net_bind_service=+ep "$_py"
}

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
