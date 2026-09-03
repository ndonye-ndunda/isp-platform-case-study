"""Guardrails: capability is absent, not forbidden.

Sanitized illustrative example — not production source.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import pytest

from examples.ai.approval_gate import (
    ActionSpec,
    ApprovalQueue,
    ProposalState,
    QueueHalted,
)
from examples.ai.readonly_tool_gateway import (
    Tool,
    ToolAccess,
    ToolGateway,
    ToolRejected,
)


def _reader(name: str = "query_logs", desc: str = "Run a read-only log query") -> Tool:
    return Tool(
        name=name,
        access=ToolAccess.READ,
        description=desc,
        parameters={"query": "string", "start": "string", "end": "string"},
        handler=lambda args: f"rows for {args.get('query', '')}",
    )


# --- tool gateway ----------------------------------------------------------


def test_write_tools_are_refused_at_registration() -> None:
    """Refused when registered, not when called.

    A write tool that exists but is blocked at call time has still been
    advertised: it appears in the schema, costs context on every request, and
    invites the model to plan around a capability it will never get.
    """
    gw = ToolGateway()
    writer = Tool(
        name="delete_series",
        access=ToolAccess.WRITE,
        description="Delete a metric series",
        parameters={"series": "string"},
        handler=lambda args: "gone",
    )
    with pytest.raises(ToolRejected, match="read-only by construction"):
        gw.register(writer)
    assert gw.advertised() == []


def test_unknown_tool_does_not_leak_the_valid_set() -> None:
    gw = ToolGateway()
    gw.register(_reader())
    with pytest.raises(ToolRejected) as exc:
        gw.call("query_logs_v2", {"query": "x"})
    assert "query_logs" not in str(exc.value).replace("query_logs_v2", "")


def test_schema_budget_is_enforced() -> None:
    """Tool schemas are paid on EVERY request.

    In the measured production case, 15 tools were ~7,100 of a 9,216-token
    prompt and prompt processing was the entire latency bottleneck. So the
    advertised set has a budget, and exceeding it must be a deliberate act.
    """
    gw = ToolGateway(schema_token_budget=40)
    gw.register(_reader(desc="short"))
    with pytest.raises(ToolRejected, match="over the 40 budget"):
        gw.register(_reader(name="query_metrics", desc="x" * 400))


def test_schema_cost_is_reported() -> None:
    gw = ToolGateway()
    assert gw.schema_cost() == 0
    gw.register(_reader())
    assert gw.schema_cost() > 0


def test_mutating_keywords_in_arguments_are_refused() -> None:
    """A backstop, explicitly NOT the primary control.

    The primary controls are the read-only credential and the absent write
    tools. A denylist of dangerous strings is the wrong primary defence because
    it requires enumerating every disaster -- this exists only to fail loudly
    if a write-capable path is ever introduced by mistake.
    """
    gw = ToolGateway()
    gw.register(_reader())
    with pytest.raises(ToolRejected, match="mutating keyword"):
        gw.call("query_logs", {"query": "DROP TABLE payments"})


def test_every_call_is_recorded_for_audit() -> None:
    """'What did the assistant actually look at?' must have an answer."""
    gw = ToolGateway()
    gw.register(_reader())
    gw.call("query_logs", {"query": "up == 0"})
    gw.call("query_logs", {"query": "rate(errors[5m])"})
    assert [name for name, _ in gw.calls] == ["query_logs", "query_logs"]


def test_duplicate_registration_is_refused() -> None:
    gw = ToolGateway()
    gw.register(_reader())
    with pytest.raises(ToolRejected, match="already registered"):
        gw.register(_reader())


# --- approval queue --------------------------------------------------------

ALLOWLIST: Mapping[str, ActionSpec] = {
    "restart_exporter": ActionSpec(
        name="restart_exporter",
        required_args=frozenset({"site"}),
        reversible=True,
    ),
    "disable_account": ActionSpec(
        name="disable_account",
        required_args=frozenset({"account_ref"}),
        reversible=False,
    ),
}


def _queue(**kw: object) -> ApprovalQueue:
    return ApprovalQueue(allowlist=ALLOWLIST, **kw)  # type: ignore[arg-type]


def test_action_not_on_the_allowlist_is_refused() -> None:
    """Allowlist, not denylist: the action nobody thought to forbid."""
    q = _queue()
    with pytest.raises(QueueHalted, match="not on the allowlist"):
        q.propose("reboot_router", {"site": "site-alpha"}, rationale="seems stuck")


def test_missing_required_arguments_are_refused() -> None:
    q = _queue()
    with pytest.raises(QueueHalted, match="missing required arguments"):
        q.propose("restart_exporter", {}, rationale="no data for 20m")


def test_a_proposal_needs_a_rationale() -> None:
    """An unreviewable proposal is an auto-approval with extra steps."""
    q = _queue()
    with pytest.raises(QueueHalted, match="rationale"):
        q.propose("restart_exporter", {"site": "site-alpha"}, rationale="   ")


def test_approval_does_not_execute() -> None:
    """The queue records intent; a separate executor applies it.

    This separation is what makes human approval a control rather than a prompt
    instruction. If the model could actuate directly, 'ask first' would live in
    the prompt -- and prompts are not controls.
    """
    q = _queue()
    p = q.propose("restart_exporter", {"site": "site-alpha"}, rationale="no data for 20m")
    assert p.state is ProposalState.PENDING
    assert q.approved() == []

    q.decide(p.proposal_id, approve=True, operator="operator-1")
    assert [x.proposal_id for x in q.approved()] == [p.proposal_id]
    assert p.decided_by == "operator-1"


def test_rejection_is_recorded() -> None:
    q = _queue()
    p = q.propose("restart_exporter", {"site": "site-alpha"}, rationale="hunch")
    q.decide(p.proposal_id, approve=False, operator="operator-1")
    assert p.state is ProposalState.REJECTED
    assert q.approved() == []


def test_deciding_twice_is_refused() -> None:
    q = _queue()
    p = q.propose("restart_exporter", {"site": "site-alpha"}, rationale="stuck")
    q.decide(p.proposal_id, approve=True, operator="operator-1")
    with pytest.raises(QueueHalted, match="not pending"):
        q.decide(p.proposal_id, approve=True, operator="operator-2")


def test_irreversible_approval_is_separately_audited() -> None:
    """Approving something you cannot undo must not look like a normal click."""
    q = _queue()
    p = q.propose("disable_account", {"account_ref": "ACC-0001"}, rationale="fraud signal")
    q.decide(p.proposal_id, approve=True, operator="operator-1")
    events = [event for _, event, _ in q.audit_log]
    assert "proposal.irreversible_approved" in events


def test_backlog_halts_the_queue() -> None:
    """A backlog larger than a human can review is indistinguishable from a loop."""
    q = _queue(max_pending=3)
    for i in range(3):
        q.propose("restart_exporter", {"site": f"s{i}"}, rationale="no data")
    with pytest.raises(QueueHalted, match="queue halted"):
        q.propose("restart_exporter", {"site": "s4"}, rationale="no data")
    assert q.killed


def test_rate_limit_halts_the_queue() -> None:
    q = _queue(max_pending=100, max_per_hour=2)
    now = datetime.now(UTC)
    q.propose("restart_exporter", {"site": "a"}, rationale="x", now=now)
    q.propose("restart_exporter", {"site": "b"}, rationale="x", now=now)
    with pytest.raises(QueueHalted, match="rate limit"):
        q.propose("restart_exporter", {"site": "c"}, rationale="x", now=now)


def test_rate_limit_window_is_rolling() -> None:
    q = _queue(max_pending=100, max_per_hour=2)
    old = datetime.now(UTC) - timedelta(hours=2)
    q.propose("restart_exporter", {"site": "a"}, rationale="x", now=old)
    q.propose("restart_exporter", {"site": "b"}, rationale="x", now=old)
    # Two hours later the window has cleared, so this must be accepted.
    q.propose("restart_exporter", {"site": "c"}, rationale="x")
    assert not q.killed


def test_kill_switch_stops_everything() -> None:
    """One operation, no deploy."""
    q = _queue()
    q.kill(reason="operator intervention")
    with pytest.raises(QueueHalted, match="kill switch"):
        q.propose("restart_exporter", {"site": "a"}, rationale="x")


def test_refused_proposals_raise_rather_than_dropping_silently() -> None:
    """A silently discarded proposal would leave the model believing it acted.

    That is the 'looked like success' failure shape reappearing inside the
    agent layer -- see docs/07.
    """
    q = _queue()
    with pytest.raises(QueueHalted):
        q.propose("not_allowed", {}, rationale="x")
    assert q.pending() == []
