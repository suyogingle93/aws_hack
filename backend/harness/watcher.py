"""Hot-reload the policy file when it changes on disk."""

from __future__ import annotations

import asyncio
import logging
import os

from .events import Event, EventBus
from .policy import PolicyEngine, PolicyError

log = logging.getLogger("harness.watcher")


def _signature(path: os.PathLike | str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


async def watch_policy(engine: PolicyEngine, bus: EventBus, interval: float = 1.0) -> None:
    """Poll the policy file; reload on change. An invalid edit keeps the old policy."""
    if engine.source is None:
        return
    last = _signature(engine.source)
    while True:
        await asyncio.sleep(interval)
        current = _signature(engine.source)
        if current is None or current == last:
            continue
        last = current
        try:
            version = engine.reload()
            reason = f"policy v{version} active (file changed)"
        except PolicyError as e:
            log.warning("policy file changed but is invalid; keeping previous policy: %s", e)
            reason = f"policy file invalid, previous policy v{engine.version} kept: {str(e).splitlines()[0]}"
        bus.publish(Event(run_id="system", kind="policy_reloaded", reason=reason))
