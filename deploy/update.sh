#!/bin/sh
# Pull the latest Kartis code onto the VPS and restart the dashboard.
#
# Nothing on the VPS does this on its own: supervisor.py only babysits
# watcher_only.py (and doesn't run here anyway), and kartis-flask.service
# just keeps gunicorn alive. Gunicorn compiles the Jinja templates once
# and caches them, so a bare `git pull` changes NOTHING visible until the
# service restarts — forgetting that restart is the whole reason this
# script exists.
#
# Usage (on the VPS):
#   /opt/kartis/deploy/update.sh
#
# Does nothing and leaves the service alone when there's nothing to pull,
# so it's safe to run on a whim or from cron.
#
# Overridable: KARTIS_REPO_DIR, KARTIS_SERVICE, KARTIS_HEALTH_URL.

set -eu

REPO_DIR="${KARTIS_REPO_DIR:-/opt/kartis}"
SERVICE="${KARTIS_SERVICE:-kartis-flask}"
# Gunicorn's own bind, inside the box — Caddy's basic auth sits in front of
# this, so checking here keeps the health probe credential-free.
HEALTH_URL="${KARTIS_HEALTH_URL:-http://127.0.0.1:8000/}"

# Only `systemctl restart` needs root; the pull and pip run as the repo owner.
if [ "$(id -u)" = "0" ]; then
    SUDO=""
else
    SUDO="sudo"
fi

cd "$REPO_DIR"

OLD_REV="$(git rev-parse HEAD)"
echo "current:  $(git log -1 --format='%h %s' "$OLD_REV")"

git pull --ff-only
NEW_REV="$(git rev-parse HEAD)"

if [ "$OLD_REV" = "$NEW_REV" ]; then
    echo "already up to date — leaving $SERVICE alone"
    exit 0
fi

echo "updated:  $(git log -1 --format='%h %s' "$NEW_REV")"

# Reinstall only when the pinned set actually moved. pip on every deploy
# costs a minute for nothing, and on a deploy that didn't touch deps it's
# a chance to drag in a surprise version.
if ! git diff --quiet "$OLD_REV" "$NEW_REV" -- requirements.txt; then
    echo "requirements.txt changed — installing"
    "$REPO_DIR/.venv/bin/pip" install -r requirements.txt
fi

echo "restarting $SERVICE"
$SUDO systemctl restart "$SERVICE"

# app.py starts APScheduler at import, so the first response lags the
# restart by a couple of seconds. Poll rather than sleeping a fixed guess.
i=0
while [ "$i" -lt 30 ]; do
    # -s not -sS: a failed attempt is expected while it boots, and 30 curl
    # error lines would bury the one warning that matters.
    if curl -fs -o /dev/null --max-time 5 "$HEALTH_URL"; then
        echo "$SERVICE is up — now at $(git log -1 --format='%h %s')"
        exit 0
    fi
    i=$((i + 1))
    sleep 1
done

# Deliberately loud and non-zero: a silent failure here leaves the old UI
# serving while the deploy looks like it worked.
echo "WARNING: $SERVICE did not answer $HEALTH_URL within 30s" >&2
echo "check: journalctl -u $SERVICE -n 50 --no-pager" >&2
exit 1
