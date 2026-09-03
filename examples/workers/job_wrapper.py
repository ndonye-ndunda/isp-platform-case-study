"""One registration wrapper for every scheduled job.

Sanitized illustrative example — not production source.

The wrapper exists so that three things cannot be forgotten (docs/04):

  * an EXPLICIT timezone -- never the scheduler's default. A job with the wrong
    timezone runs perfectly, at the wrong hour, forever, and the only symptom is
    that a daily sweep evaluates a day that has not finished yet.
  * heartbeat recording, opened before the body and closed after
  * bound log context, so every line the job emits carries its identity

Registration is an explicit call from the process entrypoint, never an import
side-effect. Import-time registration makes the set of running jobs depend on
which modules happened to be imported -- a property nobody can read off the
code, and one that changes when someone adds an unrelated import.

The crash semantics below are the part worth reading closely.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

__all__ = [
    "HeartbeatStatus",
    "JobRegistry",
    "RunHandle",
    "run_with_heartbeat",
]


class HeartbeatStatus(StrEnum):
    """Terminal states of a job run -- and one that is deliberately not terminal.

    RUNNING is written at start and is only ever *overwritten*. A row left at
    RUNNING with no completion timestamp is the signature of a hard crash
    (OOM, SIGKILL, host reboot): the process died without the chance to write
    anything else.

    That frozen row IS the crash alert signal. It is not a bug to be tidied up.
    The tidy-looking change -- a startup sweep that marks stale RUNNING rows as
    FAILED -- would destroy the distinction between "crashed" and "raised",
    which are different problems with different causes:

        FAILED           an exception was caught and recorded  -> code defect
        RUNNING (stale)  the process was killed mid-run        -> resource/host

    Detection for the two is different too. FAILED is a value you can count.
    Stale RUNNING is an absence of progress, so it needs a staleness rule over
    a last-success timestamp -- and a worker that has NEVER succeeded has no
    such timestamp and therefore no series, which is why the metrics layer also
    publishes a registry of workers that *should* exist. See
    examples/observability/liveness_metrics.py.
    """

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(slots=True)
class RunHandle:
    """Passed to the job body. Jobs never touch the heartbeat store directly.

    Jobs record *counters* through this handle and nothing else. Keeping the
    heartbeat mechanism out of job bodies means the mechanism can change --
    extra fields, a different store, richer metrics -- without editing nine
    jobs, and it means no job can accidentally mark itself completed early.
    """

    job_name: str
    started_at: datetime
    processed: int = 0
    skipped: int = 0
    failures: int = 0
    notes: dict[str, str] = field(default_factory=dict)


class HeartbeatStore(Protocol):
    async def open_run(self, job_name: str, started_at: datetime) -> None: ...
    async def close_run(
        self, job_name: str, status: HeartbeatStatus, handle: RunHandle
    ) -> None: ...


class Logger(Protocol):
    def bind(self, **context: str) -> Logger: ...
    def info(self, event: str, **fields: object) -> None: ...
    def exception(self, event: str, **fields: object) -> None: ...


JobBody = Callable[[RunHandle], Awaitable[None]]


async def run_with_heartbeat(
    job_name: str,
    body: JobBody,
    *,
    store: HeartbeatStore,
    logger: Logger,
) -> HeartbeatStatus:
    """Run one job invocation with heartbeat and log context attached.

    Returns the terminal status rather than raising. A scheduled job that
    propagates an exception into the scheduler risks the scheduler's own
    error handling deciding to stop scheduling it -- so failures are recorded
    and contained here, and the *alerting* layer decides whether a human
    should care. Containment and silence are different things: this records
    loudly and returns quietly.
    """
    started_at = datetime.now(UTC)
    log = logger.bind(job=job_name, run_started_at=started_at.isoformat())
    handle = RunHandle(job_name=job_name, started_at=started_at)

    await store.open_run(job_name, started_at)
    log.info("job.start")

    try:
        await body(handle)
    except Exception:
        # Caught exception -> FAILED, which is a *different* signal from a
        # frozen RUNNING row. See HeartbeatStatus.
        log.exception("job.failed", processed=handle.processed)
        await store.close_run(job_name, HeartbeatStatus.FAILED, handle)
        return HeartbeatStatus.FAILED

    log.info(
        "job.completed",
        processed=handle.processed,
        skipped=handle.skipped,
        failures=handle.failures,
    )
    await store.close_run(job_name, HeartbeatStatus.COMPLETED, handle)
    return HeartbeatStatus.COMPLETED


@dataclass(slots=True)
class JobRegistry:
    """Collects jobs so the expected set is an inspectable value.

    The roster matters as much as the scheduling. "Which jobs should be
    running?" must be answerable from data, because it is the join key that
    makes a never-run worker visible to alerting -- you cannot alert on the
    absence of something you cannot enumerate.
    """

    timezone_name: str
    _jobs: dict[str, str] = field(default_factory=dict)

    def register(self, job_name: str, cron: str) -> None:
        """Register a job. The timezone comes from the registry, not the caller.

        Callers cannot pass a timezone, which is the point: the only way to get
        a trigger is through here, so the only timezone available is the
        explicit one. In production, constructing a trigger anywhere else is a
        build failure -- an escape hatch that compiles defeats the guard.
        """
        if job_name in self._jobs:
            raise ValueError(f"job {job_name!r} already registered")
        self._jobs[job_name] = cron

    @property
    def roster(self) -> tuple[str, ...]:
        return tuple(sorted(self._jobs))

    def __len__(self) -> int:
        return len(self._jobs)
