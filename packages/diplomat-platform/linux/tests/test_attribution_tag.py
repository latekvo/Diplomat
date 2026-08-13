"""The model named in the attribution tag every posted comment opens with.

Which model a spawn will run on is worked out by ``AgentModel`` in the shared Swift
core, so these drive it the way the applet does — through the real ``diplomat-core``
binary, over a fenced ``$DIPLOMAT_CLAUDE_DIR`` / ``$DIPLOMAT_CONFIG`` (conftest points
both at this test's tmp dir). That is the whole Linux path: nothing here re-implements
the lookup, it asserts the one implementation is reached and honours the same two
override hooks the Python side documents.

The stakes are what makes this worth a file: the tag goes out on public comments and
reviews, so a wrong answer attributes a review to a model that never ran it.

Skipped without DIPLOMAT_CORE_BIN, same as the golden-prompt tests.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from diplomat_app.audit import AuditConfig
from diplomat_runtime import appconfig, runner, usagescan
from diplomat_runtime.review import ReviewConfig

pytestmark = pytest.mark.skipif(
    not os.environ.get("DIPLOMAT_CORE_BIN"),
    reason="DIPLOMAT_CORE_BIN not set (build it with packages/diplomat-platform/linux/install/build-core.sh)",
)

#: What the tag renders as with no model behind it — the prefix every Diplomat comment
#: has always opened with, and the one the golden prompts still hold.
PLAIN = "`\\[[Diplomat](https://github.com/latekvo/Diplomat)\\]: `"


def tag_prefix(model: str) -> str:
    return PLAIN.replace(")\\]", f"), {model}\\]")


def claude_transcript(model: str) -> None:
    """A Claude Code transcript recording one turn on ``model``, in the fenced
    ``~/.claude`` — read through :func:`usagescan.claude_dir`, so this test and the
    applet's own token scan resolve the directory the same way."""
    sessions = Path(usagescan.claude_dir()) / "projects" / "-home-me-repo"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "session.jsonl").write_text(
        '{"message":{"model":"%s"}}\n' % model, encoding="utf-8"
    )


def review_prompt() -> str:
    return ReviewConfig(depth="max", me="testuser").build_prompt()


def test_a_machine_that_says_nothing_tags_exactly_as_it_always_has():
    """The model is an addition to the tag, not a rewrite of it. With no runner state
    to read, the prefix has to come out byte-identical to the one every golden prompt
    holds — otherwise every install that cannot be asked gets a broken tag instead of
    the old one."""
    prompt = review_prompt()
    assert PLAIN in prompt
    assert "[Diplomat]: <your text>" in prompt


def test_the_tag_names_the_model_claude_code_last_ran():
    """Claude Code is handed no model by Diplomat — it is started through the user's own
    alias and picks its own — so what it last actually ran is the only thing that
    accounts for a `--model` in that alias or an in-session `/model`."""
    claude_transcript("claude-opus-5")
    prompt = review_prompt()
    assert tag_prefix("Opus 5") in prompt
    assert "[Diplomat, Opus 5]: <your text>" in prompt


def test_a_synthetic_turn_does_not_blank_the_tag():
    """Claude Code writes ``"model":"<synthetic>"`` for turns no model produced. Taking
    the last value in the file at face value would drop the model from the tag whenever
    a session happened to end on one."""
    sessions = Path(usagescan.claude_dir()) / "projects" / "-home-me-repo"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "session.jsonl").write_text(
        '{"message":{"model":"claude-opus-5"}}\n{"message":{"model":"<synthetic>"}}\n',
        encoding="utf-8",
    )
    assert tag_prefix("Opus 5") in review_prompt()


def test_the_tag_names_the_model_a_foreign_runner_is_pinned_to():
    """OpenCode and Hermes DO take the model Settings pins them to — the spawn passes it
    on the command line — so that is what the run is on and what the tag must say."""
    appconfig.set_value(appconfig.AGENT_RUNNER, runner.OPENCODE)
    appconfig.set_value(appconfig.AGENT_MODEL, "openrouter/moonshotai/kimi-k3")
    assert tag_prefix("Kimi K3") in review_prompt()


def test_an_unpinned_foreign_runner_claims_no_model():
    """Blank means OpenCode uses the model its own picker remembers. Diplomat cannot
    name that, and guessing would attribute a review to a model nobody selected."""
    appconfig.set_value(appconfig.AGENT_RUNNER, runner.OPENCODE)
    claude_transcript("claude-opus-5")
    assert PLAIN in review_prompt()


def test_a_pin_left_over_from_another_runner_is_not_attributed_to_claude_code():
    """``AgentRunner.claude``'s command carries no model flag, so the pinned field is
    inert under Claude Code. Reading it anyway would tag a Claude run with whichever
    model the user last pointed OpenCode at."""
    appconfig.set_value(appconfig.AGENT_MODEL, "openrouter/moonshotai/kimi-k3")
    claude_transcript("claude-opus-5")
    assert tag_prefix("Opus 5") in review_prompt()


def test_the_audit_prompt_carries_the_same_tag():
    """The audit's comments and PR descriptions go out under the same attribution as a
    review's, from a second copy of the block in a second asset — so the model has to
    reach both, not just the one the review path exercises."""
    claude_transcript("claude-opus-5")
    prompt = AuditConfig(open_prs=True).build_prompt()
    assert tag_prefix("Opus 5") in prompt
    assert "[Diplomat, Opus 5]: <your text>" in prompt
