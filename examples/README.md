# Illustrative examples

**Every file in this directory was written independently for this repository.**
None of it is copied, adapted, or mechanically transformed from the production
codebase it describes. No proprietary algorithm, business rule, pricing table,
payment-gateway contract, message template or system prompt is reproduced here.

What these demonstrate is the **shape** of a design and the reasoning behind
it — the part that is transferable, and the part a reader can actually evaluate.
Every file here — the modules, the tests, the YAML rules and the JSON fixture —
carries the header:

> Sanitized illustrative example — not production source.

## Why they run

Because the case study argues that conventions should be enforced mechanically,
and shipping unchecked snippets alongside that argument would undercut it.

```bash
pip install ruff mypy pytest fastapi httpx
ruff check . && ruff format --check .
mypy examples/          # --strict
pytest                  # 119 tests
python examples/guards/convention_guards.py $(git ls-files '*.py')
```

All five run in [CI](../.github/workflows/ci.yml) on every push.

The dependency surface is deliberately tiny: pytest, plus FastAPI and httpx for
the one HTTP example. No database, no Redis, no services to provision. Patterns
that would normally need infrastructure — distributed locking, dual-store commit
ordering, worker liveness — are written against small `Protocol` interfaces and
exercised with in-memory fakes. That is a design choice worth noticing on its
own: **the reason these are testable in 0.7 seconds is the same reason the real
ones are testable at all.**

## What's here

| File | Demonstrates |
|---|---|
| [`domain/money.py`](domain/money.py) | Integer minor units; one parsing boundary; no float in the path |
| [`api/idempotent_webhook.py`](api/idempotent_webhook.py) | Idempotency on a provider id, ack-fast response, honest status codes |
| [`concurrency/entity_lock.py`](concurrency/entity_lock.py) | Per-entity mutex; two failure types; instrument rather than extend |
| [`concurrency/dual_store_commit.py`](concurrency/dual_store_commit.py) | Commit ordering that makes the *detectable* inconsistency the reachable one |
| [`workers/job_wrapper.py`](workers/job_wrapper.py) | Scheduled-job wrapper; crash-as-signal heartbeat semantics |
| [`observability/liveness_metrics.py`](observability/liveness_metrics.py) | A roster series, so a never-run worker is still visible |
| [`observability/absent_guard.rules.yml`](observability/absent_guard.rules.yml) | Alert rules that survive their own data going missing |
| [`ai/readonly_tool_gateway.py`](ai/readonly_tool_gateway.py) | Read-only-by-construction tool surface; schema token budget |
| [`ai/approval_gate.py`](ai/approval_gate.py) | Proposal queue, allowlist, rate limit, kill switch, audit |
| [`ai/eval_harness.py`](ai/eval_harness.py) | Grounding / completeness / calibration / refusal scoring |
| [`guards/convention_guards.py`](guards/convention_guards.py) | Conventions as a build-failing check |
| [`synthetic_data/generate.py`](synthetic_data/generate.py) | Deterministic fixtures from reserved ranges only |

## Read the tests

The tests carry as much of the argument as the modules do, because they record
*which failures each design was built to prevent*. A few worth opening directly:

- [`test_dual_store_commit.py`](tests/test_dual_store_commit.py) —
  `test_device_failure_does_not_commit_the_ledger` is the whole reason for the
  ordering.
- [`test_liveness_metrics.py`](tests/test_liveness_metrics.py) —
  `test_never_succeeded_worker_emits_roster_but_no_timestamp` is the
  absent-series problem in six lines.
- [`test_eval_harness.py`](tests/test_eval_harness.py) —
  `test_incomplete_answer_fails_even_though_it_is_correct` encodes a real
  acceptance-run defect: the answer was true and incomplete, and a
  correctness-only eval scores it as a pass.
- [`test_idempotent_webhook.py`](tests/test_idempotent_webhook.py) —
  `test_unparseable_amount_returns_4xx_and_alerts` is the single most expensive
  mistake available on a money endpoint, asserted against.
- [`test_convention_guards.py`](tests/test_convention_guards.py) — the guards'
  own tests caught two live bugs in the guards while this repository was being
  written: a regex whose leading character class consumed the keyword it was
  searching for, and single-line docstrings never being skipped because their
  triple-quote count is *even*. Both are noted in the source. Guards need tests
  for the same reason alert rules do.

## Synthetic data

Identifier values are drawn from ranges reserved for documentation, so they
cannot collide with a real one. Values with no such range — names, account
references, amounts — are fabricated instead, and the ones that matter are
pinned by the test suite rather than trusted:

| Kind | Range | Reserved by |
|---|---|---|
| Phone numbers | `+254 700 000 0NN` | Reserved test block |
| IP addresses | `192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24` | RFC 5737 |
| Domains | `*.example.test` | RFC 6761 |
| Account refs | `ACC-0001`… | Obviously sequential |
| Names | A fixed seven-word list | Not a name corpus, on purpose |

Two of these are asserted in the test suite
([`test_synthetic_data.py`](tests/test_synthetic_data.py)): every IP literal in
the sample payloads must parse into an RFC 5737 network, and every phone
literal must sit in the reserved block. A leak-prevention rule that isn't
checked is a leak-prevention preference.

---

**Back:** [Repository overview](../README.md) ·
[Disclosure posture](../SECURITY.md)
