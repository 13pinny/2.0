# Running Claude Code on the kartis box

Why this exists: a Claude Code session in the cloud (claude.ai/code) has **no
route to this machine**. Not a permissions problem — a network one. Measured
from a cloud session on 2026-08-20:

| attempt | result |
|---|---|
| TCP to `kartis.homes:22` / `:2222` | unreachable |
| `CONNECT kartis.homes:22` via the egress proxy | `200 Connection Established`, then no SSH banner |
| same CONNECT to `github.com:22` (control) | **also** 200, also no banner |
| `https://kartis.homes/` | 401 from Caddy — works, but it's only the dashboard |

The control experiment is the point: the proxy answers 200 for *any* CONNECT
and then carries no SSH, so a 200 there proves nothing. Cloud sessions speak
HTTP(S) and nothing else, and the image has no `ssh` client. **Handing out an
SSH key would not have helped.** The only thing that reaches this box is an
agent that already lives on it.

That's what this sets up. Afterwards you can drive the box from claude.ai/code
or the Claude phone app, and — with both ends on Remote Control — another of
your sessions can message it directly.

## What this does NOT solve

The CrowdVolt `stytch_session` cookie still has to come from **your** browser.
It only exists after a Turnstile check and an OTP sent to your phone; no
amount of access to this machine produces one. See the CrowdVolt token section
in `README.md`. An agent here can do everything *around* that step — pull the
branch, install the token you paste, run the doctor, restart services — but
not the login itself.

## Requirements

* Pro or Max plan (Remote Control isn't available on API-key auth).
* Claude Code **v2.1.225+** for a session elsewhere to start a conversation
  with this one. v2.1.224 is the floor for messaging at all; v2.1.232 adds
  `@name` mentions.
* Linux (this box) — fine. Cross-session messaging is macOS/Linux only.
* **`ANTHROPIC_BASE_URL` must be unset or point at `api.anthropic.com`**, and
  none of `DISABLE_TELEMETRY`, `DO_NOT_TRACK`, `DISABLE_GROWTHBOOK`, or
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` may be set — each one disables
  the feature-flag evaluation Remote Control depends on. Check `/opt/kartis/.env`
  and the systemd units before blaming the network.

## 1. Get a terminal — you only have noVNC, and that's fine

You don't need SSH. `kartis-xvfb`, `kartis-wm` (openbox) and `kartis-vnc` are
systemd services, so **the desktop keeps running whether or not a browser tab
is open on it**. Anything you start there survives closing the tab.

Open `https://vnc.kartis.homes/vnc.html`, then right-click the desktop for the
openbox root menu and pick a terminal. If there isn't one:

```sh
sudo apt install -y xterm tmux
```

noVNC is actually the *better* environment for this than SSH would be: the
login step below opens a browser, and there's already a Chrome on that display.

## 2. Install Claude Code

```sh
curl -fsSL https://claude.ai/install.sh | bash     # or: npm i -g @anthropic-ai/claude-code
claude --version                                   # must be >= 2.1.225
```

## 3. Sign in and accept workspace trust

```sh
cd /opt/kartis
claude
```

At the prompt run `/login`. It opens a browser — use the Chrome already on the
noVNC display. Accept the workspace-trust dialog for `/opt/kartis`; Remote
Control refuses to start from an untrusted directory, and trust is never saved
for a home directory, which is why this must run from `/opt/kartis`.

## 4. Start Remote Control, and make it outlive the tab

Run it under tmux so a stray window close doesn't kill it:

```sh
tmux new -s claude
cd /opt/kartis
claude remote-control --name kartis-box
```

`Ctrl-B D` to detach, `tmux attach -t claude` to come back. Server mode prints
a session URL and toggles a QR code with the spacebar — scan it to get the box
in the Claude app on your phone.

Two modes, pick deliberately:

* `claude remote-control` — **server mode**, no local prompt. Right for a box
  you drive from elsewhere.
* `claude --remote-control` — a normal interactive session that is *also*
  reachable remotely. Right if you want to type at it in noVNC too.

## 5. Let your other sessions message it

Reachability is symmetric and both halves are required: a session elsewhere
sees this one **only when both are connected to Remote Control**. Confirm from
either side with `/list-agents` (alias `/peers`); this box should appear
labelled `Remote Control`. `/status` shows a `Peer address` row when messaging
is live.

For a box nobody is watching, add to `/opt/kartis/.claude/settings.json`:

```json
{
  "crossSessionInbound": "accept"
}
```

Without it the default can **hold** inbound messages behind an approval dialog
that nobody is there to click — and held messages expire after five minutes
(`dialogExpiry`). Read the security note before you set this.

## Security — read this before step 5

This box is not a scratch VM. It holds live Lysted / Viagogo / CrowdVolt
sessions, the auto-pricers that change real listing prices with real money,
Discord webhooks, `kartis.db`, and ticket PDFs. An always-on agent with a shell
here, reachable from any device on the account, is a serious capability.

* **Don't run it with `--dangerously-skip-permissions` or in
  `bypassPermissions`.** The convenience is not worth it on this machine. Note
  the inbound default inverts in bypass mode — a bypassing session holds every
  inbound message — so bypass plus `crossSessionInbound: accept` is exactly the
  combination that turns approval off everywhere at once.
* **`crossSessionInbound: accept` means any of your sessions can task this one
  unattended.** That is the whole point, and it's also the risk. If you'd
  rather approve each one, leave it unset and drive the box from claude.ai/code
  by hand instead.
* To keep messages *from* this box gated, set `"isolatePeerMachines": true` —
  it forces your approval before anything here messages a session on another
  machine, and a `true` from any settings scope wins.
* The pricers' master kill switch and dry-run flags are the real blast-radius
  controls (`cv_pricer_master_enabled`, `cv_pricer_dry_run`, and the viagogo
  equivalents). Don't let an agent flip them as a side effect of "fixing" a
  scrape.
* Messages carry no consent: an incoming message can never answer a permission
  prompt or change `CLAUDE.md`, and permission prompts still fire for whatever
  it asks for. That's a floor, not a substitute for the above.

## Verify

From the box:

```sh
claude --version          # >= 2.1.225
/list-agents              # inside the session: shows peers
/status                   # 'Peer address' row present == messaging is live
```

From claude.ai/code or the phone app: the session appears in the list under the
name from `--name`. Send it something harmless — `run: git -C /opt/kartis status`
— and watch it execute in the noVNC terminal.

## Known-good use for this

Once it's up, the CrowdVolt token flow collapses to: paste
`scripts/cv_token_grab.js` into your laptop's DevTools, then tell the box's
Claude to run `scripts/setup_crowdvolt_token.sh` with what you copied. Still
your cookie, still your paste — but no shell work on your side.
