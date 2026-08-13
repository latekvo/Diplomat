"""Every shared-core type both front-ends are supposed to run on, is run on by both.

The monorepo's whole claim is that the two applets are front-ends over one core. What
that claim cannot survive is a core type with a Python twin that the macOS app never
references: the Linux side gets the shared behaviour, the macOS side keeps whatever it
had, and every parity test still passes — because they compare `DiplomatCore` against
its Python twin, and the macOS app is in neither half.

That is not hypothetical. `AgentState.swift` and `AgentRegistry.swift` lived in the core
for weeks with a Python twin apiece and not one reference from
`packages/diplomat-platform/macos`, while that front-end went on tracking its runs in
UserDefaults. The suite was green throughout.

So: a core file with a Python twin must have at least one of its public types named in
the macOS sources. Deliberately a grep and not a build — the drift is exactly the shape
a grep can see, and this job has the whole checkout but no Swift toolchain.
"""

from __future__ import annotations

import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_PACKAGES = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
CORE = os.path.join(_PACKAGES, "diplomat-core", "Sources", "DiplomatCore")
# Both halves of the Python side: the shared runtime holds most of the twins, and the
# Linux applet keeps the ones that are its own (``audit``). A twin in either is a twin.
PY = [
    os.path.join(_PACKAGES, "diplomat-runtime", "diplomat_runtime"),
    os.path.join(_PACKAGES, "diplomat-platform", "linux", "diplomat_app"),
]
MACOS = os.path.join(_PACKAGES, "diplomat-platform", "macos", "Sources")

_DECL = re.compile(
    r"^public (?:struct|enum|final class|class|actor|protocol) ([A-Za-z_][A-Za-z0-9_]*)",
    re.M,
)

#: Core types the macOS app uses only THROUGH another one, so their own name never
#: appears in its sources.
#:
#: Each entry states where that use is, and adding one is the claim that the type is
#: genuinely consumed — which is exactly what a reviewer has to check, because waving a
#: type through here is also how the drift this test exists to catch would get in.
REACHED_INDIRECTLY = {
    "PRRef": "ReviewConfig.prRef — the wizards read .number / .repoMismatch off it",
}


#: Twins whose two halves are not named the same thing, each with the source line that
#: says so — the pairing below is by filename, so these would otherwise go unchecked.
RENAMED_TWINS = {
    "AgentActivity": "apiwatch",     # apiwatch.py:2
    "ApiErrorMatch": "apiwatch",     # apiwatch.py:2
    "AgentTasks": "autofix",         # autofix.py:666 "mirrors AgentTasks.swift"
    "ReviewReconcile": "autofix",    # autofix.py:80
    "AuditCategory": "core",         # core.py:117 "Mirrors …/AuditCategory.swift"
    "AgentRunner": "runner",         # AgentRunner.swift:3
}

#: Swift comments and string literals, stripped before the search below: a type is adopted
#: by being USED, and one that survives only as a mention in a doc comment is exactly the
#: unadopted type this file exists to find.
_NONCODE = re.compile(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\\n])*"', re.S)


def _macos_sources() -> str:
    out = []
    for root, _dirs, files in os.walk(MACOS):
        for name in files:
            if name.endswith(".swift"):
                with open(os.path.join(root, name), encoding="utf-8") as fh:
                    out.append(_NONCODE.sub(" ", fh.read()))
    return "\n".join(out)


def _twin_module(stem: str) -> bool:
    names = {stem.lower(), RENAMED_TWINS.get(stem, "")}
    return any(os.path.exists(os.path.join(d, f"{n}.py"))
               for d in PY for n in names if n)


def _twinned() -> list[tuple[str, list[str]]]:
    """Core files that have a Python twin module, and the public types they declare."""
    pairs = []
    for name in sorted(os.listdir(CORE)):
        if not name.endswith(".swift"):
            continue
        stem = name[: -len(".swift")]
        if not _twin_module(stem):
            continue
        with open(os.path.join(CORE, name), encoding="utf-8") as fh:
            pairs.append((stem, _DECL.findall(fh.read())))
    return pairs


def test_the_pairing_finds_the_shared_types():
    """The check is a grep, so it is worth proving the grep finds anything at all: a
    renamed directory or a tightened declaration regex would otherwise turn the test
    below into a loop over nothing that passes forever."""
    pairs = _twinned()
    assert len(pairs) >= 18, pairs
    assert ("AgentState", ["Observation", "AgentState"]) in pairs
    # One of each pairing rule, so losing either is a failure and not a quieter check.
    assert ("AgentRunner", ["AgentRunner"]) in pairs
    assert all(types for _stem, types in pairs)


def test_every_renamed_twin_names_a_module_that_exists():
    """An alias whose Python side has moved or been renamed silently un-pairs its core
    file — the same hole as never having listed it, and one nothing else here reports."""
    missing = sorted(stem for stem, module in RENAMED_TWINS.items()
                     if not any(os.path.exists(os.path.join(d, f"{module}.py")) for d in PY))
    assert not missing, f"renamed twins whose Python module is gone: {missing}"


def test_every_twinned_core_file_is_referenced_by_the_macos_app():
    """A core file with a Python twin that macOS names no type of is a shared behaviour
    only one front-end adopted.

    Per file, not per type: one reference clears the file. A type is the unit a reviewer
    reads, but it is not the unit that drifts - what went unadopted here was a whole
    module, and holding every public type to the rule would fail on the several that
    exist so the Python side can spell an error or a row (``GHError``, ``ReviewThread``)
    and have no business being named in Swift.
    """
    macos = _macos_sources()
    orphans = []
    for stem, types in _twinned():
        named = [t for t in types
                 if re.search(rf"\b{re.escape(t)}\b", macos) or t in REACHED_INDIRECTLY]
        if not named:
            orphans.append(f"{stem}.swift (declares {', '.join(types)})")
    assert not orphans, (
        "these core files have a Python twin but nothing in "
        "packages/diplomat-platform/macos references them — the Linux applet runs the "
        "shared code and the macOS one does not: " + "; ".join(orphans)
    )


def test_the_exemptions_are_types_that_exist():
    """An exemption for a type that has since been renamed or deleted is a hole nothing
    reports: it excuses no drift today and would silently excuse a NEW type that
    happened to take the same name."""
    declared = {t for _stem, types in _twinned() for t in types}
    stale = sorted(set(REACHED_INDIRECTLY) - declared)
    assert not stale, f"exemptions for types no twinned core file declares: {stale}"


def test_no_exemption_is_for_a_type_the_app_names_anyway():
    """The other way an exemption goes stale: the type got adopted directly and the
    waiver stayed. It then excuses nothing today and silently excuses whatever the name
    is next attached to."""
    macos = _macos_sources()
    unneeded = sorted(t for t in REACHED_INDIRECTLY
                      if re.search(rf"\b{re.escape(t)}\b", macos))
    assert not unneeded, f"exempted but named directly in the macOS sources: {unneeded}"
