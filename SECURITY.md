# Disclosure posture

This repository is a **technical case study**. It documents the architecture and
engineering decisions of a production system; it is not that system's source.

## What is not here, and will not be

The production repository is private and stays private. Nothing below has been
copied, paraphrased into reproducible form, or reconstructed here:

- Application source, database migrations, tests, or operational scripts
- Billing rules, the suspension decision tree, pricing, grandfathered rates, the
  churn rubric's point values, or the cycle activation sequence
- Payment-gateway integration: request shapes, callback contracts, token
  schemes, or reconciliation logic
- Credentials, secrets, tokens, API keys, hashes, or `.env` files of any kind
- Customer data: names, phone numbers, account numbers, identity documents,
  balances, payment history — in any form, real or derived from real
- Infrastructure identifiers: hostnames, domain names, IP addresses, VPN
  topology or keys, RADIUS shared secrets, router credentials, SSH keys
- Router, RADIUS, reverse-proxy, log-shipper or alerting configuration
- Dashboards, alert thresholds, runbooks, or go-live procedures
- The AI agent's system prompt text, its tool definitions, and its query
  patterns. (Aggregate measurements *are* discussed — tool count and prompt
  token budget — because the measurement is the engineering point and it
  reveals nothing about what the tools do or what they are pointed at.)

## What sanitization means here

The exercise is not word-substitution. Every document was written for this
repository against an understanding of the system, and the examples were written
independently — see [`examples/README.md`](examples/README.md).

Three specific rules were applied:

**1. The operator is anonymous.** No company name, product names, service tiers,
site names, or region within the country. "A production fibre ISP in Kenya" is
as specific as it gets.

**2. Infrastructure technology is named; commercial providers are not.**
Self-hostable, widely-deployed platforms are named where they carry a technical
point — FreeRADIUS, MikroTik RouterOS, PostgreSQL, Redis, Prometheus, Loki,
Grafana, WireGuard — because the alternative is a case study too vague to
evaluate, and naming a platform used by hundreds of thousands of operators
identifies nobody.

**Every commercial service provider is described generically**, including the
ones whose integration was a substantial piece of work: the payment rail is
"a mobile-money C2B payment gateway", the messaging channel is "an SMS gateway",
and hosting, overlay VPN, object storage, DNS, the workflow engine, the
inference runtime and the model are all referred to by function rather than
name. Naming any single one of them narrows the field, and none of them is
necessary to follow the engineering.

The metric namespace, service names, container names and internal endpoint paths
have all been replaced with generic equivalents. (A production-derived metric
prefix survived the first pass of this review and was caught by the second — the
scan for identifiers has to run over patterns, not just over the obvious
spelling of the name.)

**3. Synthetic values only — and the test suite proves it.** Phone numbers come
from a reserved test block, addresses from the RFC 5737 documentation networks,
domains from `.example.test`. Those are checkable against a published range, and
they are checked.

Money amounts are the interesting case, and the one this repository got wrong
twice before getting right. There is no reserved range for a price: a real tariff
and an invented one are indistinguishable by inspection, so a genuine figure can
be pasted into a fixture and survive every identifier scan — inside a file whose
own docstring promises nothing is real. It is also the costliest class to get
wrong, because retail prices are published, which makes an exact match a
public-record join key.

The first guard asserted a *property* — round amounts, on the theory that real
prices carry distinctive trailing digits. A mutation test then pasted a genuine
tariff that happens to be a round thousand, and the guard passed. **No property
of a number can distinguish a real price from an invented one.**

So the plan amounts and every amount in the JSON fixture are **pinned**: they are
enumerated in the test suite, and any change fails the build. Being exact about
what that buys, since overclaiming here is what produced the previous two
versions:

- It stops an amount drifting **silently** — someone making a fixture "more
  realistic" and a live figure landing where no scanner reads. That is the
  realistic failure mode and it is now closed.
- It does **not** stop an author who edits both the data and the pin
  deliberately. Nothing in CI can, and saying otherwise would be exactly the
  false assurance this document is supposed to avoid.
- The money-parser conformance corpus is **out of scope**, deliberately: its
  values exist to exercise float and boundary behaviour and are attached to no
  plan or payment.

A leak-prevention rule nobody checks is a preference. One that claims more than
it checks is worse, because it stops the next reader looking.

## On the incidents described

[Document 7](docs/07-failure-modes-and-lessons.md) discusses failures candidly,
because that is the most useful part of the case study and because a portfolio
that only describes successes tells you nothing about judgment.

They are described as **failure classes with mechanisms and mitigations** —
never with dates, customer impact, site identities, or operational specifics
that would matter to anyone but an engineer learning from the mechanism. A
finding is included only where the *general* lesson survives having the specifics
removed. Where it does not, it is left out.

**The rule applied throughout is: describe the reasoning, never the
configuration.** Explaining why a class of control matters, how a class of gap
hides, and what closes it — that is the transferable content and it is what this
repository contains. Statements about what any deployed system currently has, or
lacks, are not, and the passages that once carried them have been rewritten as
principles rather than status.

That rule was arrived at the hard way. Successive review passes each removed
what the previous one had looked for and missed a further class, because
pattern-matching finds only the leak you already imagined. The passes that
worked were the ones that asked of every paragraph *is this reasoning, or is
this configuration?* — and the single worst item found was not an identifier at
all but a set of numbers that looked synthetic and was not. Which is why the
fixtures now assert a *property* of their own values rather than being trusted to
contain safe ones.

If you find a passage that reads as configuration rather than reasoning, that is
a bug in this document as much as in the passage. Please say so.

## Reporting something

If you believe something in this repository discloses more than intended —
a value that looks real, an identifier that slipped through, an inference this
document did not anticipate — please open an issue **without including the
sensitive content itself**, or contact me through my GitHub profile. I would
rather revise this than defend it.

## Scope

There is no deployed system, service, endpoint or package associated with this
repository, so there is no vulnerability-disclosure process in the usual sense.
The example code is illustrative and is **not intended for production use**: it
demonstrates patterns and omits the surrounding concerns — configuration
validation, migrations, connection management, observability wiring — that a real
deployment requires.
