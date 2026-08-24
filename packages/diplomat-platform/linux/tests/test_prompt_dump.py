"""`DIPLOMAT_PRINT_PROMPT` - the headless dump behind every wizard's prompt.

It is the only way to see what an agent would actually be handed without spawning
one, so it has to cover all four wizards and keep the section markers a reader
(and a diff against the macOS twin) relies on. The four dumps share one body,
`selftest._print_prompt_dump`; these pin the routing into it and what it emits.

Skipped without DIPLOMAT_CORE_BIN: building a prompt shells out to the
diplomat-core CLI, same as the golden-prompt tests.
"""

from __future__ import annotations

import os

import pytest

from diplomat_app import selftest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DIPLOMAT_CORE_BIN"),
    reason="DIPLOMAT_CORE_BIN not set (build it with packages/diplomat-platform/linux/install/build-core.sh)",
)


@pytest.mark.parametrize("mode,header", [
    ("mine", "== ReviewConfig: my PRs · depth="),
    ("user", "== ReviewConfig: someone else's PRs · depth="),
    ("single", "== ReviewConfig: single PR #337 · depth="),
    ("conflicts", "== ConflictConfig: my PRs =="),
    ("conflicts-user", "== ConflictConfig: someone else's PRs =="),
    ("conflicts-single", "== ConflictConfig: single PR #337 =="),
    ("audit", "== AuditConfig: full-repo E2E test · fixIssues=False openPRs=False =="),
    ("audit-issues", "== AuditConfig: full-repo E2E test · fixIssues=True openPRs=False =="),
    ("audit-prs", "== AuditConfig: full-repo E2E test · fixIssues=False openPRs=True =="),
    ("audit-all", "== AuditConfig: full-repo E2E test · fixIssues=True openPRs=True =="),
    ("issues", "== IssueConfig: issue #421, out of a sweep · depth="),
    ("issues-single", "== IssueConfig: issue #421, named by hand · depth="),
])
def test_each_mode_dumps_the_config_it_names(capsys, mode, header):
    """All four wizards are reachable, and each toggle in the mode string reaches
    the config - a mode that silently fell through to the Review dump would still
    print a prompt, just the wrong one."""
    assert selftest.run_print_prompt(mode) == 0
    assert capsys.readouterr().out.startswith(header)


@pytest.mark.parametrize("mode", ["mine", "conflicts", "audit", "issues"])
def test_every_dump_shows_the_prompt_and_the_command(capsys, mode):
    """Both sections, in order, with a non-empty prompt between them. The shell
    command is the half that proves the prompt would actually be handed to an
    agent, and it is the easiest half to lose in a refactor of the shared body."""
    assert selftest.run_print_prompt(mode) == 0
    out = capsys.readouterr().out
    assert "----- PROMPT -----" in out
    assert "----- SHELL COMMAND -----" in out
    prompt = out.split("----- PROMPT -----", 1)[1].split("----- SHELL COMMAND -----")[0]
    assert prompt.strip(), "the PROMPT section is empty"
    assert "claude " in out.split("----- SHELL COMMAND -----", 1)[1]


def test_an_unknown_mode_falls_back_to_the_review_dump(capsys):
    """The env var is typed by hand; an unrecognised value prints something useful
    rather than failing."""
    assert selftest.run_print_prompt("wat") == 0
    assert capsys.readouterr().out.startswith("== ReviewConfig: my PRs")
