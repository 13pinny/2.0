"""Per-run time limit for the scheduler's fresh-thread jobs (app._fresh_thread_job).

A wedged browser job used to hold its APScheduler slot, its own lock and the
CDP browser lock until the next restart — vgsales sat "running" from
2026-10-28 06:45 to the 11-01 deploy, burning a core the whole time (see
pw_guard.py for that particular wedge). Python can't kill a thread, so when
a run outlives its limit this does the next best things, in order:

1. Print the stuck thread's stack, so the next wedge is diagnosable from
   `journalctl` alone (no profiler on the box).
2. Raise JobAbandoned INSIDE that thread (PyThreadState_SetAsyncExc) so it
   unwinds through its own `finally` blocks and releases its locks — the
   clean path. It is a BaseException on purpose: the retry loops it lands in
   (scraper._connect_over_cdp) catch Exception, and a first version that
   subclassed Exception was swallowed by one and went straight back to
   spinning. It lands at the next Python bytecode, so it frees a thread
   spinning in Python (the patchright busy-loop) but not one blocked inside
   a C call.
3. Release any viagogo browser lock the thread still holds
   (viagogo_listing.abandon_browser_locks) and call the job's on_abandon
   hook, which records the abandon as the run's error, swaps its lock and
   clears `running` (harmless if step 2 already unwound it). A thread still
   alive after GRACE_SECONDS is left to die on its own; if it ever wakes up
   it releases only the old locks it owned.

The scheduler's runner returns either way, so the next tick starts on time.
"""
import ctypes
import sys
import threading
import traceback

GRACE_SECONDS = 30


class JobAbandoned(BaseException):
    """Raised inside a job thread that outlived its watchdog limit."""


def _stack_of(thread):
    frame = sys._current_frames().get(thread.ident)
    if frame is None:
        return "(no frame)"
    return "".join(traceback.format_stack(frame))


def _inject(thread, exc_type):
    n = ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_ulong(thread.ident), ctypes.py_object(exc_type))
    if n > 1:      # should never happen; undo rather than hit other threads
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(thread.ident), None)
        return False
    return n == 1


def run(fn, name, max_seconds=None, on_abandon=None, grace=None):
    """Run fn in a fresh daemon thread and wait; abandon it past max_seconds.
    Returns 'ok', 'interrupted' (unwound by JobAbandoned) or 'abandoned'
    (still stuck; locks reset from outside)."""
    t = threading.Thread(target=fn, daemon=True, name=f"fresh-{name}")
    t.start()
    t.join(max_seconds)
    if not t.is_alive():
        return "ok"
    print(f"[watchdog] {name} still running after {max_seconds:.0f}s — "
          f"abandoning. Stuck at:\n{_stack_of(t)}", flush=True)
    grace = GRACE_SECONDS if grace is None else grace
    _inject(t, JobAbandoned)
    t.join(grace)
    unwound = not t.is_alive()
    freed = []
    try:
        import viagogo_listing
        freed = viagogo_listing.abandon_browser_locks(t.ident)
    except Exception:
        traceback.print_exc()
    if on_abandon is not None:
        try:
            on_abandon(max_seconds)
        except Exception:
            traceback.print_exc()
    if unwound:
        print(f"[watchdog] {name} unwound after JobAbandoned", flush=True)
        return "interrupted"
    print(f"[watchdog] {name} did not unwind within {grace}s (blocked in C?) — "
          f"left it behind; reset its state, browser locks freed: {freed or 'none'}",
          flush=True)
    return "abandoned"
