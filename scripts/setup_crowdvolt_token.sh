#!/usr/bin/env bash
# Install a CrowdVolt session token on the VPS and verify it end to end.
#
# Usage (on the box, as any user with sudo):
#
#     bash /opt/kartis/scripts/setup_crowdvolt_token.sh
#
# It reads the two KARTIS_CV_* lines from stdin (paste them, then Ctrl-D),
# writes /opt/kartis/.env.crowdvolt as kartis:kartis 0600, and runs the
# doctor. Get the lines by pasting scripts/cv_token_grab.js into the DevTools
# console of a desktop browser logged into crowdvolt.com.
#
# Nothing here touches Chrome, Xvfb, noVNC or the residential proxy — that is
# the entire point: api.crowdvolt.com is not IP-gated, only the interactive
# login is (Turnstile), so the session is minted where Turnstile passes.
set -euo pipefail

APP_DIR="${KARTIS_DIR:-/opt/kartis}"
ENV_FILE="$APP_DIR/.env.crowdvolt"
PY="$APP_DIR/.venv/bin/python"
OWNER="${KARTIS_USER:-kartis}"

[ -d "$APP_DIR" ] || { echo "no $APP_DIR — set KARTIS_DIR" >&2; exit 1; }
[ -x "$PY" ] || { echo "no venv python at $PY" >&2; exit 1; }

echo "Paste the two KARTIS_CV_* lines from cv_token_grab.js, then press Ctrl-D:"
INPUT=$(cat)

TOKEN=$(printf '%s\n' "$INPUT" | sed -n 's/^[[:space:]]*KARTIS_CV_TOKEN=//p' | tr -d '"'"'"'\r' | head -1)
FP=$(printf '%s\n'    "$INPUT" | sed -n 's/^[[:space:]]*KARTIS_CV_FINGERPRINT=//p' | tr -d '"'"'"'\r' | head -1)

if [ -z "${TOKEN:-}" ]; then
    echo "error: no KARTIS_CV_TOKEN= line found in what you pasted." >&2
    echo "       Expected two lines exactly as cv_token_grab.js printed them." >&2
    exit 1
fi

# Back up a working token before overwriting — if the new one turns out to be
# stale, the old one may still have life left.
if [ -f "$ENV_FILE" ]; then
    sudo cp -a "$ENV_FILE" "$ENV_FILE.prev"
    echo "previous token backed up to $ENV_FILE.prev"
fi

sudo install -o "$OWNER" -g "$OWNER" -m 600 /dev/null "$ENV_FILE"
printf 'KARTIS_CV_TOKEN=%s\nKARTIS_CV_FINGERPRINT=%s\n' "$TOKEN" "$FP" \
    | sudo tee "$ENV_FILE" >/dev/null
sudo chmod 600 "$ENV_FILE"
sudo chown "$OWNER:$OWNER" "$ENV_FILE"
echo "wrote $ENV_FILE (owner $OWNER, mode 600)"
echo

sudo -u "$OWNER" "$PY" "$APP_DIR/crowdvolt_sales.py" --doctor || {
    echo
    echo "The doctor reported a problem — the token was NOT accepted." >&2
    echo "Most likely the cookie was copied from a logged-out tab, or it has" >&2
    echo "already expired. Re-run cv_token_grab.js and try again." >&2
    exit 1
}

echo
echo "CrowdVolt sales tracking is live over the token transport."
echo "Restarting kartis-flask so the scheduler picks it up..."
sudo -n systemctl restart kartis-flask 2>/dev/null \
    && echo "kartis-flask restarted." \
    || echo "(couldn't restart kartis-flask automatically — run:"\
            "sudo systemctl restart kartis-flask)"
