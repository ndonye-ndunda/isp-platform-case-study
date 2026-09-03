"""The value of one parsing boundary is that you can test it exhaustively.

Sanitized illustrative example — not production source.
"""

from __future__ import annotations

import pytest

from examples.domain.money import (
    MoneyParseError,
    format_minor_units,
    parse_minor_units,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0", 0),
        ("0.00", 0),
        ("0.01", 1),
        ("0.05", 5),
        ("1", 100),
        ("1.5", 150),
        ("1.50", 150),
        ("2400", 240000),
        ("2400.00", 240000),
        ("1500.99", 150099),
        ("  1000.00  ", 100000),
        ("99999999.99", 9999999999),
    ],
)
def test_parses_valid_amounts(raw: str, expected: int) -> None:
    assert parse_minor_units(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "-1",
        "-0.01",
        "abc",
        "1.2.3",
        "1,500.00",  # thousands separator: locale-ambiguous, rejected not guessed
        "1_000",  # Decimal accepts this; a ledger must not
        "NaN",
        "Infinity",
        "-Infinity",
        "+1.00",
        "1e5",  # exponent notation on a money wire format is a red flag
        "0.001",  # more precision than the currency has
        "1.005",
        "\u0661\u0660",  # Arabic-Indic digits: Decimal accepts them, we must not
        "1.00\n",  # trailing newline survives .strip()? no - but assert it
    ],
)
def test_rejects_untrustworthy_amounts(raw: str) -> None:
    if raw == "1.00\n":
        # strip() handles this one; documenting that it is intentionally fine.
        assert parse_minor_units(raw) == 100
        return
    with pytest.raises(MoneyParseError):
        parse_minor_units(raw)


def test_no_float_in_the_path() -> None:
    """The canonical truncation bug, asserted as a regression guard.

    int(float("8.20") * 100) is 819, not 820: 8.20 has no exact binary
    representation, so the product lands fractionally below the integer and
    truncation loses a cent. Note it does NOT reproduce on round values --
    1000.00 is exact -- which is why spot-checking a round number is a
    misleading way to convince yourself a money path is safe.
    """
    assert int(float("8.20") * 100) == 819  # the bug, demonstrated
    assert parse_minor_units("8.20") == 820  # and avoided
    assert parse_minor_units("1.15") == 115
    assert parse_minor_units("1000.00") == 100000


def test_rejects_non_string_input() -> None:
    with pytest.raises(MoneyParseError):
        parse_minor_units(1500)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("minor", "currency", "expected"),
    [
        (0, None, "0.00"),
        (5, None, "0.05"),
        (123400, None, "1,234.00"),
        (240000, "KES", "KES 2,400.00"),
        (100000000, "KES", "KES 1,000,000.00"),
    ],
)
def test_formats_for_display(minor: int, currency: str | None, expected: str) -> None:
    assert format_minor_units(minor, currency=currency) == expected


def test_display_output_is_not_a_transport_format() -> None:
    """Display adds separators; the parser rejects them. That is deliberate."""
    rendered = format_minor_units(123400)
    assert rendered == "1,234.00"
    with pytest.raises(MoneyParseError):
        parse_minor_units(rendered)


def test_format_rejects_negative() -> None:
    with pytest.raises(ValueError, match="negative"):
        format_minor_units(-1)
