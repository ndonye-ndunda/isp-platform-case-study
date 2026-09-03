#!/usr/bin/env python3
"""Conventions enforced as a build-failing check.

Sanitized illustrative example — not production source. The production guard
set covers five rules over the real package layout; this is a reimplementation
of the idea over this repository's own files, and CI runs it against them.

WHY THIS EXISTS (docs/07). A convention that is not mechanically enforced
degrades to a preference, and the degradation is invisible until you audit. The
guards below are crude -- they grep. They have false positives, which is what
the escape-hatch comment is for. They are still worth far more than the style
guide they replaced, because they run.

Each rule below is traceable to a defect that actually happened:

  naive-datetime   a cycle ending a day early for anything processed after
                   21:00 local, because a "today" was silently a UTC today
  money-float      int(float("8.20") * 100) == 819, losing a cent
  broad-except     an exception handler that swallowed a failed money credit
                   and let the caller return success

A closing note that is part of the lesson: for a period, none of the production
guards ran anywhere except one developer's machine -- the hook framework was
missing from the dev requirements file, so a fresh checkout silently had no
guards at all. A guard that is not installed is a guard that does not exist.
They are now invoked directly in CI rather than through the hook framework, so
a hook-configuration problem cannot silently skip them.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass

ALLOW = "guard-ok"  # trailing comment to waive a line, deliberately

# SCOPE. Guards run over application code only. Tests are excluded on purpose:
# a test for a guard must contain the construct the guard bans, and a test of a
# display path legitimately formats money as a float. Scoping is not a loophole
# -- it is the difference between a guard people keep and a guard people learn
# to bypass. The production version scopes by package prefix for the same
# reason (billing and worker packages in, tests and migrations out).
EXCLUDE_PREFIXES: tuple[str, ...] = (
    "examples/tests/",
    "tests/",
)

# A naive "now"/"today" in business logic is a bug in any system that does not
# run in UTC: it answers a local-calendar question with a UTC answer, and is
# wrong for part of every day.
NAIVE_TIME = re.compile(r"\b(datetime\.now\(\s*\)|datetime\.utcnow\(|date\.today\()")

# float() applied to anything that looks like money, and the classic
# truncation composition.
#
# The lookahead matters: a leading `[A-Za-z_]` character class would consume
# the very keyword being searched for, so `float(amount_str)` would not
# match. That bug was live in this file until the tests caught it -- which
# is itself the argument for testing your guards.
MONEY_FLOAT = re.compile(
    r"float\(\s*(?=[A-Za-z_])[A-Za-z0-9_.]*"
    r"(?:amount|balance|price|charge|minor|cents|total)",
    re.IGNORECASE,
)
INT_OF_FLOAT = re.compile(r"int\(\s*float\(")

# `except Exception:` with nothing but `pass` on the next line. The specific
# failure was a swallowed exception on a credit path, after which the caller
# returned success -- the "looked like success" shape (docs/07).
BARE_PASS = re.compile(r"except\s+(Exception|BaseException)\s*:")


@dataclass(frozen=True, slots=True)
class Finding:
    path: str
    line_no: int
    rule: str
    text: str

    def render(self) -> str:
        return f"{self.path}:{self.line_no}: [{self.rule}] {self.text.strip()}"


def check_file(path: str, lines: Sequence[str]) -> list[Finding]:
    findings: list[Finding] = []
    in_docstring = False

    for i, raw in enumerate(lines, start=1):
        line = raw.rstrip("\n")

        # Crude triple-quote tracking so prose in docstrings -- which
        # legitimately discusses `datetime.now()` -- does not trip the guards.
        # An ODD count opens or closes a multi-line docstring; an EVEN
        # non-zero count is a self-contained one-line docstring, which must
        # also be skipped. Handling only the odd case leaves every
        # single-line docstring unfiltered -- caught by the tests.
        quotes = line.count('"""') + line.count("'''")
        if quotes % 2 == 1:
            in_docstring = not in_docstring
            continue
        if in_docstring or quotes:
            continue

        stripped = line.lstrip()
        if stripped.startswith("#") or ALLOW in line:
            continue

        if NAIVE_TIME.search(line):
            findings.append(Finding(path, i, "naive-datetime", "timezone-naive now()/today()"))
        if MONEY_FLOAT.search(line) or INT_OF_FLOAT.search(line):
            findings.append(Finding(path, i, "money-float", "float() on a money value"))
        if BARE_PASS.search(line):
            nxt = lines[i].strip() if i < len(lines) else ""
            if nxt == "pass":
                findings.append(Finding(path, i, "swallowed-except", "except ...: pass"))

    return findings


def in_scope(path: str) -> bool:
    normalised = path.removeprefix("./")
    return not normalised.startswith(EXCLUDE_PREFIXES)


def main(argv: Sequence[str]) -> int:
    paths = [p for p in argv if p.endswith(".py") and in_scope(p)]
    if not paths:
        return 0

    findings: list[Finding] = []
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError as exc:  # pragma: no cover
            print(f"{path}: cannot read ({exc})", file=sys.stderr)
            return 2
        findings.extend(check_file(path, lines))

    if not findings:
        return 0

    print(f"{len(findings)} convention violation(s):", file=sys.stderr)
    for f in findings:
        print(f"  {f.render()}", file=sys.stderr)
    print(
        f"\nIf a hit is a genuine false positive, append a '# {ALLOW}' comment "
        "to that line -- deliberately visible in review.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
