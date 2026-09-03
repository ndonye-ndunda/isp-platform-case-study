"""Fixtures must be reproducible, obviously fake, and exercise the hard paths.

Sanitized illustrative example — not production source.
"""

from __future__ import annotations

import ipaddress
import json
import pathlib
import re

import pytest

from examples.domain.money import (
    MoneyParseError,
    parse_minor_units,
)
from examples.synthetic_data.generate import (
    _PLANS,
    CANONICAL_CHARGE_MINOR,
    generate_payments,
    generate_subscribers,
)

PAYLOADS = (
    pathlib.Path(__file__).resolve().parents[1] / "synthetic_data" / "sample_payloads.json"
)


def test_generation_is_deterministic() -> None:
    """An unseeded fixture gives you flaky tests you cannot reproduce."""
    assert generate_subscribers(10) == generate_subscribers(10)


def test_phone_numbers_are_in_the_reserved_test_block() -> None:
    for sub in generate_subscribers(50):
        assert re.fullmatch(r"\+2547000000\d{2}", sub.msisdn), sub.msisdn


def test_phone_numbers_are_unique() -> None:
    subs = generate_subscribers(100)
    assert len({s.msisdn for s in subs}) == 100


def test_generator_refuses_to_exceed_the_reserved_block() -> None:
    """Rather than wrapping and silently duplicating a unique field."""
    with pytest.raises(ValueError, match="reserved-block"):
        generate_subscribers(101)
    with pytest.raises(ValueError):
        generate_subscribers(0)


def test_account_refs_are_obviously_sequential() -> None:
    subs = generate_subscribers(5)
    assert [s.account_ref for s in subs] == [
        "ACC-0001",
        "ACC-0002",
        "ACC-0003",
        "ACC-0004",
        "ACC-0005",
    ]


def test_balances_exercise_every_affordability_branch() -> None:
    subs = generate_subscribers(40)
    can = [s for s in subs if s.balance_minor >= s.charge_minor]
    cannot = [s for s in subs if s.balance_minor < s.charge_minor]
    assert can and cannot, "fixtures must cover both sides of the decision"


def test_payments_include_duplicates() -> None:
    """Duplicate delivery is the NORMAL case on a payment webhook.

    A fixture set with no duplicates cannot exercise the idempotency path,
    which is that endpoint's single most important property.
    """
    subs = generate_subscribers(40)
    payments = generate_payments(subs, duplicate_rate=0.2)
    ids = [p.provider_txn_id for p in payments]
    assert len(ids) > len(set(ids))


def test_payment_amounts_parse_through_the_real_boundary() -> None:
    """Fixtures must have the same shape as production input.

    If they don't, the tests exercise a different function than the one you
    deployed.
    """
    subs = generate_subscribers(20)
    for payment in generate_payments(subs, duplicate_rate=0.0):
        assert parse_minor_units(payment.amount) > 0


def test_sample_payloads_are_valid_json() -> None:
    json.loads(PAYLOADS.read_text())


def test_sample_payload_addresses_are_documentation_ranges() -> None:
    """RFC 5737 TEST-NET blocks only -- never a real address."""
    blob = PAYLOADS.read_text()
    for found in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", blob):
        addr = ipaddress.ip_address(found)
        assert any(
            addr in ipaddress.ip_network(net)
            for net in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
        ), f"{found} is not a documentation address"


def test_sample_payload_phone_numbers_are_reserved() -> None:
    blob = PAYLOADS.read_text()
    for found in re.findall(r"\+254\d{9}", blob):
        assert found.startswith("+2547000000"), found


# The amounts this repository is allowed to contain, pinned. See
# test_amounts_are_pinned for why this is a pin rather than a property.
EXPECTED_PLANS: tuple[tuple[str, int], ...] = (
    ("basic", 100_000),
    ("standard", 250_000),
    ("premium", 500_000),
)


