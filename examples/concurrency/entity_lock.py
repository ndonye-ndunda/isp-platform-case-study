"""Per-entity distributed mutex with a typed failure taxonomy.

Sanitized illustrative example — not production source.

In production this wraps the Redis client library's own Lock rather than
hand-rolling SET NX plus a release: correct distributed locking requires that
release be an *atomic* check-and-delete, because a lock whose lease expired and
was re-acquired by someone else must not be releasable by the original holder.
The library does that with an internal script and is well tested. Knowing which
wheels not to reinvent is part of the job -- `SET NX` plus a hand-written
release is a plausible-looking twenty lines that is wrong in a way you discover
during an incident.

This example is written against a small Protocol so it runs with no services.
The design points it demonstrates are the ones that matter (docs/04):

  * scoped per entity, so unrelated work proceeds in parallel
  * a lease, not a lock -- a crashed holder must not lock an entity out forever
  * TWO exception types, because contention and dependency-failure need
    opposite responses
  * no internal retry -- retry policy belongs to the caller
  * not reentrant, acquired once at the outermost boundary
  * held-duration instrumented rather than adding a watchdog nobody has
    justified yet
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from typing import Protocol

__all__ = [
    "LockBackend",
    "LockTimeout",
    "LockUnavailable",
    "entity_lock",
]

DEFAULT_LEASE_SECONDS = 30.0
DEFAULT_ACQUIRE_TIMEOUT_SECONDS = 5.0
HELD_WARNING_SECONDS = 5.0


class LockTimeout(RuntimeError):
    """Someone else holds the lock for this entity.

    This is NORMAL. It means the system is doing what it was designed to do:
    serialising two operations on the same entity. The correct caller response
    is to back off and retry, or to skip this entity in a batch job and pick it
    up next run.
    """


class LockUnavailable(RuntimeError):
    """The lock backend itself is unreachable.

    This is NOT normal and must not be retried in a tight loop -- a retry
    against a dead backend is an infinite loop that looks exactly like heavy
    contention. Escalate: a dependency is down and no state change is safe
    until it is back.

    Keeping this distinct from LockTimeout is the whole point of having two
    types. A single generic LockError lets a caller handle "someone else is
    busy" and "the coordination layer is gone" identically, and one of those two
    handlings is always wrong.
    """


class LockBackend(Protocol):
    """The minimum surface a lock backend must provide."""

    async def acquire(self, key: str, *, lease: float, wait: float) -> str | None:
        """Return an ownership token, or None if `wait` elapsed. Raise on outage."""
        ...

    async def release(self, key: str, token: str) -> None:
        """Release only if `token` still owns `key`. Must be atomic."""
        ...


@asynccontextmanager
async def entity_lock(
    backend: LockBackend,
    entity_id: str,
    *,
    lease: float = DEFAULT_LEASE_SECONDS,
    wait: float = DEFAULT_ACQUIRE_TIMEOUT_SECONDS,
    on_long_hold: Callable[[str, float], None] | None = None,
) -> AsyncIterator[None]:
    """Hold the lock for one entity for the duration of the block.

    Not reentrant. Acquiring this for an entity while already holding it for
    the same entity self-deadlocks: the inner acquire waits `wait` and then
    raises LockTimeout. There is no code-level guard for that -- it is an
    architectural rule maintained by design and review, and it is stated in the
    docstring precisely because it cannot be enforced by the type system.

    Raises:
        LockTimeout: another holder still had it after `wait` seconds.
        LockUnavailable: the backend could not be reached at all.
    """
    key = f"entity_lock:{entity_id}"

    try:
        token = await backend.acquire(key, lease=lease, wait=wait)
    except LockUnavailable:
        raise
    except Exception as exc:
        # Any backend-level error is a dependency outage, not contention.
        # Mapping it to LockUnavailable here means callers only ever see the
        # two documented types and cannot accidentally treat an outage as a
        # busy entity.
        raise LockUnavailable(f"lock backend unreachable for {key}") from exc

    if token is None:
        raise LockTimeout(f"{key} held elsewhere after {wait}s")

    started = time.monotonic()
    try:
        yield
    finally:
        held = time.monotonic() - started
        # Instrument, don't extend. If this warning never fires, a watchdog was
        # never needed; if it fires often, there is now evidence for building
        # one. Cheap measurement before speculative complexity.
        if held > HELD_WARNING_SECONDS and on_long_hold is not None:
            on_long_hold(key, held)
        # Release in `finally` but never mask the body's exception: a failure
        # to release is a backend problem, and the caller needs the original
        # error, which is the one that explains what actually went wrong. The
        # lease expires on its own, so a missed release costs at most one TTL.
        with suppress(Exception):
            await backend.release(key, token)
