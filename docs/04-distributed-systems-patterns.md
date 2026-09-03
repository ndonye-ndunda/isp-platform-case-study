# 4. Distributed systems patterns

> Illustrative architecture. Working, tested implementations of these patterns
> are in [`examples/`](../examples/) — independently written, not production
> source. See [SECURITY.md](../SECURITY.md).

Nine scheduled jobs, an HTTP API and a queue consumer all mutate the same
subscriber state. This section is about the primitives that make that safe, and
about how each of them is designed to *fail loudly*.

## Per-entity mutual exclusion

Any operation touching subscription status, RADIUS group membership, or sending a
disconnect must hold a lock scoped to that one subscriber.

```
key:               entity_lock:{entity_id}
lease (TTL):       > longest legitimate critical section, by a margin
acquisition wait:  short — then raise, do not block indefinitely
retry:             none, internally
reentrant:         no
```

The two durations are deliberately given as constraints rather than numbers,
because the numbers are the least transferable part: the lease has to exceed the
slowest critical section by enough margin to survive a slow dependency, and the
acquisition wait has to be short enough that a queued caller fails fast instead
of piling up. Pick them from measured section duration, and revisit them when
that measurement moves.

Five design decisions in that table, each with a reason.

**Scoped per entity, not globally.** A global lock would serialise the entire
suspension sweep. Per-subscriber locking means concurrent work on different
subscribers proceeds in parallel, and contention only occurs where it is
semantically real — the same person paying while the suspension job is evaluating
them, which is exactly the race worth serialising.

**A lease, not a lock.** The TTL is a crash safety net. A process that dies
holding a lock must not lock that subscriber out forever; Redis expires the key.
The TTL is set well above the longest legitimate critical section.

**Built on the client library's lock, not hand-rolled.** This is the single
highest-stakes primitive in the system, and correct distributed locking has
details that are easy to get wrong — notably that release must be an *atomic*
check-and-delete, because a lock whose lease expired and was re-acquired by
someone else must not be releasable by the original holder. The library does that
with an internal script and is well tested. The project's own code adds only the
key convention, the exception types and the instrumentation.

Knowing which wheels not to reinvent is a skill. `SET NX` plus a hand-written
release is a plausible-looking twenty lines that is wrong in a way you find out
about during an incident.

**Typed failures, because they need different responses.** Two exceptions, not
one:

| Exception | Means | Correct response |
|---|---|---|
| `LockTimeout` | Someone else holds it | Retry — this is *normal* |
| `LockUnavailable` | Redis itself is unreachable | Escalate — a dependency is down |

Collapsing these into a generic error is a real loss. A retry loop against a dead
Redis is an infinite loop that looks like contention; escalating on ordinary
contention pages a human about the system working correctly. The distinction is
*in the type*, so a caller cannot accidentally handle them the same way.

**Not reentrant, and acquired once at the outermost boundary.** The underlying
lock is not reentrant, so a function holding it for a subscriber that calls
another function which also acquires it for the same subscriber will deadlock
against itself — waiting the full acquisition timeout and then raising.

There is no code-level enforcement for this. It is an architectural rule
maintained by design and review. That is stated plainly in the project's own
documentation — *the code cannot enforce this; design for it* — because a
convention you cannot mechanise should at least be a convention you have written
down and admitted is fragile.

**Instrumented rather than extended.** No watchdog or lease-extension mechanism
was built. Instead, holding the lock longer than a threshold logs a warning.

