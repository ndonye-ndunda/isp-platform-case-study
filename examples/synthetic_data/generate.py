"""Deterministic synthetic fixtures. Nothing here resembles real data.

Sanitized illustrative example — not production source.

Every identifier is drawn from a reserved-for-documentation range so that a
value from this file can never collide with a real one:

    plan prices     round hundreds     -- fabricated; asserted round by tests
    phone numbers   +254 700 000 0NN   -- a reserved test block, never assigned
    IP addresses    192.0.2.0/24, 203.0.113.0/24  (RFC 5737 TEST-NET-1/-3)
    domains         *.example.test     (RFC 6761 reserved)
    account refs    ACC-0001 ...       -- obviously sequential, obviously fake
    names           from a fixed word list, not a name corpus

Seeded, so the suite is reproducible: a failing test can be re-run and produce
the same rows. Unseeded fixtures give you flaky tests you cannot reproduce and
then stop trusting.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

__all__ = [
    "CANONICAL_CHARGE_MINOR",
    "SyntheticPayment",
    "SyntheticSubscriber",
    "generate_payments",
    "generate_subscribers",
]

# The one example charge, defined once so nothing downstream hardcodes a
# second copy that could drift -- or be replaced with a real figure in a
# file the guard does not read. Round by construction; asserted round by
# tests/test_synthetic_data.py.
CANONICAL_CHARGE_MINOR = 250_000

# Deliberately bland and clearly synthetic. Using a real name corpus would
# produce fixtures that look like leaked data, which is a bad property for a
# public repository even when the rows are fabricated.
_FIRST = ("Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf")
_LAST = ("Tester", "Sample", "Example", "Fixture", "Placeholder")

# Fabricated tier prices. Deliberately ROUND values that match no real
# tariff -- and asserted as round by the test suite, because a plausible
# price is the one field class with no reserved-range analogue to fall back
# on. A real price triple would be a public-record join key: retail tariffs
# are published, so an exact match identifies an operator.
_PLANS: tuple[tuple[str, int], ...] = (
    ("basic", 100_000),
    ("standard", CANONICAL_CHARGE_MINOR),
    ("premium", 500_000),
)

_SITES = ("site-alpha", "site-bravo", "site-charlie")


@dataclass(frozen=True, slots=True)
class SyntheticSubscriber:
    account_ref: str
    display_name: str
    msisdn: str
    plan: str
    charge_minor: int
    site: str
    balance_minor: int
    cycle_end: date | None


@dataclass(frozen=True, slots=True)
class SyntheticPayment:
    provider_txn_id: str
    account_ref: str
    amount: str
    received_on: date


def generate_subscribers(
    count: int, *, seed: int = 1729, today: date | None = None
) -> Sequence[SyntheticSubscriber]:
    """Build `count` synthetic subscribers.

    The reserved +254700000000 block gives 100 distinct values, so this caps at
    100 rather than wrapping around and silently producing duplicate phone
    numbers -- a fixture generator that quietly repeats a unique field produces
    test failures that look like application bugs.
    """
    if not 0 < count <= 100:
        raise ValueError("count must be between 1 and 100 (reserved-block limit)")

    rng = random.Random(seed)
    anchor = today or date(2026, 1, 15)
    out: list[SyntheticSubscriber] = []

    for i in range(count):
        plan, charge = _PLANS[i % len(_PLANS)]
        # Balances chosen to exercise all three branches of the cycle decision:
        # cannot afford, exactly affords, comfortable surplus.
        balance = rng.choice([0, charge // 3, charge, charge * 2])
        out.append(
            SyntheticSubscriber(
                account_ref=f"ACC-{i + 1:04d}",
                display_name=f"{rng.choice(_FIRST)} {rng.choice(_LAST)}",
                msisdn=f"+2547000000{i:02d}",
                plan=plan,
                charge_minor=charge,
                site=_SITES[i % len(_SITES)],
                balance_minor=balance,
                cycle_end=anchor + timedelta(days=rng.randint(-10, 25)),
            )
        )
    return out


def generate_payments(
    subscribers: Sequence[SyntheticSubscriber],
    *,
    seed: int = 1729,
    duplicate_rate: float = 0.1,
) -> Sequence[SyntheticPayment]:
    """Build payments, deliberately including duplicates.

    `duplicate_rate` exists because duplicate delivery is the NORMAL case on a
    payment webhook, not an edge case: providers retry when your response was
    slow, when it was lost, and sometimes when it succeeded. A fixture set with
    no duplicates cannot exercise the idempotency path, which is the single
    most important property of that endpoint.
    """
    rng = random.Random(seed)
    out: list[SyntheticPayment] = []

    for i, sub in enumerate(subscribers):
        txn = f"TEST-TXN-{i + 1:06d}"
        payment = SyntheticPayment(
            provider_txn_id=txn,
            account_ref=sub.account_ref,
            # A decimal STRING, matching the wire format: fixtures should have
            # the same shape as production input, or they test a different
            # function than the one you deployed.
            amount=f"{sub.charge_minor // 100}.{sub.charge_minor % 100:02d}",
            received_on=date(2026, 1, 10) + timedelta(days=i % 28),
        )
        out.append(payment)
        if rng.random() < duplicate_rate:
            out.append(payment)  # identical id -- must be handled idempotently

    return out
