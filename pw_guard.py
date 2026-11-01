"""Make patchright's sync API raise instead of spinning when its driver dies.

The sync wrapper (`patchright/_impl/_sync_base.py`, `SyncBase._sync`) runs
every call as

    task = loop.create_task(coro)
    while not task.done():
        dispatcher_fiber.switch()

The dispatcher fiber is the greenlet that runs the asyncio loop. When the
node driver process exits, `Connection.run()` returns and that greenlet
FINISHES. Switching into a dead greenlet just returns to the caller, so the
loop never runs again, the task never completes, and the `while` above
becomes a busy loop holding the GIL — forever.

That is exactly what wedged prod from 2026-10-28 06:45 until the 11-01
restart: the vgsales tick's driver crashed during connect_over_cdp
(`ProtocolError (Network.setCacheDisabled): session closed` — an unhandled
rejection in patchright's node code when a tab closes mid-attach), the first
retries raised "Connection closed while reading from the driver", and the
next call spun at 100% CPU with `_vgsales_lock` held. The EDM tick (Tixr in
the same Chrome) did the same at 10-28 ~17:04 — the second hot thread — and
the whole gunicorn worker crawled behind the two.

install() wraps `_sync` so a call on a dead driver raises a normal
exception, which the jobs already handle (status `error`, lock released,
next tick starts a fresh driver). Idempotent; a no-op if patchright isn't
installed or its internals moved.
"""

_installed = False


def install():
    global _installed
    if _installed:
        return
    try:
        from patchright._impl import _sync_base
        from patchright._impl._errors import Error
    except Exception:
        return
    orig = _sync_base.SyncBase._sync

    def _sync(self, coro):
        fiber = getattr(self, "_dispatcher_fiber", None)
        if fiber is None:
            return orig(self, coro)
        if fiber.dead:
            try:
                coro.close()
            except Exception:
                pass
            raise Error("Playwright driver has exited (dispatcher fiber dead)")
        # Same body as the original, plus a dead-fiber exit from the loop.
        import asyncio
        import inspect
        import traceback
        import greenlet
        if self._loop.is_closed():
            coro.close()
            raise Error("Event loop is closed! Is Playwright already stopped?")
        g_self = greenlet.getcurrent()
        task = self._loop.create_task(coro)
        setattr(task, "__pw_stack__", inspect.stack(0))
        setattr(task, "__pw_stack_trace__", traceback.extract_stack(limit=10))
        task.add_done_callback(lambda _: g_self.switch())
        while not task.done():
            fiber.switch()
            if fiber.dead and not task.done():
                raise Error("Playwright driver exited mid-call (dispatcher fiber dead)")
        asyncio._set_running_loop(self._loop)
        return task.result()

    _sync_base.SyncBase._sync = _sync
    _installed = True
