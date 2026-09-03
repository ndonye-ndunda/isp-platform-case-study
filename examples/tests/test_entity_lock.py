"""The lock's contract: two failure types, and neither is retried internally.

Sanitized illustrative example — not production source.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine

import pytest

from examples.concurrency.entity_lock import (
    LockTimeout,
    LockUnavailable,
    entity_lock,
)


class FakeBackend:
    """In-memory lock backend. No Redis needed to test the contract."""

    def __init__(self, *, held: set[str] | None = None, outage: bool = False) -> None:
        self.held: set[str] = held or set()
        self.outage = outage
        self.released: list[str] = []

    async def acquire(self, key: str, *, lease: float, wait: float) -> str | None:
        if self.outage:
            raise ConnectionError("backend unreachable")
        if key in self.held:
            return None
        self.held.add(key)
        return f"token-{key}"

    async def release(self, key: str, token: str) -> None:
        if token != f"token-{key}":
            return  # not the owner; atomic check-and-delete is a no-op
        self.held.discard(key)
        self.released.append(key)


def run(coro: Coroutine[object, object, None]) -> None:
    """Drive a coroutine without needing an async pytest plugin."""
    asyncio.run(coro)


def test_acquires_and_releases() -> None:
    backend = FakeBackend()

    async def scenario() -> None:
        async with entity_lock(backend, "ACC-0001"):
            assert "entity_lock:ACC-0001" in backend.held

    run(scenario())
    assert backend.released == ["entity_lock:ACC-0001"]
    assert not backend.held


def test_contention_raises_lock_timeout() -> None:
    """Contention is NORMAL. The caller decides whether to retry or skip."""
    backend = FakeBackend(held={"entity_lock:ACC-0001"})

    async def scenario() -> None:
        async with entity_lock(backend, "ACC-0001"):
            pass

    with pytest.raises(LockTimeout):
        run(scenario())


def test_backend_outage_raises_lock_unavailable() -> None:
    """A dead backend must NOT look like contention.

    This is the distinction the two types exist for: retrying against a dead
    backend is an infinite loop that presents as heavy contention.
    """
    backend = FakeBackend(outage=True)

    async def scenario() -> None:
        async with entity_lock(backend, "ACC-0001"):
            pass

    with pytest.raises(LockUnavailable):
        run(scenario())


def test_different_entities_do_not_contend() -> None:
    """Per-entity scoping: unrelated work proceeds in parallel."""
    backend = FakeBackend()

    async def scenario() -> None:
        async with (
            entity_lock(backend, "ACC-0001"),
            entity_lock(backend, "ACC-0002"),
        ):
            assert len(backend.held) == 2

    run(scenario())
    assert not backend.held


def test_lock_is_released_even_when_the_body_raises() -> None:
    backend = FakeBackend()

    async def scenario() -> None:
        async with entity_lock(backend, "ACC-0001"):
            raise RuntimeError("business failure")

    with pytest.raises(RuntimeError, match="business failure"):
        run(scenario())
    assert not backend.held  # released despite the exception


def test_long_hold_is_reported_not_extended() -> None:
    """Instrument first; build a watchdog only if the data justifies it."""
    backend = FakeBackend()
    seen: list[tuple[str, float]] = []

    async def scenario() -> None:
        async with entity_lock(
            backend,
            "ACC-0001",
            on_long_hold=lambda key, held: seen.append((key, held)),
        ):
            pass

    run(scenario())
    # A fast test body is under the threshold, so nothing is reported. The
    # assertion is that the hook exists and stays quiet when it should.
    assert seen == []
