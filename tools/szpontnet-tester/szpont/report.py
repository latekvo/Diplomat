"""A tiny conformance-reporting framework: checks, results, spec references.

Each test case reports one or more **checks**, every check tagged with the spec
section it enforces (e.g. ``02-discovery#dial-rule``) and a MUST/SHOULD level.
The runner prints a per-check pass/fail/skip line and an aggregate verdict; a
SHOULD failure warns but does not fail the run, a MUST failure does. The exit
code is non-zero iff any MUST check failed — so a second implementation can gate
CI on ``szpontnet-tester``.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field

PASS, FAIL, SKIP, WARN = "PASS", "FAIL", "SKIP", "WARN"

_COLOR = {
    PASS: "\033[32m", FAIL: "\033[31m", SKIP: "\033[90m", WARN: "\033[33m",
}
_RESET = "\033[0m"


@dataclass
class Check:
    name: str
    result: str
    level: str          # "MUST" | "SHOULD" | "MAY"
    spec: str           # spec section reference
    detail: str = ""


@dataclass
class CaseResult:
    id: str
    title: str
    checks: list[Check] = field(default_factory=list)
    error: str | None = None   # an unexpected exception aborted the case
    skipped: str | None = None  # a reason the whole case was skipped


class Reporter:
    def __init__(self, verbose: bool = False, color: bool | None = None) -> None:
        self.verbose = verbose
        self.color = sys.stdout.isatty() if color is None else color
        self.cases: list[CaseResult] = []
        self._current: CaseResult | None = None
        self._t0 = 0.0

    # MARK: - case + check recording

    def begin_case(self, case_id: str, title: str) -> CaseResult:
        self._current = CaseResult(case_id, title)
        self.cases.append(self._current)
        self._t0 = time.monotonic()
        self._line(f"\n▶ {case_id}  {title}", bold=True)
        return self._current

    def check(self, name: str, ok: bool, level: str, spec: str, detail: str = "") -> bool:
        result = PASS if ok else (FAIL if level == "MUST" else WARN)
        self._record(Check(name, result, level, spec, detail))
        return ok

    def passed(self, name: str, level: str, spec: str, detail: str = "") -> None:
        self._record(Check(name, PASS, level, spec, detail))

    def failed(self, name: str, level: str, spec: str, detail: str = "") -> None:
        self._record(Check(name, FAIL if level == "MUST" else WARN, level, spec, detail))

    def skip(self, name: str, spec: str, detail: str = "") -> None:
        self._record(Check(name, SKIP, "MAY", spec, detail))

    def skip_case(self, reason: str) -> None:
        if self._current:
            self._current.skipped = reason
        self._line(f"  ⊘ skipped: {reason}", color=SKIP)

    def case_error(self, exc: str) -> None:
        if self._current:
            self._current.error = exc
        self._line(f"  ✗ error: {exc}", color=FAIL)

    def _record(self, check: Check) -> None:
        if self._current:
            self._current.checks.append(check)
        icon = {PASS: "✓", FAIL: "✗", SKIP: "⊘", WARN: "!"}[check.result]
        msg = f"  {icon} [{check.result}] {check.name}  ({check.level} {check.spec})"
        if check.detail and (self.verbose or check.result in (FAIL, WARN)):
            msg += f"\n      → {check.detail}"
        self._line(msg, color=check.result)

    # MARK: - output

    def _line(self, text: str, color: str | None = None, bold: bool = False) -> None:
        if self.color and color in _COLOR:
            text = _COLOR[color] + text + _RESET
        elif self.color and bold:
            text = "\033[1m" + text + _RESET
        print(text, flush=True)

    # MARK: - summary + exit

    def summary(self) -> int:
        must_fail = should_fail = passed = skipped = 0
        # A case that RAISED before recording its checks (case_error) contributes 0 FAILs,
        # so gating the verdict on `must_fail` alone would certify a candidate that WEDGED a
        # case as CONFORMANT. A case that could not run is a candidate failure, not a pass —
        # it blocks conformance exactly like a MUST failure.
        errored = [case for case in self.cases if case.error]
        for case in self.cases:
            for c in case.checks:
                if c.result == PASS:
                    passed += 1
                elif c.result == SKIP:
                    skipped += 1
                elif c.result == FAIL:
                    must_fail += 1
                elif c.result == WARN:
                    should_fail += 1

        self._line("\n" + "═" * 64, bold=True)
        self._line("SzpontNet conformance summary", bold=True)
        self._line("═" * 64, bold=True)
        self._line(f"  passed     : {passed}", color=PASS)
        self._line(f"  MUST fails : {must_fail}", color=FAIL if must_fail else None)
        self._line(f"  SHOULD warn: {should_fail}", color=WARN if should_fail else None)
        self._line(f"  errored    : {len(errored)}", color=FAIL if errored else None)
        self._line(f"  skipped    : {skipped}", color=SKIP)

        if must_fail:
            failing = [
                f"{case.id}: {c.name} ({c.spec})"
                for case in self.cases for c in case.checks if c.result == FAIL
            ]
            self._line("\nMUST failures (block conformance):", color=FAIL)
            for f in failing:
                self._line(f"  - {f}", color=FAIL)
        if errored:
            self._line("\nErrored cases (block conformance — a case that could not run "
                       "is a candidate failure, not a pass):", color=FAIL)
            for case in errored:
                self._line(f"  - {case.id}: {case.error}", color=FAIL)

        blocked = bool(must_fail) or bool(errored)
        verdict = "NON-CONFORMANT" if blocked else "CONFORMANT (v1)"
        self._line(f"\nVerdict: {verdict}", color=FAIL if blocked else PASS, bold=True)
        return 1 if blocked else 0
