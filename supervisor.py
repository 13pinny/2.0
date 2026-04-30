"""Self-updating supervisor for the headless watcher.

What it does, in a single process:
  1. Spawns watcher_only.py as a child.
  2. Every PULL_INTERVAL seconds, runs `git pull --ff-only`.
  3. If new commits arrived, terminates the child, refreshes
     dependencies (in case requirements.txt changed), and starts a fresh
     watcher.
  4. If the child crashes for any other reason, restarts it (with a
     simple backoff so a broken commit doesn't burn CPU in a tight loop).

Designed to be the single thing Task Scheduler launches at boot. No
webhooks, no inbound ports — the home server only needs outbound git
access. Works for both public and private GitHub repos as long as the
first manual `git pull` succeeded (Git Credential Manager caches the
credentials after that).

Logs to stdout — redirect with `>> supervisor.log 2>&1` in the .bat
when running unattended.
"""
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

REPO_DIR = Path(__file__).parent
PYTHON = REPO_DIR / ".venv" / "Scripts" / "python.exe"
WATCHER = REPO_DIR / "watcher_only.py"
load_dotenv(REPO_DIR / ".env")

PULL_INTERVAL = int(os.getenv("KARTIS_PULL_INTERVAL_SECONDS") or 300)
MAX_CRASH_BACKOFF = 3600  # cap exponential backoff at 1 hour
GIT_BRANCH = os.getenv("KARTIS_GIT_BRANCH")  # optional pin; default = current


def log(msg):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def run_git(*args):
    return subprocess.run(
        ["git", *args], cwd=REPO_DIR, capture_output=True, text=True,
    )


def git_pull():
    """Returns True iff `git pull` brought new commits."""
    before = run_git("rev-parse", "HEAD").stdout.strip()
    cmd = ["pull", "--ff-only"]
    if GIT_BRANCH:
        cmd += ["origin", GIT_BRANCH]
    res = run_git(*cmd)
    if res.returncode != 0:
        log(f"git pull failed (rc={res.returncode}): {res.stderr.strip() or res.stdout.strip()}")
        return False
    after = run_git("rev-parse", "HEAD").stdout.strip()
    return before != after


def reinstall_deps():
    """Run after a pull so requirements.txt changes get picked up. Quiet on
    success; failures don't kill the supervisor — the watcher just starts
    with whatever's installed and may surface the error itself."""
    res = subprocess.run(
        [str(PYTHON), "-m", "pip", "install", "-q", "-r", "requirements.txt"],
        cwd=REPO_DIR, capture_output=True, text=True,
    )
    if res.returncode != 0:
        log(f"pip install failed: {res.stderr.strip()[:300]}")
    else:
        log("dependencies refreshed")


def start_watcher():
    log(f"starting watcher: {PYTHON} {WATCHER}")
    return subprocess.Popen(
        [str(PYTHON), str(WATCHER)], cwd=REPO_DIR,
        stdout=sys.stdout, stderr=sys.stderr,
    )


def stop_watcher(proc, timeout=15):
    if proc.poll() is not None:
        return
    log("stopping watcher…")
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        log("watcher didn't terminate, killing")
        proc.kill()


def main():
    log(f"supervisor starting; repo={REPO_DIR} pull_interval={PULL_INTERVAL}s")
    head = run_git("rev-parse", "HEAD").stdout.strip()[:12]
    branch = run_git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    log(f"branch={branch} head={head}")

    proc = start_watcher()
    last_pull = time.time()
    crash_count = 0
    crash_wait_until = 0

    stopping = False

    def shutdown(signum, frame):
        nonlocal stopping
        log(f"caught signal {signum}, shutting down")
        stopping = True
    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, shutdown)

    while not stopping:
        time.sleep(5)

        # 1) restart if the watcher died unexpectedly
        rc = proc.poll()
        if rc is not None and time.time() >= crash_wait_until:
            crash_count += 1
            backoff = min(MAX_CRASH_BACKOFF, 10 * (2 ** (crash_count - 1)))
            log(f"watcher exited rc={rc} (crash #{crash_count}); restarting in {backoff}s")
            crash_wait_until = time.time() + backoff
            time.sleep(backoff if backoff <= 30 else 30)
            proc = start_watcher()
            continue
        if rc is None:
            # alive — reset crash counter once it's been up for a minute
            crash_count = 0

        # 2) periodic git pull
        if time.time() - last_pull >= PULL_INTERVAL:
            last_pull = time.time()
            try:
                if git_pull():
                    new_head = run_git("rev-parse", "HEAD").stdout.strip()[:12]
                    log(f"new commit pulled: {new_head} — restarting watcher")
                    stop_watcher(proc)
                    reinstall_deps()
                    proc = start_watcher()
            except Exception as e:
                log(f"pull/restart cycle failed: {type(e).__name__}: {e}")

    stop_watcher(proc)
    log("supervisor stopped")


if __name__ == "__main__":
    main()
