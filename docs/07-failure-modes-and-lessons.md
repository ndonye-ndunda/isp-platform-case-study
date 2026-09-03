# 7. Failure modes and lessons

> Engineering lessons from operating this system. Incidents are described as
> failure *classes* with their mechanisms and mitigations; no customer, site,
> date-specific or commercially sensitive detail appears here — see
> [SECURITY.md](../SECURITY.md).

## One failure shape, twenty-one times

A review pass over the notification subsystem produced twenty-one findings. In
retrospect they were not twenty-one problems. They were one problem with
twenty-one instances:

> **Every broken path returned something that looked like success.**

The evidence, each independently discovered:

| Component | Reported | Reality |
|---|---|---|
| Workflow engine | Empty `200` | The run had errored |
| SMS gateway | `201 Created` | Request *accepted*, not delivered |
| Gateway under rate limit | `200` | Message dropped |
| Payment webhook, on a parse failure | `200` | Money received, never credited |
| Backup pipeline | exit `0` | Valid, empty archive |
| Alert rules over an absent series | no alert | Rules could never fire |
| Rule linter with a lint error | exit `0` | The error was real |
| Model with a truncated context | fluent answer | Never saw its tools |
| Unimplemented stub | audit `SUCCESS` | Nothing happened |

So the delivery log agreed with itself perfectly and disagreed with reality. Every
row said sent. Nothing had been sent. The system was not *unaware* of a problem —
it was actively, consistently reporting the opposite of the truth, through the
exact channel built to tell the truth.

### Why this happens so reliably

It is not carelessness. It is the interaction of three normal, defensible
engineering practices:

1. **HTTP status codes describe transport, not semantics.** `200` means "I
   received and processed your request", where "processed" is the *server's*
   definition. A workflow engine that ran a workflow which failed has, from its
   perspective, correctly processed your request.
2. **Asynchronous systems must acknowledge before completing.** `201 Created` on a
   message-send is honest: a queue entry was created. Delivery happens later,
   through a channel you have to separately consume.
3. **Absence is not an error.** Empty query results, truncated contexts, absent
   metric series, and zero-byte archives are all *valid* states. Treating them as
   errors would break far more than it fixed.

Each is correct locally. Composed, they produce a system where the default answer
to "did that work?" is yes.

### What I do differently now

**Never accept the transport's word for the domain outcome.** `200` is evidence
the request arrived. It is not evidence of what you asked for. Where the two are
distinguishable, parse the body and assert on the domain result.

**Distrust "it reached the send node".** That phrasing is quoted in the project's
own contributor notes with a warning attached, because it is *precisely* the
verification that let all of this ship. Reaching the send step and the message
arriving are different claims, and the first one masquerades as the second
because it is so much easier to observe.

**Verify at the far end, or admit you haven't verified.** The only real
confirmation of a delivered message is a delivery receipt or a human saying they
got it. Everything upstream is progress, not proof. If the far-end check doesn't
exist, the honest status is "unverified", not "working".

**Make absence loud.** An absent metric, an empty result set, a zero-length file:
each is a state your system will reach, and each needs an explicit branch that
says so. See [§5](05-observability-and-operations.md).

**Prefer honest failure over quiet success.** Given a choice between a path that
errors visibly and one that degrades silently, take the error every time. A `500`
gets fixed on the day it appears. A silent success gets discovered in a review
pass, months later, having been wrong the entire time.

### Why this is the AI-engineering lesson

The mapping is exact:

| Distributed systems | LLM systems |
|---|---|
| `200` ≠ the operation succeeded | A confident answer ≠ a correct answer |
| Empty result ≠ nothing is wrong | "No issues found" ≠ nothing was checked |
| Reaching the send node ≠ delivered | Emitting a tool call ≠ the right tool with right args |
| Absent series ≠ healthy | Truncated context ≠ the model declining to use tools |
| Stub returning `SUCCESS` | Fluent prose over data never retrieved |

A language model's output is *uniformly fluent*. Correct and incorrect answers are
delivered with identical confidence, in identical prose, at identical length.
That is the same problem as a `200` on a failed workflow, with the discriminating
signal removed entirely.

The engineering response is also the same, which is what makes the experience
transferable rather than merely analogous:

- **Ground claims in retrievable evidence.** Tool results, with citations that
  resolve — the equivalent of parsing the response body instead of reading the
  status code.
- **Restrict capability rather than requesting good behaviour.** Strip the write
  tools. Don't instruct the model not to write. Same reasoning as pinning a
  library's dangerous default instead of remembering not to trigger it.
- **Measure the thing, not the proxy.** Tool-call rate, grounding rate,
  completeness against an independent ground-truth run — not "the demo looked
  good", which is the AI equivalent of "it reached the send node".
