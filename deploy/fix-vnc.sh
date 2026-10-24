#!/bin/bash
# One-shot repair for the black noVNC screen on the VPS. Safe to re-run.
#
# Usage (on the VPS):
#   cd /opt/kartis && sudo -u kartis git pull --ff-only && sudo bash deploy/fix-vnc.sh
#
# What a black noVNC screen means: x11vnc is showing the bare Xvfb root, i.e.
# no Chrome window is on the display. On the 4 GB box that's almost always
# memory: the OOM killer takes Chrome — or Xvfb itself, which drops every
# window at once — and noVNC keeps connecting to an empty display. This
# script:
#   1. installs the missing X tools (x11vnc, xdotool, openbox, xsetroot)
#   2. adds a 2 GB swapfile if the box has no swap (the 4 GB box had none,
#      so any spike went straight to the OOM killer)
#   3. kills orphaned browser drivers left behind by failed launches
#   4. installs the display units (Xvfb/openbox/x11vnc/noVNC) with the OOM
#      killer told to never pick them, and drop-ins capping each Chrome's
#      memory and making Chrome the OOM killer's first choice. The Chrome
#      units themselves are NOT replaced — the box's copies may point at a
#      different browser binary than the repo's.
#   5. lets the kartis user restart those units without a password, so the
#      dashboard's "Fix Screen" button can repair the display on its own
#   6. restarts the whole stack bottom-up and prints a health report
set -u

REPO_DIR="${KARTIS_REPO_DIR:-/opt/kartis}"
RUN_AS="${KARTIS_USER:-kartis}"
UNIT_DIR=/etc/systemd/system

if [ "$(id -u)" != "0" ]; then
    echo "run with sudo: sudo bash $0" >&2
    exit 1
fi
cd "$REPO_DIR" || exit 1

step() { printf '\n== %s\n' "$*"; }
has_unit() { [ -f "$UNIT_DIR/$1.service" ]; }

step "packages"
DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
    x11vnc xdotool openbox x11-xserver-utils novnc websockify >/dev/null \
    && echo "ok" || echo "apt-get failed (continuing with what's installed)"

step "swap"
if [ "$(awk '/SwapTotal/ {print $2}' /proc/meminfo)" = "0" ]; then
    if [ ! -f /swapfile ]; then
        fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
        chmod 600 /swapfile
        mkswap /swapfile >/dev/null
    fi
    swapon /swapfile && echo "enabled 2 GB /swapfile"
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
else
    echo "swap already present"
fi
# Prefer dropping page cache over swapping Chrome's heap out.
echo 'vm.swappiness=20' > /etc/sysctl.d/90-kartis.conf
sysctl -q -p /etc/sysctl.d/90-kartis.conf

step "orphaned browser drivers"
# A patchright driver/browser whose parent died is re-parented to PID 1 and
# holds its RAM forever. The CDP Chromes are systemd-managed (their parent
# is also 1), so match only patchright's own paths.
ORPHANS=$(ps -eo pid=,ppid=,args= | awk '$2 == 1 && /patchright|ms-playwright/ {print $1}')
if [ -n "$ORPHANS" ]; then
    echo "killing: $ORPHANS"
    kill $ORPHANS 2>/dev/null; sleep 2; kill -9 $ORPHANS 2>/dev/null
else
    echo "none"
fi

step "systemd units"
for u in kartis-xvfb kartis-wm kartis-vnc kartis-novnc; do
    install -m 644 "deploy/systemd/$u.service" "$UNIT_DIR/$u.service" && echo "installed $u"
done

dropin() {  # dropin <unit> <file body>
    has_unit "$1" || return 0
    mkdir -p "$UNIT_DIR/$1.service.d"
    printf '%s\n' "$2" > "$UNIT_DIR/$1.service.d/memory.conf"
    echo "drop-in for $1"
}
dropin kartis-chrome    $'[Service]\nMemoryHigh=1200M\nMemoryMax=1600M\nOOMScoreAdjust=500'
dropin kartis-chrome-cv $'[Service]\nMemoryHigh=700M\nMemoryMax=1000M\nOOMScoreAdjust=500'
dropin kartis-flask     $'[Service]\nOOMScoreAdjust=200'

step "sudoers (dashboard Fix Screen / Restart Chrome buttons)"
SYSTEMCTL=$(command -v systemctl)
CMDS=""
for u in kartis-xvfb kartis-wm kartis-vnc kartis-novnc kartis-chrome kartis-chrome-cv; do
    CMDS="$CMDS${CMDS:+, }$SYSTEMCTL restart $u"
done
TMP=$(mktemp)
echo "$RUN_AS ALL=(root) NOPASSWD: $CMDS" > "$TMP"
if visudo -cf "$TMP" >/dev/null; then
    install -m 440 "$TMP" /etc/sudoers.d/kartis-display && echo "ok"
else
    echo "sudoers rule failed validation — skipped"
fi
rm -f "$TMP"

step "restart display stack"
systemctl daemon-reload
systemctl enable kartis-xvfb kartis-wm kartis-vnc kartis-novnc >/dev/null 2>&1
# Xvfb first: restarting it kills every X client, so everything else after.
for u in kartis-xvfb kartis-wm kartis-chrome kartis-chrome-cv kartis-vnc kartis-novnc; do
    has_unit "$u" || continue
    if [ "$u" = kartis-chrome-cv ] && ! systemctl is-enabled -q "$u"; then continue; fi
    systemctl restart "$u" && echo "restarted $u" || echo "FAILED $u"
    [ "$u" = kartis-xvfb ] && sleep 1
done
# Picks up the new code (Fix Screen endpoint) and its own OOM drop-in.
if has_unit kartis-flask && systemctl is-enabled -q kartis-flask; then
    systemctl restart kartis-flask && echo "restarted kartis-flask"
fi

step "waiting for Chrome"
for i in $(seq 30); do
    curl -s -m 2 http://localhost:9222/json/version >/dev/null && break
    sleep 1
done

step "health"
free -h
echo
for u in kartis-xvfb kartis-wm kartis-chrome kartis-chrome-cv kartis-vnc kartis-novnc kartis-flask; do
    has_unit "$u" && printf '%-18s %s\n' "$u" "$(systemctl is-active "$u")"
done
echo
curl -s -m 3 http://localhost:9222/json/version >/dev/null \
    && echo "Chrome CDP :9222   up" || echo "Chrome CDP :9222   DOWN — journalctl -u kartis-chrome -n 50"
echo
echo "top memory users:"
ps -eo rss=,comm= --sort=-rss | head -8 | awk '{printf "  %6d MB  %s\n", $1/1024, $2}'
echo
echo "recent OOM kills:"
dmesg -T 2>/dev/null | grep -i "killed process" | tail -5 || true
echo
step "display repair (un-minimize / open a Chrome window)"
sudo -u "$RUN_AS" env $(grep -E '^KARTIS_CDP_URL' .env 2>/dev/null | xargs) \
    "$REPO_DIR/.venv/bin/python" display_repair.py --repair | head -40
echo
echo "done — open https://vnc.kartis.homes/vnc.html"
