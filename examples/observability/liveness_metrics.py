"""Worker liveness exposition, designed so a NEVER-RUN worker is still visible.

Sanitized illustrative example — not production source.

The problem this solves is the absent-series problem (docs/05). A staleness
alert compares `now()` against a last-success timestamp:

    time() - isp_worker_last_success_timestamp_seconds{job="x"} > 3600

For a worker that has run and then stopped, that works. For a worker that has
NEVER run -- a typo in its registration, a job removed by an unrelated refactor,
a deploy that dropped it -- there is no series, the expression matches nothing,
and "matches nothing" means "no alert". The worker most likely to be broken is
the one this rule cannot see.

The fix is to publish a ROSTER: a series enumerating every worker that *should*
exist, emitted from the registry rather than from run history. Joining expected
against observed turns an absence into a computable difference, and a
difference can fire.

    isp_worker_registered{job="x"} 1        <- always present, from the roster
    isp_worker_last_success_...{job="x"}    <- present only after one success

    # the never-succeeded rule then becomes expressible:
    isp_worker_registered
      unless isp_worker_last_success_timestamp_seconds
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

__all__ = ["WorkerObservation", "render_exposition"]


@dataclass(frozen=True, slots=True)
class WorkerObservation:
    """What the heartbeat store knows about one worker.

    `last_success` and `last_failure` are Optional on purpose. `None` means
    "never happened", which is different from zero -- and rendering `0` for a
    never-succeeded worker would make it look like a success at the Unix epoch,
    i.e. maximally stale, i.e. it would fire the *staleness* rule instead of the
    *never-succeeded* rule. Two different problems deserve two different alerts.
    """

    job: str
    last_success: float | None
    last_failure: float | None
    frozen_runs: int


def render_exposition(
    roster: Sequence[str],
    observations: Mapping[str, WorkerObservation],
) -> str:
    """Render Prometheus text exposition for the worker liveness contract.

    `roster` is the source of truth for which workers should exist and comes
    from the scheduler's registry -- NOT from the set of rows in the heartbeat
    table. That distinction is the entire point: deriving the roster from
    observed runs means a worker that never ran is absent from its own roster,
    and the join can never detect it.
    """
    lines: list[str] = []

    lines.append("# HELP isp_worker_registered Workers the scheduler expects to exist.")
    lines.append("# TYPE isp_worker_registered gauge")
    for job in sorted(roster):
        lines.append(f'isp_worker_registered{{job="{job}"}} 1')

    lines.append("")
    lines.append(
        "# HELP isp_worker_last_success_timestamp_seconds "
        "Unix time of the last successful run."
    )
    lines.append("# TYPE isp_worker_last_success_timestamp_seconds gauge")
    for job, obs in _sorted(observations):
        if obs.last_success is not None:
            lines.append(
                "isp_worker_last_success_timestamp_seconds"
                f'{{job="{job}"}} {obs.last_success:.0f}'
            )

    lines.append("")
    lines.append(
        "# HELP isp_worker_last_failure_timestamp_seconds "
        "Unix time of the last run that raised."
    )
    lines.append("# TYPE isp_worker_last_failure_timestamp_seconds gauge")
    for job, obs in _sorted(observations):
        if obs.last_failure is not None:
            lines.append(
                "isp_worker_last_failure_timestamp_seconds"
                f'{{job="{job}"}} {obs.last_failure:.0f}'
            )

    lines.append("")
    lines.append(
        "# HELP isp_worker_frozen_runs Runs left at 'running' with no "
        "completion -- the signature of a killed process, not a caught exception."
    )
    lines.append("# TYPE isp_worker_frozen_runs gauge")
    for job, obs in _sorted(observations):
        lines.append(f'isp_worker_frozen_runs{{job="{job}"}} {obs.frozen_runs}')

    return "\n".join(lines) + "\n"


def _sorted(
    observations: Mapping[str, WorkerObservation],
) -> Iterable[tuple[str, WorkerObservation]]:
    return sorted(observations.items())