def test_amounts_are_pinned_not_merely_checked_for_shape() -> None:
    """Guard for the leak class that identifier scanning cannot catch.

    A phone number or an address can be checked against a published reserved
    range. A *price* cannot, and the reason is worth stating precisely because
    the first two versions of this guard both got it wrong:

    **No property of a number can distinguish a real tariff from an invented
    one.** The first version asserted "round hundreds", on the theory that real
    prices carry distinctive trailing digits. A mutation test then pasted in a
    genuine tariff that happens to be a round thousand, and the guard passed.
    Real prices can be round. There is no shape to test for.

    So this is a **pin**, not a property. The amounts are enumerated here, and
    any change to them fails this test -- which forces the change through a
    second file and a human reading this docstring. That is the honest scope,
    and it is worth being exact about what it does and does not buy:

    - It DOES stop an amount drifting silently, which is the realistic failure:
      someone makes a fixture "more realistic" and a live figure lands in a
      file no scanner reads.
    - It does NOT stop an author who edits both files deliberately. Nothing in
      CI can, and claiming otherwise is the overclaiming this repository is a
      case study about.

    Why it matters: retail prices are published, so an exact match is a
    public-record join key, and an anonymized case study carrying one can name
    its own subject.
    """
    assert _PLANS == EXPECTED_PLANS, (
        "plan amounts changed. This is a pinned value, not a free parameter: "
        "confirm the new figures are invented and match no real tariff, then "
        "update EXPECTED_PLANS in this file."
    )
    assert (
        CANONICAL_CHARGE_MINOR == 250_000
    ), "the canonical charge changed - same check as above before updating this pin"


def test_generated_amounts_stay_round() -> None:
    """Cheap secondary check: nothing acquires a sub-unit part downstream.

    Not a leak guard -- the pin above is that. This catches arithmetic in the
    generator accidentally producing fractional currency.
    """
    subs = generate_subscribers(30)
    for payment in generate_payments(subs, duplicate_rate=0.0):
        minor = parse_minor_units(payment.amount)
        assert minor % 100_00 == 0, f"{payment.amount} is not a whole-hundred amount"


def test_every_fixture_amount_is_a_whole_hundred() -> None:
    """Covers the JSON fixture, which the two guards above do not reach.

    This is the gap that mattered. The first version of this guard read only
    ``_PLANS`` and the generator's output, so a real tariff pasted into
    sample_payloads.json left the whole suite green while the disclosure
    document promised that could not happen -- a check reporting success without
    verifying its claim, which is the exact failure this repository is a case
    study about. The lesson is in docs/07: assert on the artifact a reader will
    actually see, not on the code path you happened to be looking at.

    Scope is deliberate and stated: every amount-like value in the fixture,
    including amounts rendered inside message bodies. It does NOT cover the
    money-parser conformance corpus in test_money.py, whose values (8.20, 1.15,
    1500.99, 99999999.99) exist to exercise real float and boundary behaviour
    and are attached to no plan or payment.
    """
    blob = json.loads(PAYLOADS.read_text())
    found: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "amount" and isinstance(value, str):
                    found.append(value)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str):
            found.extend(re.findall(r"KES ([\d,]+\.\d{2})", node))

    walk(blob)
    assert found, "fixture carries no amounts - has the guard drifted from the data?"

    distinct: set[int] = set()
    for raw in found:
        try:
            minor = parse_minor_units(raw.replace(",", ""))
        except MoneyParseError:
            continue  # deliberately-invalid input, e.g. the separator rejection case
        assert minor % 100_00 == 0, f"fixture amount {raw!r} is not a whole hundred"
        distinct.add(minor)

    # Pinned, for the reason given in test_amounts_are_pinned: shape alone
    # cannot tell a real tariff from an invented one.
    assert distinct == {CANONICAL_CHARGE_MINOR}, (
        f"fixture carries unexpected amounts {sorted(distinct)}; the fixture "
        "should use only the canonical charge. Confirm any new figure is "
        "invented before widening this pin."
    )