- **Alert on silent degradation.** Answers-without-tool-calls climbing is the
  absent-series problem wearing a different hat.

I did not learn this from working on AI systems. I learned it from a workflow
engine that returned empty `200`s and an alert rule that could never fire. The
transfer runs in that direction, and I think it is the more useful direction.

---

## Detection indistinguishable from health

Covered mechanically in [§5](05-observability-and-operations.md); recorded here
as the lesson, because it is the single most expensive thing in this document.

An observability gap allowed a site-health condition to persist undetected: the
alert expressions evaluated an **absent** metric series, and an absent series
produces no alert. The rules were loaded, syntactically valid, and correct. The
exporter that would have produced their input had not been deployed. Every check
a reasonable engineer would run — does the rule load, does the linter pass, is
the expression right — passed.

Three durable conclusions:

1. **Monitoring needs monitoring.** "Is this alert *capable* of firing?" is a
   distinct question from "is this alert correct?", and only the second one is
   answered by review.
2. **Alert rules are code and require tests.** Given a synthetic series, does it
   fire? Given the series absent, does the guard fire? Both, in CI.
3. **A dashboard showing nothing looks like a healthy system.** Empty panels read
   as calm. This is a UI affordance actively working against the operator.

---

## Alerting that depends on what it monitors

Infrastructure alerts were originally routed through the application's own
notification pipeline. The application cannot page you about being down.

Beyond the circularity, it created an unrelated coupling: infra alerting was
blocked on a *customer-messaging* provider approval, so a complete monitoring
stack could notify nobody for reasons that had nothing to do with monitoring.

**The lesson is to classify alerts by origin and give each a path that shares no
components with what it reports on.** Infra alerts now leave through a mail path
that touches no application code. Business notifications keep the rich pipeline,
because they need domain context.

The corollary is that this reasoning does not terminate on its own: whatever
sends the alert is itself a component that can fail silently, so at some point
the chain has to leave the building entirely and something external has to alert
on *silence* rather than on an event.

---

## Deployment lessons

**A pull is not a deploy.** Where containers build from an image with no source
mount, pulling the repository changes nothing until the image is rebuilt and the
containers recreated. This is the trap that produces the sentence *"but I fixed
that"* about a system still running the old behaviour — and it is worst for
changes whose effect is invisible when absent, because nothing about the running
system contradicts you. **What the repository says is true of the repository,
not of what is running.** The only cure is verifying the deployed artifact.

**Configuration is read at container create, not start.** So `restart` doesn't
pick up a changed value and `recreate` does. Generalised: where several services
share a configuration value, changing it is a *set* operation, and a set
operation done by hand is one that can be partially completed. Derive the set
from the configuration and assert afterwards that every member took the change.

**Half-applied credentials fail silently, not loudly.** Where two related values
must change together — an account identifier and its key — a mismatch does not
error. It routes some traffic one way and some the other, with both paths
returning success. Change them together, and prove afterwards which one is
actually in effect.

**Turn the feature on last.** A live-mode flag is set *after* a real end-to-end
delivery has been observed, never in the same change that supplies the
credentials. Otherwise a configuration error and a code error are indistinguishable
at exactly the moment you least want ambiguity.

---

## Tooling and dependency lessons

**Dangerous library defaults deserve mechanical guards.** A RADIUS client library
defaulting to three internal retries silently corrupted retry orchestration — the
caller's timeouts and backoff were computed against a duration three times what it
believed. Found once. Now a build-failing check, because the cost of a grep rule
is far below the cost of rediscovering it.

**Verification steps that cannot fail aren't verification.** The alert-rule linter
exits zero on lint errors unless failures are explicitly made fatal. It was green
while reporting a real problem. Always confirm your checker can actually fail —
break something on purpose and watch it go red.

**Two ways to load the same configuration can produce two different results.**
Where a tool offers both a CLI and a UI path for importing the same file, they
are separate code paths and may not agree — one may register a resource at a
different address than the other, so a documented endpoint returns `404` and no
amount of re-reading the file explains why. Test both, document which one you
rely on, and write down a single command that confirms which happened.

**Import is not necessarily idempotent.** An import that discards the file's own
identifier creates a *new* resource rather than updating the existing one, and
the duplicate may then compete silently for the same address. Counting resources
before and after belongs in the procedure, not in your assumptions.

---

## Practices that paid for themselves

### A decision log with supersession

Every locked decision is a numbered section with context, the decision, and its
reasoning. Reversals are recorded as new sections that explicitly supersede
earlier ones — and **the superseded section is kept**, marked, with its original
reasoning intact.

