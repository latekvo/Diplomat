"""Regression tests for the shared-core logic the Linux UI renders.

Pure, offline — no gh, no display. Run with: ``python -m pytest linux/tests``
(or ``python linux/tests/test_logic.py`` for a dependency-free smoke run).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from argent_utils import review  # noqa: E402
from argent_utils.models import Filters, OpenIssue, OpenPR, ReviewThread  # noqa: E402
from argent_utils.prref import parse_pr_ref  # noqa: E402
from argent_utils.prtarget import PRTarget  # noqa: E402
from argent_utils.store import Store  # noqa: E402

NOW = datetime.now(timezone.utc)
OLD = NOW - timedelta(days=15)


def _prs() -> list[OpenPR]:
    return [
        OpenPR(101, "skill", "u/101", False, "alice", NOW, None,
               ["skills/foo/SKILL.md"], None, []),
        OpenPR(102, "installer", "u/102", True, "bob", NOW, None,
               ["packages/argent-installer/x.ts"], None, []),
        OpenPR(103, "stale", "u/103", False, "carol", OLD, OLD, ["src/x.ts"], None, []),
        OpenPR(104, "approved", "u/104", False, "latekvo", NOW, None, ["a.ts"],
               "APPROVED", []),
        OpenPR(105, "unaddressed", "u/105", False, "latekvo", NOW, None, ["b.ts"],
               None, [ReviewThread(False, True, "reviewer")]),
    ]


def _issues() -> list[OpenIssue]:
    return [
        OpenIssue(201, "ext", "i/201", "ext", "NONE", OLD, OLD, 0, [], ["bug"], False),
        OpenIssue(202, "member", "i/202", "dev", "MEMBER", NOW, NOW, 1, [], [], True),
    ]


def test_filters_select_expected_numbers():
    prs, issues = _prs(), _issues()
    assert [p.number for p in Filters.skill_prs(prs)] == [101]
    assert [p.number for p in Filters.installer_prs(prs)] == [102]
    assert [p.number for p in Filters.stale_ready_prs(prs)] == [103]
    assert [i.number for i in Filters.unaddressed_external_issues(issues)] == [201]
    assert [p.number for p in Filters.my_approved_prs(prs, "latekvo")] == [104]
    assert [p.number for p in Filters.my_unaddressed_review_prs(prs, "latekvo")] == [105]


def test_my_tools_empty_without_identity():
    prs = _prs()
    assert Filters.my_approved_prs(prs, "") == []
    assert Filters.my_unaddressed_review_prs(prs, "") == []


def test_store_lookup():
    s = Store()
    s.me = "latekvo"
    s.prs = _prs()
    s.issues = _issues()
    s.has_loaded = True
    assert s.lookup(101).on_lists == ["skillPRs"]
    assert s.lookup(201).on_lists == ["unaddressedIssues"]
    assert s.lookup(999).on_lists == []
    assert not s.lookup(999).is_on_any_list


def test_review_prompt_blocks_by_target():
    # My PRs: markReady + reply blocks, no formal-review block. We commit here,
    # so no review-only guard and the commit-attribution rule stays in.
    mine = review.ReviewConfig(me="latekvo").build_prompt()
    assert "mark it ready for review" in mine
    assert 'replying "Fixed in <commit_hash>"' in mine
    assert "POST a pull-request review" not in mine
    assert "ABSOLUTELY DO NOT touch their branch" not in mine

    # Someone else's PRs: formal-review block only, plus a hard no-commit guard.
    # We never touch their branch, so the commit-attribution rule is dropped.
    other = review.ReviewConfig(
        target=PRTarget.SOMEONE, username="someuser"
    ).build_prompt()
    assert "POST a pull-request review" in other
    assert "mark it ready for review" not in other
    assert "ABSOLUTELY DO NOT touch their branch" in other
    assert "No AI attribution" not in other

    # Single-PR mode (Specific PR target): fetch one PR by number.
    single = review.ReviewConfig(
        target=PRTarget.SPECIFIC, specific_pr="337", me="latekvo"
    )
    assert single.is_single_pr and single.is_valid
    single_prompt = single.build_prompt()
    assert single_prompt.startswith("Review PR #337 in software-mansion/argent.")
    # A specific PR may be mine OR someone else's, so the prompt is author-gated:
    # poll the author, then CASE A (mine -> fix on branch, mark ready) / CASE B
    # (theirs -> review only, never touch the branch, DO NOT mark ready).
    assert "WHO AUTHORED IT" in single_prompt
    assert "CASE A" in single_prompt and "CASE B" in single_prompt
    assert "on the PR's branch" in single_prompt  # depth onBranch fix step
    assert "mark it ready for review" in single_prompt  # CASE A
    assert "ABSOLUTELY DO NOT touch their branch" in single_prompt  # CASE B guard
    assert "isn't yours to advance" in single_prompt  # CASE B: don't mark ready
    assert "No AI attribution" in single_prompt  # CASE A commit guidance

    # Mark-ready off gates only CASE A: the mark-ready block drops, the
    # do-not-advance guard stays.
    single_no_ready = review.ReviewConfig(
        target=PRTarget.SPECIFIC, specific_pr="337", me="latekvo", mark_ready=False
    ).build_prompt()
    assert "mark it ready for review" not in single_no_ready
    assert "isn't yours to advance" in single_no_ready

    # A whose-PRs sweep with no PR-state box ticked is invalid (would review nothing).
    assert not review.ReviewConfig(
        target=PRTarget.MINE, me="latekvo", include_drafts=False, include_ready=False
    ).is_valid


def test_pr_ref_parsing():
    owner, repo = "software-mansion", "argent"

    # Bare number, with or without a leading '#'.
    assert parse_pr_ref("337", owner, repo).number == 337
    assert parse_pr_ref("  #42 ", owner, repo).number == 42

    # A full GitHub PR URL (trailing path allowed) for the target repo.
    url = parse_pr_ref(f"https://github.com/{owner}/{repo}/pull/337/files", owner, repo)
    assert url.number == 337 and url.is_valid and not url.repo_mismatch
    assert parse_pr_ref(f"github.com/{owner}/{repo}/pull/9", owner, repo).number == 9

    # The owner/repo#n shorthand, case-insensitive on the repo.
    short = parse_pr_ref(f"{owner.upper()}/{repo}#12", owner, repo)
    assert short.number == 12 and short.is_valid

    # A URL for a different repo extracts the number but is flagged + invalid.
    other = parse_pr_ref("https://github.com/other/proj/pull/5", owner, repo)
    assert other.number == 5 and other.repo_mismatch and not other.is_valid

    # Junk has no number.
    assert parse_pr_ref("not a pr", owner, repo).number is None
    assert parse_pr_ref("", owner, repo).number is None


def test_review_single_pr_accepts_url():
    # A pasted URL for the target repo resolves to the same single-PR prompt.
    cfg = review.ReviewConfig(
        target=PRTarget.SPECIFIC,
        specific_pr="https://github.com/software-mansion/argent/pull/337",
        me="latekvo",
    )
    assert cfg.is_valid
    assert cfg.build_prompt().startswith("Review PR #337 in software-mansion/argent.")

    # A URL for a different repo is rejected (SPAWN stays disabled).
    wrong = review.ReviewConfig(
        target=PRTarget.SPECIFIC,
        specific_pr="https://github.com/other/proj/pull/9",
        me="latekvo",
    )
    assert not wrong.is_valid and wrong.pr_ref.repo_mismatch


def test_conflict_single_pr_accepts_url():
    from argent_utils.conflicts import ConflictConfig, Target

    ok = ConflictConfig(
        target=Target.SPECIFIC,
        specific_pr="https://github.com/software-mansion/argent/pull/337",
    )
    assert ok.is_valid
    assert ok.build_prompt().startswith("Take PR #337 in software-mansion/argent.")

    wrong = ConflictConfig(target=Target.SPECIFIC, specific_pr="https://github.com/x/y/pull/1")
    assert not wrong.is_valid and wrong.pr_ref.repo_mismatch


def test_review_prompt_trailer_has_no_ai_attribution():
    p = review.ReviewConfig(me="latekvo").build_prompt()
    assert "No AI attribution" in p


def test_audit_prompt_toggles_gate_blocks():
    from argent_utils.audit import AuditConfig

    # The whole-repo audit needs no input and the hard-repro bar is always present.
    base = AuditConfig(me="latekvo").build_prompt()
    assert AuditConfig().is_valid
    assert "100% CERTAINTY" in base
    assert base.startswith("Run a FULL end-to-end test of the ENTIRE software-mansion/argent")
    # Reproduction must be driven on a real simulator/emulator; severity (H/M/L) is
    # classified for every finding — both always present, even in the read-only default.
    assert "SIMULATOR / EMULATOR" in base
    assert "HIGH" in base and "LOW" in base
    # Default (find-only): read-only, no issue-handling, no PRs (so no 20-LOC PR gate).
    assert "READ-ONLY audit" in base
    assert "OPEN ISSUES" not in base
    assert "focused pull request" not in base
    assert "20 lines" not in base

    # fix_issues adds the bug-issue block (and is explicit about skipping features).
    issues = AuditConfig(fix_issues=True).build_prompt()
    assert "OPEN ISSUES" in issues
    assert "SKIP every feature request" in issues
    assert "READ-ONLY audit" in issues  # still read-only until open_prs is set

    # open_prs swaps the read-only guard for the open-a-PR block + no-attribution,
    # and every opened PR must be a draft.
    prs = AuditConfig(open_prs=True).build_prompt()
    assert "focused pull request" in prs
    assert "DRAFT" in prs
    # Dedup against existing PRs by actual code, not titles.
    assert "DUPLICATE" in prs and "gh pr diff" in prs
    # Low/nitpick fixes only earn a PR when the diff is under 20 lines.
    assert "20 lines" in prs
    assert "No AI attribution" in prs
    assert "READ-ONLY audit" not in prs

    # Both on: issue-handling + PRs together.
    both = AuditConfig(fix_issues=True, open_prs=True).build_prompt()
    assert "OPEN ISSUES" in both and "focused pull request" in both


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
