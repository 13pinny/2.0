# Kartis VPS deploy

Bootstrap a fresh Hetzner CAX11 (or any small Ubuntu 24.04 box) to host
Flask + scraper + Chrome. The DB stays SQLite (`kartis.db`) and lives on
the same disk. Local Windows dev keeps working unchanged — these env
vars all have safe defaults.

## Bill of materials (Phase 1)

- 1 × Hetzner CAX11 (ARM, 2 vCPU, 4 GB RAM) — €3.79/mo
- 1 × domain (Porkbun / Cloudflare Registrar `.com`) — ~$10/yr
- Backblaze B2 bucket (or any S3-compatible) for off-site backups — <$1/mo

## 1. Provision the box

```sh
# Hetzner Cloud → CAX11, Ubuntu 24.04 LTS, add your SSH key.
ssh root@<vps-ip>
adduser --disabled-password --gecos "" kartis
usermod -aG sudo kartis
mkdir -p /home/kartis/.ssh
cp /root/.ssh/authorized_keys /home/kartis/.ssh/
chown -R kartis:kartis /home/kartis/.ssh
chmod 700 /home/kartis/.ssh
chmod 600 /home/kartis/.ssh/authorized_keys

# Disable root SSH; force key auth.
sed -i 's/^#\?PermitRootLogin .*/PermitRootLogin no/' /etc/ssh/sshd_config
sed -i 's/^#\?PasswordAuthentication .*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl reload ssh

# Firewall: just SSH + HTTPS.
ufw allow OpenSSH
ufw allow 443/tcp
ufw allow 80/tcp     # Caddy needs :80 for the Let's Encrypt HTTP-01 challenge
ufw --force enable
```

Re-login as the `kartis` user for the rest.

## 2. System packages

```sh
sudo apt update && sudo apt install -y \
    python3.12 python3.12-venv python3-pip \
    chromium-browser \
    xvfb \
    caddy \
    git \
    rclone \
    tigervnc-standalone-server novnc websockify

# On ARM Hetzner, `chromium-browser` is the snap-free build from the
# Ubuntu universe repo. If `which chromium` returns nothing, try
# `apt install chromium` (some images package it under the bare name).
```

## 3. Clone & install

```sh
sudo mkdir -p /opt/kartis
sudo chown kartis:kartis /opt/kartis
cd /opt/kartis
git clone <your-repo-url> .

python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt gunicorn
patchright install chromium
```

Copy `.env.example` to `.env` and edit:

```sh
cp .env.example .env
vi .env
```

Set at minimum:

```
KARTIS_CHROME_BIN=/usr/bin/chromium
KARTIS_BACKUP_DIR=/opt/kartis/backups
DISCORD_WEBHOOK_URL=...      # if you want drop notifications
GMAIL_USER=...
GMAIL_APP_PASSWORD=...
```

Leave `KARTIS_CDP_URL` and `KARTIS_CDP_URL_VIAGOGO` commented out for
Phase 1 — defaults are correct.

## 4. Migrate the SQLite DB from your laptop

```sh
# from your Windows laptop, in the repo:
scp kartis.db kartis@<vps-ip>:/opt/kartis/kartis.db
```

The Chrome `user_data/` profile does **not** migrate between OSes — Chrome
stores host-specific encryption keys. You'll re-login on the VPS in
step 7 (one-time, ~5 min).

## 5. systemd units

```sh
sudo cp deploy/systemd/kartis-xvfb.service /etc/systemd/system/
sudo cp deploy/systemd/kartis-chrome.service /etc/systemd/system/
sudo cp deploy/systemd/kartis-flask.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kartis-xvfb kartis-chrome
# (Don't start kartis-flask yet — Chrome isn't logged in.)
```

Verify:

```sh
systemctl status kartis-xvfb kartis-chrome
curl -s http://localhost:9222/json/version | head
```

The second command should print JSON with the Chrome version.

## 6. Domain + Caddy + basic auth

Buy the domain. Add an `A` record `kartis.<yourdomain>.com` → VPS IP.

```sh
# Pick a username/password, then:
caddy hash-password
# (paste the password when prompted; it prints the bcrypt hash)

sudo cp deploy/Caddyfile.example /etc/caddy/Caddyfile
sudo vi /etc/caddy/Caddyfile
#   - replace kartis.example.com with your real subdomain
#   - replace `you` and the hash with the username + hash you generated
sudo systemctl reload caddy
```

Once DNS has propagated (usually <5 min), `curl -u you:pw https://kartis.<yourdomain>.com/`
will 502 because Flask isn't running yet — that's expected.

## 7. One-time browser login via noVNC

Chrome on the VPS needs to be logged into Lysted, Viagogo, and CrowdVolt
once. Easiest path is to open a temporary noVNC session and use Chrome
inside your laptop browser.

