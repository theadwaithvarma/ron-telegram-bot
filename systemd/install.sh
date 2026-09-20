#!/bin/bash
# Reinstall Ron's systemd units after a host reboot.
#
# /etc on this host is EPHEMERAL — host reboots wipe /etc/systemd/system,
# which is how the bot lost its watchdog unit on 2026-09-20. The files in
# this directory are the persistent canonical copies; this script puts
# them back and starts everything. Run it after any host reboot, or any
# time `systemctl status ron-watchdog` says "could not be found".
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
if [ ! -f /home/hatch/.config/ron/env ]; then
  echo "FATAL: /home/hatch/.config/ron/env missing — refusing to start the bot without its key/proxy env." >&2
  exit 1
fi
for u in ron-watchdog.service ron-backup.service ron-backup.timer; do
  cp "$SRC/$u" "/etc/systemd/system/$u"
  chmod 644 "/etc/systemd/system/$u"
done
systemctl daemon-reload
systemctl enable --now ron-watchdog.service
systemctl enable --now ron-backup.timer
# A restart (not just start) guarantees the daemon picks up the new units.
systemctl restart ron-watchdog.service
sleep 3
echo "watchdog: $(systemctl is-active ron-watchdog.service)"
echo "backup timer: $(systemctl is-active ron-backup.timer)"
