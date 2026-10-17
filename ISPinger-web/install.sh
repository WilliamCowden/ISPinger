#!/usr/bin/env bash
# ISPinger Web installer for Ubuntu / Debian (VM or LXC).
# Run from the folder containing ispinger_web.py:   sudo bash install.sh
# Re-run it any time to update the script; your data and login are kept.
set -euo pipefail

PORT="${PORT:-8080}"
APP_DIR=/opt/ispinger
ENV_FILE=/etc/ispinger.env
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ $EUID -ne 0 ]]; then echo "Run this with sudo: sudo bash install.sh"; exit 1; fi
if [[ ! -f "$SRC_DIR/ispinger_web.py" ]]; then echo "ispinger_web.py must be in the same folder as install.sh"; exit 1; fi

echo "Installing python3 and ping..."
apt-get update -qq
apt-get install -y -qq python3 iputils-ping >/dev/null

echo "Creating service user and $APP_DIR..."
id ispinger &>/dev/null || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin ispinger
install -d -o ispinger -g ispinger -m 750 "$APP_DIR"
install -o ispinger -g ispinger -m 644 "$SRC_DIR/ispinger_web.py" "$APP_DIR/ispinger_web.py"

if [[ ! -f "$ENV_FILE" ]]; then
  echo
  echo "Set the login for the web page."
  read -rp "  Username [admin]: " WEB_USER; WEB_USER=${WEB_USER:-admin}
  while true; do
    read -rsp "  Password: " WEB_PASS; echo
    [[ -n "$WEB_PASS" ]] && break
    echo "  Password can't be empty."
  done
  esc() { local s=${1//\\/\\\\}; printf '%s' "${s//\"/\\\"}"; }
  printf 'ISPINGER_USER="%s"\nISPINGER_PASSWORD="%s"\n' "$(esc "$WEB_USER")" "$(esc "$WEB_PASS")" > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
fi

echo "Writing systemd service..."
cat > /etc/systemd/system/ispinger.service <<EOF
[Unit]
Description=ISPinger web IP watcher
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ispinger
Group=ispinger
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=/usr/bin/python3 $APP_DIR/ispinger_web.py --host 0.0.0.0 --port $PORT --data $APP_DIR/ispinger-data.json
Restart=on-failure
RestartSec=5
# Lets ping work as the unprivileged service user, nothing more
AmbientCapabilities=CAP_NET_RAW
CapabilityBoundingSet=CAP_NET_RAW
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable ispinger >/dev/null
systemctl restart ispinger

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow "$PORT"/tcp >/dev/null && echo "Opened port $PORT in UFW."
fi

sleep 1
if systemctl is-active --quiet ispinger; then
  IP=$(hostname -I | awk '{print $1}')
  echo
  echo "ISPinger is running. Open http://$IP:$PORT in a browser."
else
  echo "The service didn't start. See: journalctl -u ispinger -n 30"
  exit 1
fi
