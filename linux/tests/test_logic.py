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
    # My PRs: markReady + reply blocks, no formal-review block.
    mine = review.ReviewConfig(me="latekvo").build_prompt()
    assert "mark it ready for review" in mine
    assert 'replying "Fixed in <commit_hash>"' in mine
    assert "POST a pull-request review" not in mine

    # Someone else's PRs: formal-review block only.
    other = review.ReviewConfig(target_is_mine=False, username="someuser").build_prompt()
    assert "POST a pull-request review" in other
    assert "mark it ready for review" not in other

    # Single-PR mode (both scope boxes off): fetch one PR by number.
    single = review.ReviewConfig(
        include_drafts=False, include_ready=False, specific_pr="337", me="latekvo"
    )
    assert single.is_single_pr and single.is_valid
    assert single.build_prompt().startswith("Review PR #337 in software-mansion/argent.")


def test_review_prompt_trailer_has_no_ai_attribution():
    p = review.ReviewConfig(me="latekvo").build_prompt()
    assert "No AI attribution" in p


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
