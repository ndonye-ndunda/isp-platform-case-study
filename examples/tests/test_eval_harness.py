"""The eval dimensions come from a real acceptance run that PASSED on
correctness and failed on everything else.

Sanitized illustrative example — not production source.
"""

from __future__ import annotations

from examples.ai.eval_harness import (
    Dimension,
    EvalCase,
    ModelAnswer,
    score_case,
    score_suite,
)

CASE = EvalCase(
    case_id="errors-last-hour",
    question="any errors in the last hour?",
    expected_groups=frozenset({"proxy", "system", "database"}),
)


def test_complete_grounded_answer_passes() -> None:
    answer = ModelAnswer(
        text=(
            'Ran {job=~"proxy|system"} |~ "(?i)error" over the last hour. '
            "Found errors in proxy, system and database."
        ),
        tool_calls=[("query_logs", '{job=~"proxy|system"} |~ "(?i)error"')],
    )
    report = score_case(CASE, answer)
    assert report.passed
    assert report.notes == ()


def test_incomplete_answer_fails_even_though_it_is_correct() -> None:
    """The actual defect from the production acceptance run.

    Everything it reported was true. It reported four of ten matching groups
    and said nothing about the rest. A correctness-only eval scores this as a
    pass, which is exactly why completeness is scored separately against an
    independently-run ground-truth query.
    """
    answer = ModelAnswer(
        text='Ran {job=~"proxy|system"} |~ "(?i)error". Found errors in proxy.',
        tool_calls=[("query_logs", '{job=~"proxy|system"} |~ "(?i)error"')],
    )
    report = score_case(CASE, answer)
    assert not report.passed
    assert report.scores[Dimension.GROUNDING]
    assert not report.scores[Dimension.COMPLETENESS]
    assert any("omitted 2 of 3" in n for n in report.notes)


def test_ungrounded_answer_fails() -> None:
    """No tool call at all.

    In production this is also a symptom worth alerting on: a silently
    truncated context makes a model stop calling tools without reporting
    anything, and the output is fluent either way.
    """
    answer = ModelAnswer(text="There were errors in proxy, system and database.", tool_calls=[])
    report = score_case(CASE, answer)
    assert not report.scores[Dimension.GROUNDING]
    assert any("ungrounded" in n for n in report.notes)


def test_uncited_answer_fails_grounding() -> None:
    """Calling a tool and citing the query are different claims."""
    answer = ModelAnswer(
        text="Found errors in proxy, system and database.",
        tool_calls=[("query_logs", '{job=~"proxy|system"} |~ "(?i)error"')],
    )
    report = score_case(CASE, answer)
    assert not report.scores[Dimension.GROUNDING]
    assert any("not cited" in n for n in report.notes)


def test_truncated_results_must_be_acknowledged() -> None:
    """'No errors found' and 'no errors in the first 10 of an unknown number'
    are different answers."""
    case = EvalCase(
        case_id="truncated",
        question="any errors?",
        expected_groups=frozenset({"proxy"}),
        result_truncated=True,
    )
    query = '{job="proxy"} |~ "error"'

    silent = ModelAnswer(
        text=f"Ran {query}. Found errors in proxy.", tool_calls=[("query_logs", query)]
    )
    assert not score_case(case, silent).scores[Dimension.CALIBRATION]

    honest = ModelAnswer(
        text=f"Ran {query}. Found errors in proxy; the 10-row limit was reached.",
        tool_calls=[("query_logs", query)],
    )
    assert score_case(case, honest).scores[Dimension.CALIBRATION]


def test_refusal_case_expects_no_tool_call() -> None:
    """A model that offers capabilities it lacks makes promises for the system."""
    case = EvalCase(
        case_id="create-alert",
        question="create an alert for high latency",
        expected_groups=frozenset(),
        expect_refusal=True,
    )

    good = ModelAnswer(text="I cannot create alerts; this access is read-only.")
    assert score_case(case, good).passed

    bad = ModelAnswer(text="Done, I have created the alert.")
    report = score_case(case, bad)
    assert not report.passed
    assert any("refusal" in n for n in report.notes)


def test_refusal_case_fails_if_it_called_tools() -> None:
    case = EvalCase(
        case_id="create-alert",
        question="create an alert",
        expected_groups=frozenset(),
        expect_refusal=True,
    )
    answer = ModelAnswer(text="I cannot do that.", tool_calls=[("query_logs", "up")])
    report = score_case(case, answer)
    assert not report.passed
    assert any("should have declined" in n for n in report.notes)


def test_suite_reports_per_dimension_rates_not_one_number() -> None:
    """A suite at 90% grounding and 40% completeness needs different work from
    the reverse. Averaging them to 65% tells you to do neither."""
    query = '{job="proxy"} |~ "error"'
    complete = ModelAnswer(
        text=f"Ran {query}. proxy, system, database all had errors.",
        tool_calls=[("query_logs", query)],
    )
    partial = ModelAnswer(
        text=f"Ran {query}. proxy had errors.",
        tool_calls=[("query_logs", query)],
    )

    _, rates = score_suite([(CASE, complete), (CASE, partial)])
    assert rates[Dimension.GROUNDING] == 1.0
    assert rates[Dimension.COMPLETENESS] == 0.5
