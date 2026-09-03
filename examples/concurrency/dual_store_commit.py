"""Ordering writes across two stores when you cannot have a transaction.

Sanitized illustrative example — not production source.

The problem (docs/04): a state change must land in an authoritative store, in a
projection owned by an external daemon, and as a network command to hardware.
There is no distributed transaction available and two-phase commit would be a
poor trade at this size.

So the ordering is chosen to make the only reachable inconsistency the one that
is DETECTABLE and IDEMPOTENTLY REPAIRABLE:

    1. acquire the per-entity lock
    2. write the projection      (commits inside its own call)
    3. send the network command   (may fail)
    4. commit the authoritative store  -- LAST, only if 2 and 3 succeeded
    5. audit                      (independent transaction)
    6. enqueue notification

Authoritative-commits-last means the reachable failure is "projection ahead of
truth", which a reconciliation job finds and can repair by re-deriving the
projection. The inverse -- truth ahead of projection -- would mean the ledger
believes someone is connected while the network disagrees, and nothing would
notice, because the ledger is the authority everything else is compared against.

The generalisable rule: when you cannot have atomicity, choose which
inconsistency you get, and choose the one you can see.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

__all__ = [
    "CommandOutcome",
    "StateChangeFailed",
    "apply_state_change",
]


class CommandOutcome(StrEnum):
    """Result of the network command. Deliberately not a bool.

    A boolean would collapse "the device said no" and "the device said nothing"
    into one value, and they need different handling: a rejection is a
    configuration problem that will not fix itself, a timeout may be transient,
    and "no session existed" is frequently success in disguise -- there was
    nothing to disconnect because the subscriber was already offline.
    """

    APPLIED = "applied"
    NO_SESSION = "no_session"
    REJECTED = "rejected"
    TIMEOUT = "timeout"


class StateChangeFailed(RuntimeError):
    """The change did not complete; the authoritative store was not committed."""


class Transaction(Protocol):
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...


class Projection(Protocol):
    async def write_group(self, entity_id: str, group: str) -> None:
        """Write and commit the projection. Commits internally -- see below."""
        ...


class DeviceCommand(Protocol):
    async def force_reauth(self, entity_id: str) -> CommandOutcome:
        """Pure network I/O. No locking, no database, no audit, no queue."""
        ...


class AuditSink(Protocol):
    async def record(self, entity_id: str, event: str, result: str) -> None:
        """Write in an INDEPENDENT transaction, and never raise."""
        ...


@dataclass(frozen=True, slots=True)
class StateChange:
    entity_id: str
    target_group: str
    event: str


async def apply_state_change(
    change: StateChange,
    *,
    tx: Transaction,
    projection: Projection,
    device: DeviceCommand,
    audit: AuditSink,
) -> CommandOutcome:
    """Apply one state change across both stores, in the safe order.

    The caller is expected to already hold the per-entity lock. That is
    deliberate: the lock is acquired ONCE at the outermost boundary, and a
    function that acquires it internally cannot be composed with one that
    already holds it (the mutex is not reentrant).

    Note the asymmetric commit contract, which is surprising enough to be worth
    documenting explicitly rather than leaving to be inferred: the projection
    commits INSIDE `write_group`, while the authoritative transaction commits
    HERE, in the caller. Someone adding a caller and assuming symmetry produces
    a subtly-broken sequence that still passes its tests.
    """
    try:
        await projection.write_group(change.entity_id, change.target_group)
        outcome = await device.force_reauth(change.entity_id)

        if outcome in (CommandOutcome.REJECTED, CommandOutcome.TIMEOUT):
            # Do NOT commit. The projection has moved ahead of the truth, which
            # reconciliation will find. Committing here would produce the
            # invisible inconsistency instead of the visible one.
            await tx.rollback()
            # Audit inside the failure path, in its own transaction, so this row
            # survives the rollback above. If audit joined the main transaction,
            # the system would keep a complete record of successes and no record
            # at all of failures -- exactly backwards.
            await audit.record(change.entity_id, change.event, "failed")
            raise StateChangeFailed(f"device command {outcome} for {change.entity_id}")

        await tx.commit()

    except StateChangeFailed:
        raise
    except Exception as exc:
        await tx.rollback()
        await audit.record(change.entity_id, change.event, "failed")
        raise StateChangeFailed(str(exc)) from exc

    # Success audit goes AFTER the commit, so the trail cannot claim something
    # the database never persisted.
    await audit.record(change.entity_id, change.event, "success")
    return outcome