```sh
# On the VPS:
DISPLAY=:99 chromium &   # opens Chrome on the Xvfb display (no GUI yet)

# Bridge :99 to a VNC port:
x11vnc -display :99 -nopw -listen localhost -xkb &
# (apt install x11vnc if missing)

# Bridge VNC to a websocket noVNC can speak:
websockify --web=/usr/share/novnc/ 6080 localhost:5900 &
```

Tunnel from your laptop:

```sh
ssh -L 6080:localhost:6080 kartis@<vps-ip>
```

Open `http://localhost:6080/vnc.html` in your laptop browser. You'll see
the VPS Xvfb display with Chrome on it. Log into:

1. Lysted (clear the Cloudflare challenge if shown)
2. Viagogo (`inv.viagogo.com`)
3. CrowdVolt

(Once Phase 2/3 are on, Viagogo and CrowdVolt each log in on their own
Chrome instead — see those sections. Only Lysted stays on this one.)

Close the Chrome window when done. Sessions persist in `/opt/kartis/user_data/`.

Kill the noVNC bridge processes (`pkill websockify x11vnc`) — we don't
need them again until a session expires.

Restart Chrome via systemd so it picks up the saved session:

```sh
sudo systemctl restart kartis-chrome
```

## 8. Start Flask

```sh
sudo systemctl enable --now kartis-flask
sudo systemctl status kartis-flask
journalctl -u kartis-flask -f   # watch for the APScheduler boot log
```

Visit `https://kartis.<yourdomain>.com/`, enter your basic-auth creds,
trigger a resync from the UI (or `curl -u you:pw -X POST https://kartis.<yourdomain>.com/api/resync`).

## 9. Backups to B2

```sh
rclone config   # add a remote called "b2"
chmod +x /opt/kartis/deploy/backup-to-b2.sh
crontab -e
# Add:
# 30 3 * * * /opt/kartis/deploy/backup-to-b2.sh >> /var/log/kartis-backup.log 2>&1
```

Kartis itself snapshots `kartis.db` at 03:00 daily via APScheduler
([app.py:3051](../app.py)), so the rclone push at 03:30 picks up a
fresh snapshot.

## 10. Retire the laptop instance

Once the VPS has run a clean scrape and you've poked the UI:

- Stop the local Flask + Chrome on the laptop.
- Disable autostart: undo `install_autostart.bat` (Task Scheduler entry).
- Update your laptop bookmark to `https://kartis.<yourdomain>.com/`.

Laptop RAM should drop by whatever Chrome was using (typically 500 MB–1.5 GB).

---

## Phase 2 — Add residential proxy for Viagogo

Only if Phase 1 still hits Cloudflare 403s on Viagogo after 24–48 h.

1. Sign up at [IPRoyal Royal Residential](https://iproyal.com/residential-proxies/)
   (PAYG, $1.75/GB). Grab `host:port:user:pass`.

2. Create the proxy env file (chmod 600 since it holds creds):

   ```sh
   sudo mkdir -p /etc/kartis
   sudo bash -c 'cat > /etc/kartis/viagogo-proxy.env <<EOF
   PROXY_URL=http://USER:PASS@residential.iproyal.com:12321
   EOF'
   sudo chmod 600 /etc/kartis/viagogo-proxy.env
   ```

3. Install the second Chrome unit and start it:

   ```sh
   sudo cp deploy/systemd/kartis-chrome-viagogo.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now kartis-chrome-viagogo
   ```

4. Re-run the noVNC step (step 7 above) on the Viagogo Chrome — but this
   time start `chromium --user-data-dir=/opt/kartis/user_data_viagogo
   --remote-debugging-port=9224 --proxy-server=$PROXY_URL` and log into
   Viagogo only.

5. Point the scraper at the Viagogo Chrome by uncommenting in `.env`:

   ```
   KARTIS_CDP_URL_VIAGOGO=http://localhost:9224
   ```

   (:9224, not :9223 — :9223 belongs to the CrowdVolt Chrome in Phase 3.)

6. Restart Flask:

   ```sh
   sudo systemctl restart kartis-flask
   ```

7. Trigger a resync; watch the logs for Viagogo. Successful scrape =
   you're done. If it still 403s, the IP from this proxy session may be
   flagged — rotate the session in IPRoyal's dashboard and retry.

---

## Phase 3 — CrowdVolt on its own Chrome (residential proxy)

CrowdVolt's Cloudflare interstitial-challenges the Hetzner IP and the
Turnstile loops even for a human — while Viagogo 403s residential IPs, so
the box IP can't simply move. CrowdVolt therefore gets its own Chrome
(`kartis-chrome-cv`, CDP **:9223**, profile `/opt/kartis/user_data_cv`)
egressing through a paid static-residential proxy, and the main :9222
Chrome keeps the datacenter IP for Lysted/Viagogo.

1. Put the provider's proxy in `/opt/kartis/.env.cvproxy` (gitignored,
   chmod 600 — percent-encode any `:` or `@` inside user/pass):

   ```sh
   sudo -u kartis bash -c 'cat > /opt/kartis/.env.cvproxy <<EOF
   KARTIS_CVPROXY_UPSTREAM=http://USER:PASS@residential.iproyal.com:12321
   EOF'
   sudo chmod 600 /opt/kartis/.env.cvproxy
   ```

   Verify the upstream and see the egress IP before involving Chrome:

   ```sh
   sudo -u kartis /opt/kartis/.venv/bin/python /opt/kartis/cvproxy.py --test
   ```

