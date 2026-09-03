"""The webhook's three properties: idempotent, ack-fast, honest status codes.

Sanitized illustrative example — not production source.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from examples.api.idempotent_webhook import CreditResult, router
from examples.domain.money import format_minor_units
from examples.synthetic_data.generate import CANONICAL_CHARGE_MINOR

# Derived, never hardcoded: a second literal copy of an amount is a place a
# real figure can be pasted into without the fixture guard noticing.
AMOUNT_MINOR = CANONICAL_CHARGE_MINOR
AMOUNT_STR = format_minor_units(AMOUNT_MINOR).replace(",", "")

TOKEN = "test-token-not-a-real-secret"


@dataclass
class FakeLedger:
    seen: set[str] = field(default_factory=set)
    known_accounts: set[str] = field(default_factory=lambda: {"ACC-0001"})
    credits: list[tuple[str, int]] = field(default_factory=list)

    async def credit(
        self, *, provider_txn_id: str, account_ref: str, amount_minor: int
    ) -> CreditResult:
        # Stands in for a UNIQUE index on provider_txn_id. The real defence is
        # in the database, not here: an application-level check-then-insert
        # races against itself under concurrent delivery.
        if provider_txn_id in self.seen:
            return CreditResult(accepted=True, duplicate=True)
        self.seen.add(provider_txn_id)
        if account_ref not in self.known_accounts:
            return CreditResult(accepted=False, duplicate=False)
        self.credits.append((account_ref, amount_minor))
        return CreditResult(accepted=True, duplicate=False)


@dataclass
class FakeQueue:
    enqueued: list[tuple[str, str]] = field(default_factory=list)

    async def enqueue(self, account_ref: str, provider_txn_id: str) -> None:
        self.enqueued.append((account_ref, provider_txn_id))


@dataclass
class FakeAlerter:
    alerts: list[tuple[str, str]] = field(default_factory=list)

    async def critical(self, event: str, detail: str) -> None:
        self.alerts.append((event, detail))


@dataclass
class FakeDeps:
    ledger: FakeLedger = field(default_factory=FakeLedger)
    queue: FakeQueue = field(default_factory=FakeQueue)
    alerter: FakeAlerter = field(default_factory=FakeAlerter)
    webhook_token: str = TOKEN


@pytest.fixture
def deps() -> FakeDeps:
    return FakeDeps()


@pytest.fixture
def client(deps: FakeDeps) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.deps = deps
    return TestClient(app)


def _payload(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "provider_txn_id": "TEST-TXN-000001",
        "account_ref": "ACC-0001",
        "amount": AMOUNT_STR,
    }
    base.update(kw)
    return base


def test_accepts_a_valid_payment_and_queues_activation(
    client: TestClient, deps: FakeDeps
) -> None:
    resp = client.post(f"/webhook/payments/{TOKEN}", json=_payload())

    assert resp.status_code == 200
    assert resp.json() == {"status": "accepted"}
    assert deps.ledger.credits == [("ACC-0001", AMOUNT_MINOR)]
    # Ack-fast: activation is QUEUED, not performed inline.
    assert deps.queue.enqueued == [("ACC-0001", "TEST-TXN-000001")]


def test_redelivery_is_idempotent_and_does_not_requeue(
    client: TestClient, deps: FakeDeps
) -> None:
    """Providers retry. A retry must be indistinguishable from the original."""
    first = client.post(f"/webhook/payments/{TOKEN}", json=_payload())
    second = client.post(f"/webhook/payments/{TOKEN}", json=_payload())

    assert first.status_code == second.status_code == 200
    assert second.json() == {"status": "duplicate"}
    assert len(deps.ledger.credits) == 1
    assert len(deps.queue.enqueued) == 1  # NOT activated twice


def test_unparseable_amount_returns_4xx_and_alerts(client: TestClient, deps: FakeDeps) -> None:
    """The most expensive available mistake would be returning 200 here.

    Money has arrived. If the response says success and the only trace is a log
    line, the payment is received and never credited.
    """
    resp = client.post(f"/webhook/payments/{TOKEN}", json=_payload(amount="2,500.00"))

    assert resp.status_code == 422
    assert deps.ledger.credits == []
    assert [e for e, _ in deps.alerter.alerts] == ["payment.amount_unparseable"]


def test_unmatched_account_is_recorded_not_discarded(
    client: TestClient, deps: FakeDeps
) -> None:
    """The money is real, so this is never a silent drop."""
    resp = client.post(f"/webhook/payments/{TOKEN}", json=_payload(account_ref="ACC-9999"))

    assert resp.status_code == 200
    assert resp.json() == {"status": "unmatched"}
    assert deps.queue.enqueued == []
    assert [e for e, _ in deps.alerter.alerts] == ["payment.unmatched"]


def test_wrong_token_is_indistinguishable_from_a_missing_route(
    client: TestClient, deps: FakeDeps
) -> None:
    resp = client.post("/webhook/payments/wrong-token", json=_payload())
    assert resp.status_code == 404
    assert deps.ledger.credits == []


def test_non_ascii_token_does_not_raise_a_500(client: TestClient, deps: FakeDeps) -> None:
    """An auth check must reject unexpected input, not crash on it.

    Comparing decoded strings can raise inside the comparison for input outside
    the expected character set -- a type bug that presents as a security bug,
    because the endpoint returns 500 instead of a clean rejection.
    """
    resp = client.post("/webhook/payments/töken", json=_payload())
    assert resp.status_code == 404


def test_schema_violations_are_rejected_before_any_logic(
    client: TestClient, deps: FakeDeps
) -> None:
    resp = client.post(f"/webhook/payments/{TOKEN}", json={"amount": "10.00"})
    assert resp.status_code == 422
    assert deps.ledger.credits == []


def test_negative_amount_is_refused(client: TestClient, deps: FakeDeps) -> None:
    resp = client.post(f"/webhook/payments/{TOKEN}", json=_payload(amount="-10.00"))
    assert resp.status_code == 422
    assert deps.ledger.credits == []
