"""Ordering: the authoritative store commits LAST, so the visible failure wins.

Sanitized illustrative example — not production source.
"""

from __future__ import annotations

import asyncio

import pytest

from examples.concurrency.dual_store_commit import (
    CommandOutcome,
    StateChange,
    StateChangeFailed,
    apply_state_change,
)


class FakeTx:
    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


class FakeProjection:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.writes: list[tuple[str, str]] = []

    async def write_group(self, entity_id: str, group: str) -> None:
        if self.fail:
            raise ConnectionError("projection store unreachable")
        self.writes.append((entity_id, group))


class FakeDevice:
    def __init__(self, outcome: CommandOutcome) -> None:
        self.outcome = outcome
        self.calls = 0

    async def force_reauth(self, entity_id: str) -> CommandOutcome:
        self.calls += 1
        return self.outcome


class FakeAudit:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    async def record(self, entity_id: str, event: str, result: str) -> None:
        self.rows.append((entity_id, event, result))


CHANGE = StateChange(entity_id="ACC-0001", target_group="active", event="activated")


def test_happy_path_commits_last_and_audits_after() -> None:
    tx, proj, dev, audit = (
        FakeTx(),
        FakeProjection(),
        FakeDevice(CommandOutcome.APPLIED),
        FakeAudit(),
    )

    outcome = asyncio.run(
        apply_state_change(CHANGE, tx=tx, projection=proj, device=dev, audit=audit)
    )

    assert outcome is CommandOutcome.APPLIED
    assert proj.writes == [("ACC-0001", "active")]
    assert dev.calls == 1
    assert tx.committed and not tx.rolled_back
    assert audit.rows == [("ACC-0001", "activated", "success")]


def test_no_session_is_success() -> None:
    """Nothing to disconnect means the subscriber was already offline."""
    tx, proj, dev, audit = (
        FakeTx(),
        FakeProjection(),
        FakeDevice(CommandOutcome.NO_SESSION),
        FakeAudit(),
    )

    outcome = asyncio.run(
        apply_state_change(CHANGE, tx=tx, projection=proj, device=dev, audit=audit)
    )
    assert outcome is CommandOutcome.NO_SESSION
    assert tx.committed


@pytest.mark.parametrize("bad", [CommandOutcome.REJECTED, CommandOutcome.TIMEOUT])
def test_device_failure_does_not_commit_the_ledger(bad: CommandOutcome) -> None:
    """The whole point of the ordering.

    The projection has moved ahead of the truth, which reconciliation can see
    and repair idempotently. Committing here would produce the INVISIBLE
    inconsistency instead -- the ledger claiming a state the network never
    reached, with nothing to detect it.
    """
    tx, proj, dev, audit = FakeTx(), FakeProjection(), FakeDevice(bad), FakeAudit()

    with pytest.raises(StateChangeFailed):
        asyncio.run(apply_state_change(CHANGE, tx=tx, projection=proj, device=dev, audit=audit))

    assert proj.writes  # projection ahead: detectable, repairable
    assert not tx.committed
    assert tx.rolled_back


def test_failure_audit_survives_the_rollback() -> None:
    """Audit is written in the exception path, in its own transaction.

    If audit rows joined the main transaction, every failure would roll back
    its own record and the system would keep a complete history of successes
    and no record at all of failures.
    """
    tx, proj, dev, audit = (
        FakeTx(),
        FakeProjection(),
        FakeDevice(CommandOutcome.REJECTED),
        FakeAudit(),
    )

    with pytest.raises(StateChangeFailed):
        asyncio.run(apply_state_change(CHANGE, tx=tx, projection=proj, device=dev, audit=audit))

    assert audit.rows == [("ACC-0001", "activated", "failed")]


def test_projection_failure_never_reaches_the_device() -> None:
    tx, proj, dev, audit = (
        FakeTx(),
        FakeProjection(fail=True),
        FakeDevice(CommandOutcome.APPLIED),
        FakeAudit(),
    )

    with pytest.raises(StateChangeFailed):
        asyncio.run(apply_state_change(CHANGE, tx=tx, projection=proj, device=dev, audit=audit))

    assert dev.calls == 0
    assert not tx.committed
    assert audit.rows == [("ACC-0001", "activated", "failed")]