2. Install and start the units (the forwarder must come up first — Chrome
   silently drops `user:pass` given in `--proxy-server`, so `cvproxy.py`
   injects the credentials on localhost):

   ```sh
   sudo cp deploy/systemd/kartis-cvproxy.service /etc/systemd/system/
   sudo cp deploy/systemd/kartis-chrome-cv.service /etc/systemd/system/
   sudo cp deploy/systemd/kartis-wm.service /etc/systemd/system/   # openbox titlebars
   sudo systemctl daemon-reload
   sudo systemctl enable --now kartis-cvproxy kartis-wm kartis-chrome-cv
   ```

3. Log into CrowdVolt **in that window**: open the noVNC bookmark, pick the
   second Chrome window (offset at 60,60), and sign in. The session lands in
   `user_data_cv` — the main Chrome no longer has a CrowdVolt login at all.

4. Point both the pricer and the sales scrape at it in `/opt/kartis/.env`:

   ```
   KARTIS_CDP_URL_CROWDVOLT=http://localhost:9223
   ```

   Then `sudo systemctl restart kartis-flask`.

### Can't clear Cloudflare to log in? Use a token instead (preferred)

Turnstile — the invisible Cloudflare widget CrowdVolt's login sits behind
(sitekey `0x4AAAAAACEEE5RZU0aYZaTU`) — frequently refuses to clear in the
noVNC Chrome, and no amount of clicking helps. That is a **browser
fingerprint** problem, not the IP: an Xvfb Chrome has software-rendered
WebGL, no GPU, no audio device and `--remote-debugging-port` attached, all
of which Turnstile scores badly. CrowdVolt's own client even has an error
code for it, `cf_not_cleared`.

The IP is not the issue, and this is worth internalising before spending
more on proxies: probed 2026-08-20 from a plain **datacenter** ASN with
`curl` and no browser at all, `api.crowdvolt.com` answered every endpoint
with an ordinary application error — `302 auth_required`, `401 missing
bearer token`, `302 token_rejected` for a stale bearer — and never a
Cloudflare interstitial. `www.crowdvolt.com` served its full pages too.
**Only the interactive login is gated.** So mint the session where Turnstile
is happy (your own laptop) and just carry it to the box:

1. In your normal desktop Chrome, log into crowdvolt.com. Open DevTools
   (F12) -> Console, paste the contents of **`scripts/cv_token_grab.js`**,
   press Enter. It copies the two `KARTIS_CV_*` lines to your clipboard.

2. **Easiest — no shell at all:** open `https://kartis.homes/cvtoken`, paste
   into the box, hit **Save & verify**. It writes `.env.crowdvolt` 0600,
   checks the token against CrowdVolt immediately, and has a *Sync sales now*
   button so you don't wait for the hourly tick. Same basic auth as the rest
   of the dashboard, and the token is never echoed back to the page.

   Or from a terminal on the VPS, paste them into the installer, then Ctrl-D:

   ```sh
   bash /opt/kartis/scripts/setup_crowdvolt_token.sh
   ```

   It writes `/opt/kartis/.env.crowdvolt` as `kartis:kartis` 0600 (backing up
   any previous token to `.prev`), runs the doctor, and restarts
   `kartis-flask`. If the token is rejected it says so and leaves the backup
   in place rather than reporting success.

To re-check at any time — this touches no browser at all:

   ```sh
   sudo -u kartis /opt/kartis/.venv/bin/python /opt/kartis/crowdvolt_sales.py --doctor
   ```

   The doctor verifies the token, fetches sales, and reports **per-column
   fill rates**. That last part matters: if CrowdVolt ever renames a field,
   the doctor names the exact new key to add to the `_F_*` alias tuples
   instead of leaving a quietly blank column. It also probes `auth/refresh`
   to show whether renewal can be automated.

