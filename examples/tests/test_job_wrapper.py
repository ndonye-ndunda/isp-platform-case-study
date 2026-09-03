"""Crash semantics: a frozen 'running' row is a signal, not a bug.

Sanitized illustrative example — not production source.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from examples.workers.job_wrapper import (
    HeartbeatStatus,
    JobRegistry,
    RunHandle,
    run_with_heartbeat,
)


class FakeStore:
    def __init__(self) -> None:
        self.opened: list[str] = []
        self.closed: list[tuple[str, HeartbeatStatus, int]] = []

    async def open_run(self, job_name: str, started_at: datetime) -> None:
        self.opened.append(job_name)

    async def close_run(
        self, job_name: str, status: HeartbeatStatus, handle: RunHandle
    ) -> None:
        self.closed.append((job_name, status, handle.processed))


class FakeLogger:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.context: dict[str, str] = {}

    def bind(self, **context: str) -> FakeLogger:
        child = FakeLogger()
        child.events = self.events
        child.context = {**self.context, **context}
        return child

    def info(self, event: str, **fields: object) -> None:
        self.events.append(event)

    def exception(self, event: str, **fields: object) -> None:
        self.events.append(event)


def test_successful_run_completes_and_logs_context() -> None:
    store, logger = FakeStore(), FakeLogger()

    async def body(handle: RunHandle) -> None:
        handle.processed = 7

    status = asyncio.run(run_with_heartbeat("suspension", body, store=store, logger=logger))

    assert status is HeartbeatStatus.COMPLETED
    assert store.opened == ["suspension"]
    assert store.closed == [("suspension", HeartbeatStatus.COMPLETED, 7)]
    assert logger.events == ["job.start", "job.completed"]


def test_raised_exception_is_recorded_failed_not_propagated() -> None:
    """FAILED is the caught-exception signal, distinct from a frozen row.

    The exception is contained so the scheduler does not decide to stop
    scheduling this job. Contained is not the same as silent: it is recorded
    and the alerting layer decides whether a human should care.
    """
    store, logger = FakeStore(), FakeLogger()

    async def body(handle: RunHandle) -> None:
        handle.processed = 3
        raise RuntimeError("downstream unavailable")

    status = asyncio.run(run_with_heartbeat("reconciliation", body, store=store, logger=logger))

    assert status is HeartbeatStatus.FAILED
    assert store.closed == [("reconciliation", HeartbeatStatus.FAILED, 3)]
    assert "job.failed" in logger.events


def test_a_killed_process_leaves_the_run_open() -> None:
    """Simulates a hard crash: open_run happened, close_run never did.

    This is what a killed worker looks like in the store. The row stays at
    'running' with no completion, which is the ONLY evidence a SIGKILL
    produces -- and therefore the alert signal. A startup sweep that rewrote
    these to 'failed' would erase the distinction between a code defect and a
    host resource problem.
    """
    store = FakeStore()
    asyncio.run(store.open_run("metrics", datetime.now(UTC)))

    assert store.opened == ["metrics"]
    assert store.closed == []  # the frozen-row signature


def test_registry_exposes_a_roster() -> None:
    """The roster is the join key that makes a never-run worker visible."""
    reg = JobRegistry(timezone_name="Africa/Nairobi")
    reg.register("suspension", "0 2 * * *")
    reg.register("reconciliation", "*/15 * * * *")
    reg.register("metrics", "0 4 * * *")

    assert len(reg) == 3
    assert reg.roster == ("metrics", "reconciliation", "suspension")


def test_registry_rejects_duplicate_registration() -> None:
    reg = JobRegistry(timezone_name="Africa/Nairobi")
    reg.register("suspension", "0 2 * * *")
    try:
        reg.register("suspension", "0 3 * * *")
    except ValueError as exc:
        assert "already registered" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("duplicate registration should raise")
