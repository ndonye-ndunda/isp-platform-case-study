"""The roster series is what makes a never-succeeded worker detectable.

Sanitized illustrative example — not production source.
"""

from __future__ import annotations

from examples.observability.liveness_metrics import (
    WorkerObservation,
    render_exposition,
)

ROSTER = ("churn", "metrics", "reconciliation", "suspension")


def test_roster_is_emitted_for_every_registered_worker() -> None:
    out = render_exposition(ROSTER, {})
    for job in ROSTER:
        assert f'isp_worker_registered{{job="{job}"}} 1' in out


def test_never_succeeded_worker_emits_roster_but_no_timestamp() -> None:
    """The whole design goal.

    'suspension' has never succeeded, so it has NO last-success series. The
    `registered unless last_success` rule therefore matches it and can fire.
    Emitting 0 instead would make it look like a success at the Unix epoch and
    would trip the STALENESS rule -- the wrong alert for the wrong reason.
    """
    obs = {
        "suspension": WorkerObservation(
            job="suspension", last_success=None, last_failure=None, frozen_runs=0
        )
    }
    out = render_exposition(ROSTER, obs)

    assert 'isp_worker_registered{job="suspension"} 1' in out
    assert 'isp_worker_last_success_timestamp_seconds{job="suspension"}' not in out


def test_successful_worker_emits_a_timestamp() -> None:
    obs = {
        "metrics": WorkerObservation(
            job="metrics",
            last_success=1_770_000_000.0,
            last_failure=None,
            frozen_runs=0,
        )
    }
    out = render_exposition(ROSTER, obs)
    assert 'isp_worker_last_success_timestamp_seconds{job="metrics"} 1770000000' in out


def test_frozen_runs_are_always_emitted_including_zero() -> None:
    """Zero is a real value here and must be present.

    An absent frozen-runs series would make the rule unevaluable -- the
    absent-series problem. A 0 is an assertion of health; no series is an
    absence of information, and the two must not look alike.
    """
    obs = {
        "churn": WorkerObservation(
            job="churn", last_success=1.0, last_failure=None, frozen_runs=0
        ),
        "metrics": WorkerObservation(
            job="metrics", last_success=1.0, last_failure=None, frozen_runs=2
        ),
    }
    out = render_exposition(ROSTER, obs)
    assert 'isp_worker_frozen_runs{job="churn"} 0' in out
    assert 'isp_worker_frozen_runs{job="metrics"} 2' in out


def test_exposition_has_help_and_type_for_every_family() -> None:
    out = render_exposition(ROSTER, {})
    for family in (
        "isp_worker_registered",
        "isp_worker_last_success_timestamp_seconds",
        "isp_worker_last_failure_timestamp_seconds",
        "isp_worker_frozen_runs",
    ):
        assert f"# HELP {family} " in out
        assert f"# TYPE {family} gauge" in out


def test_output_ends_with_a_newline() -> None:
    """Prometheus text format requires it; a missing trailing newline is a
    silent parse failure at the scrape endpoint."""
    assert render_exposition(ROSTER, {}).endswith("\n")
