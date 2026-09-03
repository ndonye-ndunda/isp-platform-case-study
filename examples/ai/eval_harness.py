"""Evaluation harness for a grounded, tool-using operations assistant.

Sanitized illustrative example — not production source. Nothing like this is
deployed; it is the designed evaluation approach from docs/06.

WHY THESE FOUR DIMENSIONS. They come from a real acceptance run whose answer
was *correct and still defective*. The model chose the right tool, wrote valid
LogQL unaided, and reported real data -- and then:

    * dropped 6 of 10 matching log groups              -> COMPLETENESS
    * mislabelled the timezone of its own results      -> GROUNDING
    * omitted the query it had run                     -> CITATION
    * did not mention that a result limit was hit      -> CALIBRATION
    * offered to create alerts, which it cannot do     -> REFUSAL

A correctness-only eval scores that run as a pass. That is the argument for
scoring the other dimensions separately: the most likely failure of an
operations assistant is not a wrong answer, it is a confidently INCOMPLETE one,
and incompleteness is invisible unless something independently establishes what
the full answer was.

Hence `expected_groups` below is produced by running the ground-truth query
directly, outside the model, and diffing.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "Dimension",
    "EvalCase",
    "EvalReport",
    "ModelAnswer",
    "score_case",
    "score_suite",
]


class Dimension(StrEnum):
    GROUNDING = "grounding"
    COMPLETENESS = "completeness"
    CALIBRATION = "calibration"
    REFUSAL = "refusal"


@dataclass(frozen=True, slots=True)
class EvalCase:
    """One question with independently-established ground truth."""

    case_id: str
    question: str
    # Established by running the query outside the model. This is the only
    # reliable basis for a completeness score -- asking the model whether it
    # was complete is asking the defect to grade itself.
    expected_groups: frozenset[str]
    # True when the correct behaviour is to decline. An assistant that offers
    # capabilities it lacks makes promises on the system's behalf.
    expect_refusal: bool = False
    # True when the ground-truth query hit a result limit, so a well-calibrated
    # answer must say the result set was truncated.
    result_truncated: bool = False


@dataclass(frozen=True, slots=True)
class ModelAnswer:
    """What came back. `tool_calls` is the audit trail, not the model's claim."""

    text: str
    tool_calls: Sequence[tuple[str, str]] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class EvalReport:
    case_id: str
    scores: dict[Dimension, bool]
    notes: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return all(self.scores.values())


_TRUNCATION_HINTS = re.compile(
    r"\b(truncated|limit|capped|first \d+|at least|more than)\b", re.IGNORECASE
)
_REFUSAL_HINTS = re.compile(
    r"\b(cannot|can't|unable|not able|do not have|read-only)\b", re.IGNORECASE
)


def score_case(case: EvalCase, answer: ModelAnswer) -> EvalReport:
    """Score one answer across the four dimensions.

    Every check here is mechanical and cheap by design. An eval you have to
    hand-grade is an eval you run once, and an eval you run once tells you
    nothing about regression -- which is the only thing evals are actually for.
    """
    scores: dict[Dimension, bool] = {}
    notes: list[str] = []

    # --- REFUSAL ------------------------------------------------------------
    if case.expect_refusal:
        refused = bool(_REFUSAL_HINTS.search(answer.text))
        scores[Dimension.REFUSAL] = refused and not answer.tool_calls
        if not refused:
            notes.append("expected an explicit refusal; none found")
        if answer.tool_calls:
            notes.append("called tools on a question it should have declined")
        # The remaining dimensions are not meaningful for a refusal case.
        scores[Dimension.GROUNDING] = True
        scores[Dimension.COMPLETENESS] = True
        scores[Dimension.CALIBRATION] = True
        return EvalReport(case.case_id, scores, tuple(notes))

    # --- GROUNDING ----------------------------------------------------------
    # An answer must come from a tool call, and must cite it. "Emitting a tool
    # call" is not the same as "citing the query that produced the answer" --
    # the second is what makes the claim checkable by a human.
    made_call = bool(answer.tool_calls)
    cited = any(query[:24] in answer.text for _, query in answer.tool_calls)
    scores[Dimension.GROUNDING] = made_call and cited
    if not made_call:
        # This specific failure is also a symptom worth alerting on in
        # production: a model that stops calling tools may have had its schemas
        # silently truncated out of context (docs/06).
        notes.append("no tool call: answer was ungrounded")
    elif not cited:
        notes.append("tool was called but the query was not cited")

    # --- COMPLETENESS -------------------------------------------------------
    # Search the PROSE, not the citation. A cited query such as
    # {job=~"proxy|system"} contains group names the answer never actually
    # reported on, and counting those as "found" would credit the model for
    # the exact omission this dimension exists to catch.
    prose = answer.text
    for _, query in answer.tool_calls:
        prose = prose.replace(query, " ")
    found = {g for g in case.expected_groups if g.lower() in prose.lower()}
    missing = case.expected_groups - found
    scores[Dimension.COMPLETENESS] = not missing
    if missing:
        notes.append(
            f"omitted {len(missing)} of {len(case.expected_groups)}: " f"{sorted(missing)}"
        )

    # --- CALIBRATION --------------------------------------------------------
    # "No errors found" and "no errors in the first 10 of an unknown number"
    # are different answers. Rendering the second as the first is the most
    # dangerous thing an operations assistant can do.
    if case.result_truncated:
        acknowledged = bool(_TRUNCATION_HINTS.search(answer.text))
        scores[Dimension.CALIBRATION] = acknowledged
        if not acknowledged:
            notes.append("result set was truncated but the answer did not say so")
    else:
        scores[Dimension.CALIBRATION] = True

    return EvalReport(case.case_id, scores, tuple(notes))


def score_suite(
    pairs: Sequence[tuple[EvalCase, ModelAnswer]],
) -> tuple[Sequence[EvalReport], dict[Dimension, float]]:
    """Score a suite and return per-dimension pass rates.

    Per-dimension rates, not one aggregate. A single number hides exactly the
    thing that matters: a suite at 90% grounding and 40% completeness needs
    entirely different work from the reverse, and averaging them to 65% tells
    you to do neither.
    """
    reports = [score_case(case, answer) for case, answer in pairs]
    rates: dict[Dimension, float] = {}
    for dim in Dimension:
        relevant = [r.scores[dim] for r in reports if dim in r.scores]
        rates[dim] = (sum(relevant) / len(relevant)) if relevant else 0.0
    return reports, rates
