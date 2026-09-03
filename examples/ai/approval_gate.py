"""Human-approval queue for AI-proposed actions.

Sanitized illustrative example — not production source. Nothing like this is
deployed; it is the designed shape of a write path, per docs/06.

THE CORE IDEA. The model does not act. It writes an INTENT to a queue, and a
separate executor applies approved intents. That separation is what makes
"human approval" a control rather than a prompt instruction:

    model ──proposes──▶ queue ──human reviews──▶ executor ──▶ system

If the model can actuate directly, then "ask before doing something dangerous"
lives in the prompt, and a prompt is not a control -- particularly not in a
system whose context contains text an attacker can influence.

The controls encoded below, each mapping to a specific failure:

    allowlist, not denylist   the action nobody thought to forbid
    proposal queue            direct actuation from a manipulated context
    human approval            irreversible action on a wrong inference
    rate limit + halt         a loop that disconnects the whole subscriber base
    reversibility             an action you cannot undo
    kill switch               needing a code change to stop it
    permanent audit           not being able to reconstruct what it did
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

__all__ = [
    "ActionSpec",
    "ApprovalQueue",
    "Proposal",
    "ProposalState",
    "QueueHalted",
]


class ProposalState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"


class QueueHalted(RuntimeError):
    """The queue stopped accepting proposals: rate limit tripped or kill switch."""


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """One permitted action.

    ALLOWLIST, not denylist. Only actions described here can ever be proposed.
    The difference is the difference between reasoning about what should be
    possible and trying to enumerate every disaster -- and the second one is
    always one item short.
    """

    name: str
    required_args: frozenset[str]
    # Whether the action's prior state can be captured and restored. An action
    # that is not reversible cannot be auto-approved under any circumstances,
    # regardless of how confident the proposal is.
    reversible: bool
    # Even reversible actions may need a human. `False` here means an operator
    # must approve every instance.
    auto_approvable: bool = False


@dataclass(slots=True)
class Proposal:
    proposal_id: int
    action: str
    arguments: Mapping[str, str]
    rationale: str
    proposed_at: datetime
    state: ProposalState = ProposalState.PENDING
    prior_state: Mapping[str, str] | None = None
    decided_by: str | None = None


@dataclass(slots=True)
class ApprovalQueue:
    """Accepts proposals; never executes them.

    Execution belongs to a separate worker reading APPROVED rows. Keeping the
    executor out of this class is not tidiness -- it means the component the
    model talks to has no code path that changes anything.
    """

    allowlist: Mapping[str, ActionSpec]
    max_pending: int = 20
    max_per_hour: int = 30
    killed: bool = False

    _proposals: list[Proposal] = field(default_factory=list)
    _next_id: int = 1

    def kill(self, *, reason: str) -> None:
        """Stop accepting proposals immediately.

        One operation, no deploy. In production this also revokes the agent's
        credential, so the kill switch does not depend on this process being
        healthy enough to honour a boolean.
        """
        self.killed = True
        self._audit("queue.killed", reason)

    def propose(
        self,
        action: str,
        arguments: Mapping[str, str],
        *,
        rationale: str,
        now: datetime | None = None,
    ) -> Proposal:
        """Record an intent. Raises rather than silently dropping.

        A refused proposal must be an error the caller sees. Silently
        discarding it would leave the model believing it had acted -- the
        "looked like success" failure shape (docs/07) reappearing inside the
        agent layer.
        """
        moment = now or datetime.now(UTC)

        if self.killed:
            raise QueueHalted("kill switch engaged; no proposals accepted")

        spec = self.allowlist.get(action)
        if spec is None:
            raise QueueHalted(f"action {action!r} is not on the allowlist; refused")

        missing = spec.required_args - set(arguments)
        if missing:
            raise QueueHalted(
                f"action {action!r} missing required arguments: {sorted(missing)}"
            )

        if not rationale.strip():
            # A proposal with no stated reason cannot be reviewed, and an
            # unreviewable proposal is an auto-approval with extra steps.
            raise QueueHalted("a proposal must carry a rationale for the reviewer")

        pending = sum(p.state is ProposalState.PENDING for p in self._proposals)
        if pending >= self.max_pending:
            self.kill(reason=f"pending backlog reached {pending}")
            raise QueueHalted(
                f"pending backlog {pending} >= {self.max_pending}; queue halted. "
                "A backlog this size means the agent is proposing faster than a "
                "human can review, which is indistinguishable from a loop."
            )

        recent = sum(
            1 for p in self._proposals if (moment - p.proposed_at).total_seconds() < 3600
        )
        if recent >= self.max_per_hour:
            self.kill(reason=f"rate limit: {recent} proposals in the last hour")
            raise QueueHalted(f"rate limit {self.max_per_hour}/h exceeded; halted")

        proposal = Proposal(
            proposal_id=self._next_id,
            action=action,
            arguments=dict(arguments),
            rationale=rationale,
            proposed_at=moment,
        )
        self._next_id += 1
        self._proposals.append(proposal)
        self._audit("proposal.created", f"{action} #{proposal.proposal_id}")
        return proposal

    def decide(self, proposal_id: int, *, approve: bool, operator: str) -> Proposal:
        """Record a human decision. Approval never executes anything here."""
        proposal = self._require(proposal_id)
        if proposal.state is not ProposalState.PENDING:
            raise QueueHalted(f"proposal #{proposal_id} is {proposal.state}, not pending")

        spec = self.allowlist[proposal.action]
        if approve and not spec.reversible:
            # A reviewer may still approve an irreversible action, but it must
            # be an explicit, separately-recorded act -- not a click that looks
            # identical to approving a reversible one.
            self._audit(
                "proposal.irreversible_approved",
                f"#{proposal_id} by {operator}",
            )

        proposal.state = ProposalState.APPROVED if approve else ProposalState.REJECTED
        proposal.decided_by = operator
        self._audit(
            "proposal.decided",
            f"#{proposal_id} {proposal.state} by {operator}",
        )
        return proposal

    def approved(self) -> Sequence[Proposal]:
        """What the executor should pick up."""
        return [p for p in self._proposals if p.state is ProposalState.APPROVED]

    def pending(self) -> Sequence[Proposal]:
        return [p for p in self._proposals if p.state is ProposalState.PENDING]

    # Audit is append-only and, in production, permanently retained in its own
    # table. Reconstructing what an agent did months later is the whole reason
    # the table exists, so it is never pruned with the general log retention.
    audit_log: list[tuple[datetime, str, str]] = field(default_factory=list)

    def _audit(self, event: str, detail: str) -> None:
        self.audit_log.append((datetime.now(UTC), event, detail))

    def _require(self, proposal_id: int) -> Proposal:
        for p in self._proposals:
            if p.proposal_id == proposal_id:
                return p
        raise QueueHalted(f"no proposal #{proposal_id}")
