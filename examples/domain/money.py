"""Currency handling: parse once, at one boundary, into integer minor units.

Sanitized illustrative example — not production source.

The pattern this demonstrates (see docs/02-billing-and-payments.md):

  1. Money is an integer count of minor units everywhere internally.
  2. External input is converted at exactly ONE function.
  3. Conversion back to a human-readable string happens only at a UI boundary.

Rule 2 is what makes rule 1 enforceable. A single conversion point can be
tested exhaustively; ten conversion points is a place where the eleventh gets
it wrong.

Floating point is never used. `0.1 + 0.2 != 0.3` in IEEE 754, and in a ledger
that error compounds into a balance that is *almost* right, indefinitely, which
is the hardest class of bug to notice and the most expensive to reconstruct.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

__all__ = ["MoneyParseError", "format_minor_units", "parse_minor_units"]

# Characters we accept in an external amount. Notably absent: the comma.
# Thousands separators are locale-ambiguous ("1,500" is one thousand five
# hundred in some locales and one-point-five in others), so we reject rather
# than guess. Guessing on a money path is how you charge someone 1000x.
_ALLOWED = set("0123456789.")


class MoneyParseError(ValueError):
    """An external amount could not be trusted.

    Deliberately its own type. Callers on the payment path need to distinguish
    "this payload is malformed" from any other ValueError raised nearby, because
    the correct response differs: a malformed amount must be surfaced loudly
    (alert + audit) rather than swallowed by a generic handler that returns a
    success status to the payment provider. See docs/07 -- a webhook that logs a
    parse failure and still returns 200 has taken money it never credited.
    """


def parse_minor_units(raw: str, *, minor_unit_digits: int = 2) -> int:
    """Convert an external decimal amount string to integer minor units.

    >>> parse_minor_units("1000.00")
    100000
    >>> parse_minor_units("0.05")
    5
    >>> parse_minor_units("2500")
    250000

    The conversion is str -> Decimal -> int. There is no float anywhere in the
    path, including intermediately. `int(float("8.20") * 100)` is 819, not 820,
    because 8.20 has no exact binary representation and the product lands just
    below the integer. That is the exact bug this exists to avoid, and it is
    worth knowing that it does NOT reproduce for every value -- 10.10 is exact,
    so a spot-check on a tidy number will tell you the code is fine when it is
    not.

    Raises:
        MoneyParseError: empty, negative, non-numeric, non-finite, or carrying
            more fractional digits than the currency has.
    """
    if not isinstance(raw, str):  # pragma: no cover - defensive, see docstring
        raise MoneyParseError(f"amount must be a string, got {type(raw).__name__}")

    text = raw.strip()
    if not text:
        raise MoneyParseError("amount is empty")

    # Reject before Decimal sees it. Decimal happily accepts "NaN", "Infinity",
    # "1_000" and leading "+", none of which should reach a ledger, and each of
    # which fails later in a place with less context than here.
    if not text or not set(text) <= _ALLOWED:
        raise MoneyParseError(f"amount has unexpected characters: {raw!r}")
    if text.count(".") > 1:
        raise MoneyParseError(f"amount has more than one decimal point: {raw!r}")

    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise MoneyParseError(f"amount is not a decimal: {raw!r}") from exc

    if not value.is_finite():  # pragma: no cover - unreachable given _ALLOWED
        raise MoneyParseError(f"amount is not finite: {raw!r}")
    if value < 0:
        raise MoneyParseError(f"amount is negative: {raw!r}")

    exponent = value.as_tuple().exponent
    # `exponent` is an int for finite values; the str forms ('n', 'N', 'F') are
    # unreachable here because is_finite() has already returned True.
    assert isinstance(exponent, int)
    if -exponent > minor_unit_digits:
        raise MoneyParseError(
            f"amount has more than {minor_unit_digits} fractional digits: {raw!r}"
        )

    scaled = value.scaleb(minor_unit_digits)
    minor = int(scaled)
    if Decimal(minor) != scaled:  # pragma: no cover - guarded by the check above
        raise MoneyParseError(f"amount is not representable in minor units: {raw!r}")
    return minor


def format_minor_units(
    minor: int, *, minor_unit_digits: int = 2, currency: str | None = None
) -> str:
    """Render integer minor units for display. UI boundary only.

    >>> format_minor_units(250000, currency="KES")
    'KES 2,500.00'
    >>> format_minor_units(5)
    '0.05'

    Never feed the output of this back into parse_minor_units -- the grouping
    separator is deliberately rejected on the way in. Display and transport are
    different formats and conflating them is a round-trip bug waiting to happen.
    """
    if minor < 0:
        raise ValueError("minor units must not be negative")

    scale = 10**minor_unit_digits
    whole, fraction = divmod(minor, scale)
    rendered = f"{whole:,}.{fraction:0{minor_unit_digits}d}"
    return f"{currency} {rendered}" if currency else rendered
