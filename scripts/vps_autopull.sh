#!/usr/bin/env bash
# VPS auto-deploy: pull the tracked branch and restart kartis-flask when new
# commits land. Run by kartis-autopull.timer every 5 minutes (see
# scripts/kartis-autopull.{service,timer}; install by copying both to
# /etc/systemd/system/ and `systemctl enable --now kartis-autopull.timer`).
#
# Mirrors supervisor.py's watcher-mode flow: fetch → ff-only pull →
# reinstall deps if requirements.txt changed → restart the service. A failed
# pull (diverged history, network) leaves the running app untouched.
set -u
cd /opt/kartis || exit 1

git fetch --quiet || exit 0            # transient network failure — try next tick
LOCAL=$(git rev-parse @)
REMOTE=$(git rev-parse @{u} 2>/dev/null) || exit 0
[ "$LOCAL" = "$REMOTE" ] && exit 0     # nothing new

OLD_REQ=$(git rev-parse @:requirements.txt 2>/dev/null || echo none)
git pull --ff-only --quiet || { echo "autopull: ff-only pull failed (diverged?)"; exit 1; }
NEW_REQ=$(git rev-parse @:requirements.txt 2>/dev/null || echo none)

if [ "$OLD_REQ" != "$NEW_REQ" ]; then
    echo "autopull: requirements.txt changed — reinstalling deps"
    .venv/bin/pip install -q -r requirements.txt || echo "autopull: pip install failed — restarting anyway"
fi

echo "autopull: deployed $(git log -1 --oneline)"
sudo -n systemctl restart kartis-flask
