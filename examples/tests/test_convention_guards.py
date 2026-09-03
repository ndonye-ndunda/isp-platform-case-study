"""The guards are crude greps. These tests pin down what they do and don't catch.

Sanitized illustrative example — not production source.
"""

from __future__ import annotations

from examples.guards.convention_guards import check_file


def run(src: str) -> list[str]:
    return [f.rule for f in check_file("sample.py", src.splitlines(keepends=True))]


def test_catches_naive_datetime() -> None:
    assert run("stamp = datetime.now()\n") == ["naive-datetime"]
    assert run("today = date.today()\n") == ["naive-datetime"]
    assert run("stamp = datetime.utcnow()\n") == ["naive-datetime"]


def test_allows_timezone_aware_time() -> None:
    assert run("stamp = datetime.now(UTC)\n") == []
    assert run("stamp = datetime.now(tz=EAT)\n") == []


def test_catches_float_on_money() -> None:
    assert run("value = float(amount_str)\n") == ["money-float"]
    assert run("cents = int(float(raw_amount))\n") == ["money-float"]
    assert run("x = float(row.balance_minor)\n") == ["money-float"]


def test_allows_float_on_non_money() -> None:
    assert run("ratio = float(packet_loss_percent)\n") == []
    assert run("secs = float(timeout_setting)\n") == []


def test_catches_swallowed_exception() -> None:
    src = "try:\n    credit()\nexcept Exception:\n    pass\n"
    assert run(src) == ["swallowed-except"]


def test_allows_handled_exception() -> None:
    src = "try:\n    credit()\nexcept Exception:\n    log.exception('failed')\n"
    assert run(src) == []


def test_comments_are_ignored() -> None:
    assert run("# datetime.now() is banned here\n") == []


def test_docstrings_are_ignored() -> None:
    """Prose legitimately discusses the banned constructs.

    A guard that trips on its own documentation trains people to disable it.
    """
    src = '"""Do not use datetime.now() in billing code."""\n'
    assert run(src) == []


def test_multiline_docstring_body_is_ignored() -> None:
    src = '"""\nAvoid datetime.now() and float(amount).\n"""\nx = 1\n'
    assert run(src) == []


def test_escape_hatch_waives_a_line() -> None:
    """False positives get a visible, reviewable waiver -- not a disabled guard."""
    src = "legacy = float(amount_str)  # guard-ok: display path only\n"
    assert run(src) == []


def test_multiple_findings_in_one_file() -> None:
    src = "a = datetime.now()\nb = float(total_amount)\n"
    assert sorted(run(src)) == ["money-float", "naive-datetime"]


def test_scope_excludes_test_files() -> None:
    """Guards run over application code only.

    A test for a guard must contain the construct the guard bans. Without a
    scope rule the guard fails on its own fixtures, and the natural response is
    to disable it -- so scoping is what keeps the guard alive.
    """
    from examples.guards.convention_guards import in_scope

    assert in_scope("examples/domain/money.py")
    assert in_scope("./examples/ai/eval_harness.py")
    assert not in_scope("examples/tests/test_money.py")
    assert not in_scope("./examples/tests/test_money.py")