The reasoning is worth spelling out: a watchdog is real complexity — a background
task racing the lease, with its own failure modes — justified only if critical
sections actually approach the TTL. Nobody knew whether they did. So the cheap
thing that produces the data was built first, on the basis that the warning
either fires (and now there's evidence for the watchdog) or it doesn't (and the
watchdog was never needed).

Implementation: [`examples/concurrency/entity_lock.py`](../examples/concurrency/entity_lock.py).

## Two stores, one ordered write

A state change spans PostgreSQL (authoritative) and MariaDB (the RADIUS
projection), plus a UDP packet to hardware. There is no distributed transaction
available, and introducing two-phase commit for this would be a poor trade.

The ordering is chosen so that the *cheapest-to-repair* inconsistency is the only
reachable one:

```
1. acquire per-subscriber lock
2. write the RADIUS projection        (commits inside its own service call)
3. send the disconnect                (network I/O, may fail)
4. commit the billing transaction     (only if 2 and 3 succeeded)
5. audit                              (independent transaction)
6. enqueue notification
```

Billing commits **last**. Which means the failure window is:

- RADIUS updated, billing not committed → the projection is ahead of the truth.
  Reconciliation detects it and reports it. Recovery is re-deriving the projection
  from billing state, which is idempotent and safe.
- The inverse — billing committed, RADIUS never updated — would mean the ledger
  believes someone is active while the network disagrees, and *nothing in the
  system would notice*, because billing is the authority everything else is
  compared against.

So the ordering is picked to make the detectable inconsistency the reachable one.
That is the whole reasoning, and it generalises: when you cannot have atomicity,
choose which inconsistency you get, and choose the one you can see.

The commit contracts are deliberately **asymmetric** — billing commits in the
outer caller; the RADIUS service commits internally. This is surprising enough
that it is documented as an explicit contract rather than left to be inferred,
because someone adding a caller and assuming symmetry would produce a
subtly-broken sequence that passes tests.

Illustration: [`examples/concurrency/dual_store_commit.py`](../examples/concurrency/dual_store_commit.py).

## The audit trail commits independently

Audit records are written in their own transaction, separate from the operation
being audited.

This inverts the intuitive design, and the reason is the case that matters most.
If audit rows joined the main transaction, then **every failure would roll back
its own audit record** — the system would have a complete history of successes
and no record whatsoever of failures. Precisely backwards.

So: audit for a success is written *after* the main commit; audit for a failure is
written *inside the exception handler*, in an independent transaction that
survives the main rollback.

The audit writer also never raises into its caller. A logging subsystem that can
fail an operation is a liability — the tail wagging the dog. It logs its own
failure and returns.

### Honest stubs

A convention with more value than its size suggests: when a mechanism is not yet
implemented, its audit result is `SKIPPED`, never `SUCCESS`.

This sounds like pedantry until you consider what the alternative produces. A
stub that records success generates an audit trail asserting that things happened
which did not happen — and that trail is the thing you consult during an incident
to establish what the system did. One `SUCCESS` from a stub poisons the evidence.

This is the same failure family as everything in
[§7](07-failure-modes-and-lessons.md): a stub recording success is a broken path
returning something that looks like success.

## Scheduled jobs and one registration wrapper

Nine jobs. Every one is registered through a single wrapper, and no job
constructs its own trigger. The wrapper attaches, automatically:

- an explicit timezone (never the scheduler's default — see
  [§2](02-billing-and-payments.md))
- heartbeat recording, opened before the job body and closed after
- bound log context, so every line a job emits carries its identity and run

Registration is an **explicit call from the process entrypoint**, never an import
side-effect. Import-time registration means the set of running jobs depends on
which modules happened to be imported, which is a property no one can read off
the code — and which changes when someone adds an unrelated import.

Constructing a trigger outside the wrapper is a build failure. The wrapper's job
is to make the things that must not be forgotten unforgettable, and an escape
hatch that compiles defeats it.

Implementation: [`examples/workers/job_wrapper.py`](../examples/workers/job_wrapper.py).

## Crash semantics: the frozen row *is* the signal

Each job run writes a heartbeat: a row set to `running` at start, updated to
`completed` or `failed` at the end.

A job that raises is caught and recorded `failed`. A job whose **process is
killed** — OOM, `SIGKILL`, host reboot — writes nothing further. Its row stays
`running`, with a null completion timestamp, forever.

That frozen row is not a bug to be tidied up. It is the only evidence a hard
crash produces, and it is therefore the alert signal. The project's documentation
says so explicitly, with an instruction not to "fix" it — because the tidy-looking
change (a startup sweep that marks stale `running` rows as `failed`) would destroy
the distinction between *crashed* and *raised*, which are different problems with
different causes.

Two states, two signals:

| Terminal state | Means | Detected by |
|---|---|---|
| `failed` | Exception, caught and recorded | Failure-rate rules |
| `running`, stale | Process died mid-run | Staleness rules on `last_success` |

The alerting then has to cover a third case that neither of those does: a worker
with **no history at all**. A staleness rule compares against a last-success
timestamp; a worker that has never succeeded has no timestamp, no series, and
therefore no alert — the absent-series problem again. The fix is a registry
series listing every worker that *should* exist, joined against the
last-success series, so a worker that has never once run is still visible as
missing.

That specific gap — *a crashed worker pages nobody* — was open and tracked for a
period before it was closed. It is in the register with its close date.

Metric shape: [`examples/observability/liveness_metrics.py`](../examples/observability/liveness_metrics.py).

## Queue durability and the transport inversion

Outbound messages go through a Redis queue consumed by a worker, which posts to a
workflow engine acting as transport to the SMS gateway.

The architecture here was **inverted mid-project**, and it is the clearest
architectural-judgment story in the system.

**Originally:** the application posted structured event payloads, and the workflow
engine held the message copy — one workflow per event type, twenty-four of them,
each with the text inside it.

**The problems, in the order they were discovered:**

1. Message copy lived outside the repository. It was not reviewed, not
   versioned, not tested, and not diffable. Nine copy defects were found living
   in those workflows.
2. Copy could not be unit-tested. The cost constraint below is testable in code
   and not testable in a GUI.
3. Every event type needed its own workflow, so the transport's complexity grew
   linearly with the domain's vocabulary.
4. Most seriously: **the transport reported success for errored runs.** An errored
   workflow returned an empty `200`. The delivery log was therefore a record of
   *what was submitted*, not of what was sent, and it agreed with itself
   perfectly.

**After:** the application renders the finished message and the workflow engine
interpolates nothing. Twenty-four workflows collapsed to one. Copy became code —
reviewed, versioned, and covered by tests that assert properties the GUI could
never check.

The generalisable principle: **push logic to where it can be tested, and let the
transport be as dumb as possible.** A transport that understands your domain is a
second implementation of your domain, in a language with no tests.

### Cost as a testable invariant

Each message must fit a single SMS segment in the 7-bit GSM alphabet. Exceeding
it splits the message into two, and a split message costs twice as much to send.

So it isn't a style rule, it's a **money rule**, and it is enforced by a test that
fails the build if any renderer produces a message that would split — including
when a variable-length substitution (a name, an amount) pushes it over. Encoding
arithmetic, checked in CI, on every rendered message.

A related detail worth keeping because it is the kind of thing that only bites
once: alphanumeric SMS sender IDs are limited to **11 characters**. A longer value
does not fail at send time — it fails at *registration*, weeks earlier, with the
provider. Reading the constraint before submitting the paperwork saved a
multi-week round trip.

### No retry loop

Failed messages are not retried in a general loop.

For a notification whose content is time-sensitive — "your cycle ends in three
days" — a retry that succeeds four hours later delivers a message that is now
wrong. And an unbounded retry against a rate-limited gateway makes the rate
limiting worse while producing an ever-growing backlog of increasingly stale
messages.

Rate limiting is applied in the worker, before the send, rather than reacting to
the gateway's rejection afterwards. Two watchdog-class events bypass the queue
entirely, on the grounds that a notification path with a queue in it is a poor
place to put the alert about the queue being broken.

---

**Next:** [5. Observability and operations](05-observability-and-operations.md) ·
**Back:** [3. Network integration](03-network-integration.md)
