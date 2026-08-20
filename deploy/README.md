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
   --remote-debugging-port=9223 --proxy-server=$PROXY_URL` and log into
   Viagogo only.

5. Point the scraper at the Viagogo Chrome by uncommenting in `.env`:

   ```
   KARTIS_CDP_URL_VIAGOGO=http://localhost:9223
   ```

6. Restart Flask:

   ```sh
   sudo systemctl restart kartis-flask
   ```

7. Trigger a resync; watch the logs for Viagogo. Successful scrape =
   you're done. If it still 403s, the IP from this proxy session may be
   flagged — rotate the session in IPRoyal's dashboard and retry.

---

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

**B2 sync silently doing nothing.**
`rclone config show b2` to confirm the remote exists; `--max-age 36h`
in the script means only files modified in the last 36 h are pushed.