Keeping them is the valuable part. The most useful question during a design
discussion is "did we consider this and reject it?", and only a log that retains
rejected and reversed reasoning can answer it. A log pruned to only-current
decisions is a specification; a log with its history is an argument you can
re-enter.

The convention that gives it teeth: **a change touching a locked rule must cite
the section number.** If you can't cite one, you have either found a gap (flag
it) or you are drifting (stop). That turns the log from documentation into a
control.

### Conventions enforced by a linter

Guards that fail the build, each traceable to a real defect:

| Guard | Catches |
|---|---|
| Naive `datetime.now()` / `date.today()` in billing code | Timezone bugs in a non-UTC business |
| `float(` near a money identifier; `int(float(...))` | Currency precision loss |
| RADIUS client without pinned retries | A dependency's corrupting default |
| Direct environment reads outside the config module | Configuration that bypasses validation |
| Cron trigger constructed outside the registration wrapper | Missing timezone / heartbeat / log context |

Each is crude. Each greps. Each has false positives with an escape-hatch comment.
They are still worth far more than the documentation that preceded them, because
**a convention that isn't mechanically enforced degrades to a preference**, and
the degradation is invisible until you audit.

The corollary, learned the hard way: for a period, none of these guards ran
anywhere except one developer's machine — the hook framework was missing from the
dev requirements file, so a fresh checkout silently had no guards at all. A guard
that isn't installed is a guard that doesn't exist. They now run in CI, invoked
directly rather than through the hook framework, so a hook-configuration problem
cannot silently skip them.

### A defect register, kept separate from the security register

Two registers, deliberately: correctness defects, and security findings. Different
severities, different audiences, different questions.

The rule that makes the defect register earn its keep: **check it before "fixing"
anything.** More than one apparent bug turned out to be a known, deliberate
behaviour with reasoning attached — the frozen heartbeat row in
[§4](04-distributed-systems-patterns.md) being the clearest case. It looks exactly
like a bug. Tidying it up would have destroyed the only signal a hard crash
produces.

### Honest status reporting

Distinguish **built** from **deployed** from **delivering**, and refuse to
collapse them.

Consider a subsystem whose accurate status is: *code complete, tested, deployed,
pipeline verified end to end — and the externally-dependent final hop not yet
open.* Every clause is true, and only the last one matters to the person the
subsystem exists to serve.

Compressing that to "notifications: done" would be the single most expensive
sentence available, because it retires the item from everybody's attention while
the actual blocker is untouched. The discipline of writing the long version is
what keeps a real dependency visible instead of buried under a green checkmark —
and it is why "done" is not a status worth recording. *Done according to whom,
measured where?* is.

### Verification gates between steps

A working rhythm: after each step, run the probe and *show the output*; don't
advance until it passes. Standalone probe scripts, distinct from the unit suite,
kept as durable regression checks — runnable by hand against real infrastructure
when something looks wrong.

The distinction is useful and I'd keep it: **tests answer "is the logic right?",
probes answer "is the system actually wired up?"** They fail for different
reasons, and a green test suite says nothing whatsoever about the second question.

---

## What I would do differently

Not a retrospective performance. Four things I got wrong.

**Build the delivery-confirmation path before the delivery path.** The
notification system was built, reviewed and deployed before anything could
independently confirm a message arrived. All twenty-one findings above trace to
that ordering. The check that tells you a subsystem works is not a follow-up
task — it is part of the subsystem, and it should be built first, because until it
exists you cannot evaluate anything you build after it.

**Deploy the data source before writing rules over it.** Rules written against
an exporter that is not yet reporting give the *appearance* of coverage with none
of the substance — and the appearance is worse than an acknowledged gap, because
an acknowledged gap gets scheduled and a green dashboard does not.

**Set up CI first, not late.** A guard that runs only on the machine of whoever
wrote it is not a guard, and a guard missing from the dev-dependency file does
not run even there. Both failures are silent, and both leave a window in which
exactly what the guard exists to prevent can land. CI is not a late-project
polish item; it is the mechanism that makes every earlier convention stick, so
it is worth having before the conventions it enforces.

**Treat an unexecuted procedure as a hypothesis.** A documented command that
nobody has run is not a capability — it is a belief about a capability, and the
two are indistinguishable until someone runs it. This applies most sharply to
recovery procedures, which are written when there is no pressure and executed
only when there is nothing but pressure. The general rule I would now apply from
the start: any procedure that matters gets executed once, deliberately, on a
day when it is allowed to fail.

---

**Back:** [6. AI engineering](06-ai-engineering.md) ·
[Start over: 1. System architecture](01-system-architecture.md)
