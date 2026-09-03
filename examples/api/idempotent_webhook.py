"""An idempotent, ack-fast payment webhook.

Sanitized illustrative example — not production source. The real gateway
integration, payload schema, token scheme and credit logic are not reproduced.

Three properties, each of which exists because of a specific failure (docs/02):

  1. IDEMPOTENT on the provider's transaction id. Providers retry: when your
     response was slow, when it was lost, and sometimes when it succeeded. The
     defence is a UNIQUE CONSTRAINT in the database, not an application-level
     "check then insert", which races against itself under concurrent delivery.

  2. ACK-FAST. The 2xx is returned once the credit has committed and BEFORE the
     downstream activation. Holding the provider's connection open across a
     network command to physical hardware turns a slow router into a webhook
     timeout, which turns into a provider retry, which puts duplicate-delivery
     pressure on the money path.

  3. HONEST STATUS CODES. A malformed payload gets a 4xx and an alert -- not a
     200 with a log line. A webhook that logs a parse failure and still returns
     success has taken money it never credited, and the only trace is a log
     entry nobody is reading. See docs/07.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Annotated, Protocol, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from examples.domain.money import MoneyParseError, parse_minor_units

router = APIRouter(tags=["payments"])


class PaymentNotification(BaseModel):
    """Inbound confirmation. Field names are illustrative, not a real provider's."""

    provider_txn_id: str = Field(min_length=1, max_length=64)
    account_ref: str = Field(min_length=1, max_length=32)
    # A decimal STRING, deliberately. Accepting a float here would mean the
    # value has already lost precision before any of our code sees it, and no
    # amount of careful downstream handling recovers it.
    amount: str = Field(min_length=1, max_length=32)


@dataclass(frozen=True, slots=True)
class CreditResult:
    accepted: bool
    duplicate: bool


class Ledger(Protocol):
    async def credit(
        self, *, provider_txn_id: str, account_ref: str, amount_minor: int
    ) -> CreditResult:
        """Credit atomically, relying on a UNIQUE index on provider_txn_id.

        Must return duplicate=True (not raise) when the id is already present,
        so a retry produces the same response as the original call.
        """
        ...


class ActivationQueue(Protocol):
    async def enqueue(self, account_ref: str, provider_txn_id: str) -> None: ...


class Alerter(Protocol):
    async def critical(self, event: str, detail: str) -> None: ...


class Deps(Protocol):
    @property
    def ledger(self) -> Ledger: ...
    @property
    def queue(self) -> ActivationQueue: ...
    @property
    def alerter(self) -> Alerter: ...
    @property
    def webhook_token(self) -> str: ...


def get_deps(request: Request) -> Deps:
    """Wired at app startup. Kept as a dependency so tests inject fakes."""
    return cast(Deps, request.app.state.deps)


# NOTE ON SHAPE: a shared secret in the URL path is shown here because it is
# what many provider integrations force on you -- some callers cannot be
# configured to send headers. It is a WEAK position and worth stating why: a
# path segment is logged by proxies, CDNs, and anything downstream, so the
# credential leaks into places you do not control and cannot easily purge.
# Prefer a signed body (HMAC over the payload with a shared key) when the
# provider supports it; where it does not, treat the path secret as
# already-compromised and make the network control carry real weight.
@router.post("/webhook/payments/{token}", status_code=status.HTTP_200_OK)
async def receive_payment(
    token: str,
    payload: PaymentNotification,
    deps: Annotated[Deps, Depends(get_deps)],
) -> dict[str, str]:
    """Accept a payment confirmation.

    The 200 means "we have your money". It does NOT mean "the service is
    active" -- activation is queued and runs behind a per-entity lock. Those are
    two different claims and the system never conflates them: the "you are
    connected" message is sent by the activation path, after reconnection, not
    from here. Telling someone their internet is on while it isn't is worse
    than telling them nothing.
    """
    # Compare as bytes, in constant time. Two distinct hazards: `==` on decoded
    # strings leaks timing, AND unexpected input can raise inside the comparison
    # itself -- turning an auth check into an unhandled 500. Encoding first
    # closes both, which is why this is one line rather than a try/except.
    if not hmac.compare_digest(token.encode(), deps.webhook_token.encode()):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    try:
        amount_minor = parse_minor_units(payload.amount)
    except MoneyParseError as exc:
        # Loud, not quiet. An unparseable amount means a provider contract
        # change or an attack, and either way a human must look. Returning 200
        # here is the single most expensive mistake available on this endpoint.
        await deps.alerter.critical(
            "payment.amount_unparseable",
            f"txn={payload.provider_txn_id} reason={exc}",
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="amount not parseable",
        ) from exc

    result = await deps.ledger.credit(
        provider_txn_id=payload.provider_txn_id,
        account_ref=payload.account_ref,
        amount_minor=amount_minor,
    )

    if result.duplicate:
        # Same answer as the first delivery. A retry must be indistinguishable
        # from the original from the provider's point of view, and must NOT
        # re-enqueue activation.
        return {"status": "duplicate"}

    if not result.accepted:
        # Could not match the reference to an account. The money is real and
        # has arrived, so this is never a silent discard: it is recorded as
        # unmatched for operator resolution and alerted on.
        await deps.alerter.critical("payment.unmatched", f"txn={payload.provider_txn_id}")
        return {"status": "unmatched"}

    await deps.queue.enqueue(payload.account_ref, payload.provider_txn_id)
    return {"status": "accepted"}
