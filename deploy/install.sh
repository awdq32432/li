#!/usr/bin/env bash
# Installs and starts the recorder (with the live shadow engine) and the hourly report
# as systemd services. Public market data only: no keys are read or needed.
#   sudo bash deploy/install.sh
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_AS="${SUDO_USER:-$(id -un)}"

sudo -u "$RUN_AS" python3 -m venv "$DIR/.venv" 2>/dev/null || { apt-get install -y python3-venv && sudo -u "$RUN_AS" python3 -m venv "$DIR/.venv"; }
sudo -u "$RUN_AS" "$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"
sudo -u "$RUN_AS" "$DIR/.venv/bin/python" -m unittest discover -s "$DIR/tests" -t "$DIR" 2>&1 | tail -3

unit() {  # name, description, module and arguments
cat > "/etc/systemd/system/$1.service" <<UNIT
[Unit]
Description=$2
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_AS
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python -u -m $3
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
}
unit leadlag-recorder "Lead-lag recorder and shadow engine (no orders)" "leadlag.recorder"
unit leadlag-report "Lead-lag hourly report" "leadlag.report --loop"
systemctl daemon-reload
systemctl enable --now leadlag-recorder leadlag-report
sleep 60
echo "--- recorder log, last lines ---"
journalctl -u leadlag-recorder -n 15 --no-pager | cut -c1-260
echo "--- first report (last 1 minute) ---"
sudo -u "$RUN_AS" "$DIR/.venv/bin/python" -m leadlag.report --minutes 1 | sed -n '/^FEEDS/,/^STATE/p'