With a token present the hourly sync uses it and **never opens Chrome for
CrowdVolt at all** — no Xvfb, no noVNC, no residential proxy, no Turnstile.
Those stay installed only as the fallback for when the token lapses; if this
holds up you can stop `kartis-chrome-cv` and `kartis-cvproxy` and drop the
proxy bill. When the session does expire the sync says so explicitly
(`token_rejected`) and the dashboard shows LOGIN EXPIRED — repeat steps 1-3.

5. Confirm sales tracking end to end:

   ```sh
   sudo -u kartis /opt/kartis/.venv/bin/python /opt/kartis/crowdvolt_sales.py
   ```

   That prints one line per sale off CrowdVolt's JSON API (Delivered +
   Incomplete tabs). Add `--write` to persist, or `--raw` to dump the raw
   API rows when a column comes back blank. Once it looks right, trigger a
   resync from the dashboard and check `crowdvolt_sales` in the UI.

## Driving the box from a phone or another machine

`CLAUDE_ON_THE_BOX.md` in this directory sets up Claude Code running on this
machine with Remote Control, so you can steer it from claude.ai/code or the
Claude app instead of typing in the noVNC terminal. Relevant because a cloud
Claude session has no route here at all — no SSH from those sandboxes, only
HTTP(S) — so an agent that already lives on the box is the only thing that can
act on it. Read the security section there first: this machine holds live
marketplace logins and the auto-pricers.

## Troubleshooting

**`curl http://localhost:9222/json/version` hangs.**
Chrome's CDP port is stuck. `sudo systemctl restart kartis-chrome`.

**Patchright says "Browser closed" mid-scrape.**
Xvfb died (OOM is the usual culprit on a 2 GB box). Check
`dmesg | grep -i oom`. Upgrade to CPX21/CAX11-with-4GB if it recurs.

**Cloudflare 403 on every Viagogo run from the VPS.**
You're in Phase 2 territory. The Hetzner range is probably flagged for
your Viagogo account specifically. Move to the residential proxy.

**"LOGIN EXPIRED" badge in the UI for one source.**
The session cookie aged out. This is the recurring chore (esp. Lysted).
Once the permanent VNC stack is up (services + `vnc.kartis.homes` Caddy
block), you do **not** need SSH or the manual bridge from step 7 — just
open the bookmark `https://vnc.kartis.homes/vnc.html` (same basic-auth as
the dashboard), re-login in the Chrome window there, then back on the box
`sudo systemctl restart kartis-chrome`. The Chrome restart can also be
triggered from anywhere with `curl`-able access if you wire up an endpoint.

**noVNC bookmark shows "Failed to connect" but services look "active".**
x11vnc drifted off port 5900 (auto-probed to 5901 after a quick restart)
while websockify still bridges 5900. The `kartis-vnc.service` unit pins
`-rfbport 5900` to prevent this; if you see it, confirm with
`ss -tlnp | grep 5900` and that the unit has the `-rfbport 5900` flag.

**CrowdVolt sales stuck at 0.**
Almost always the CDP split: the scrape has to run on the CrowdVolt Chrome
(:9223), not the main one. Check `KARTIS_CDP_URL_CROWDVOLT` is in
`/opt/kartis/.env`, then `curl http://localhost:9223/json/version` and
`systemctl status kartis-cvproxy kartis-chrome-cv`. If those are healthy,
run `crowdvolt_sales.py` by hand — "no stytch_session cookie" means the
CrowdVolt login in `user_data_cv` expired, `token_rejected` means the
`.env.crowdvolt` token did, and rows with blank columns mean CrowdVolt
renamed a field (`--raw` shows the new names). For any login problem prefer
the token route above over fighting Turnstile in noVNC.

**Cloudflare won't let me log into CrowdVolt in the noVNC browser.**
Expected, and not the IP — see "Use a token instead" above; that path skips
the browser entirely. If you specifically want the in-browser login to work,
the single highest-value thing to try is logging in from a Chrome started
**without** `--remote-debugging-port`: stop `kartis-chrome-cv`, run
`DISPLAY=:99 google-chrome-stable --user-data-dir=/opt/kartis/user_data_cv`
by hand, clear Turnstile and sign in there, quit, then
`sudo systemctl start kartis-chrome-cv`. The profile keeps the session and
CDP only has to be absent while the challenge is being solved. Also worth
knowing: a residential proxy can make Turnstile *worse*, since those exit
IPs are shared with abusers — `cvproxy.py --test` prints the egress IP, and
turning the proxy off is a legitimate experiment given the API is not
IP-gated.

**B2 sync silently doing nothing.**
`rclone config show b2` to confirm the remote exists; `--max-age 36h`
in the script means only files modified in the last 36 h are pushed.
