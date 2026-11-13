#!/usr/bin/env bash
# Installiert/aktualisiert den Highlight Checker als systemd-Dienst.
# Aufruf im Projektordner:  bash deploy/install.sh [PORT]
set -euo pipefail
PORT="${1:-${PORT:-8095}}"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(id -un)"

cd "$DIR"
if [ ! -x .venv/bin/python ]; then
  sudo apt-get install -y python3-venv >/dev/null 2>&1 || true
  python3 -m venv .venv
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
mkdir -p data && chmod 700 data

sed -e "s#__USER__#$USER_NAME#g" -e "s#__DIR__#$DIR#g" -e "s#__PORT__#$PORT#g" \
  deploy/highlight-checker.service | sudo tee /etc/systemd/system/highlight-checker.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now highlight-checker
sudo systemctl restart highlight-checker
sleep 2
systemctl --no-pager --lines=5 status highlight-checker || true
echo
echo "Läuft auf: http://$(hostname -I | awk '{print $1}'):$PORT"
