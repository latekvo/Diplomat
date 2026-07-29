"""Cross-platform parity for the six tool lists.

``ToolData.items`` (Sources/DiplomatCore/ToolKind.swift) and ``Store.items_for``
(linux/diplomat_app/store.py) are two implementations of the same six lists,
identical down to the text of every row — ``"@{author} · {age} · {n} open
thread{s}"`` and the rest. Neither can delegate to the other the way prompt
assembly does: the lists are rebuilt on every render, so a shell-out per render
is not an option.

That left the row rules single-sourced nowhere and asserted nowhere, while
``core/README.md`` claims all the triage logic lives once. Changing a line2
format on one platform and not the other was a silent divergence with no test in
its way — the exact drift the golden prompts exist to prevent for prompts.

So: one fixture, both implementations, diff the rows. The Swift side answers via
``diplomat-core tool-data`` (the same binary the prompt tests already shell out
to, already required by this job). Sorting, filtering, pluralisation, the
optional third line and the empty-identity guards are all in scope, because they
all show up in the rows.

Fixture timestamps are offsets from *now* landing halfway between age
boundaries (3.5 days, not 3.0), so the milliseconds between the two processes
reading their own clock can never round one of them to a different label.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import timedelta

import pytest

from diplomat_app.models import OpenIssue, OpenPR, ReviewThread, now
from diplomat_app.store import Store

CORE_BIN = os.environ.get("DIPLOMAT_CORE_BIN")

pytestmark = pytest.mark.skipif(
    not CORE_BIN, reason="DIPLOMAT_CORE_BIN not set (build it with linux/scripts/build-core.sh)"
)

ME = "octocat"


def _iso(dt) -> str:
    return dt.isoformat()


def _fixture():
    """One fixture exercising every list, and the edges that shape a row.

    Returns ``(json_payload, prs, issues)`` — the same data twice, once in the
    shape the Swift CLI decodes and once as the Python dataclasses.
    """
    t = now()

    # Ages sit halfway between boundaries so neither side can round differently.
    created_3_5d = t - timedelta(days=3, hours=12)
    created_9_5d = t - timedelta(days=9, hours=12)
    created_40_5d = t - timedelta(days=40, hours=12)
    ready_20_5d = t - timedelta(days=20, hours=12)
    created_5_5h = t - timedelta(hours=5, minutes=30)

    prs = [
        # Skill PR: two SKILL.md files, so line3 lists both — and pluralisation and
        # join order are part of the row.
        OpenPR(number=101, title="Add profiler skill", url="https://x/pull/101",
               is_draft=False, author="alice", created_at=created_3_5d,
               ready_for_review_at=None,
               files=["skills/argent-native-profiler/SKILL.md",
                      "skills/argent-flow/SKILL.md", "src/unrelated.ts"],
               review_decision=None, review_threads=[]),
        # Draft skill PR — the draft/ready word in line2.
        OpenPR(number=102, title="Draft skill", url="https://x/pull/102",
               is_draft=True, author="bob", created_at=created_5_5h,
               ready_for_review_at=None,
               files=["skills/one/SKILL.md"],
               review_decision=None, review_threads=[]),
        # Installer PR with exactly ONE matching file (the prefixes come from
        # core/filters.json) — the singular "1 file", and line3 with the
        # "packages/" prefix stripped.
        OpenPR(number=103, title="Installer tweak", url="https://x/pull/103",
               is_draft=False, author="carol", created_at=created_9_5d,
               ready_for_review_at=None,
               files=["packages/argent-installer/setup.ts", "README.md"],
               review_decision=None, review_threads=[]),
        # Installer PR with TWO matching files across both prefixes — the plural,
        # and a multi-line line3.
        OpenPR(number=110, title="Installer + CLI", url="https://x/pull/110",
               is_draft=True, author="frank", created_at=created_5_5h,
               ready_for_review_at=None,
               files=["packages/argent-installer/a.ts", "packages/argent-cli/b.ts"],
               review_decision=None, review_threads=[]),
        # Stale ready, born ready (no conversion timestamp).
        OpenPR(number=104, title="Long-open PR", url="https://x/pull/104",
               is_draft=False, author="dave", created_at=created_40_5d,
               ready_for_review_at=None, files=["src/a.ts"],
               review_decision=None, review_threads=[]),
        # Stale ready, converted from draft — the other arm of the same word.
        OpenPR(number=105, title="Converted PR", url="https://x/pull/105",
               is_draft=False, author="erin", created_at=created_40_5d,
               ready_for_review_at=ready_20_5d, files=["src/b.ts"],
               review_decision=None, review_threads=[]),
        # Mine + approved. Two of them, deliberately out of sort order in the
        # fixture: with a single row, a sort-direction drift would be invisible.
        OpenPR(number=106, title="My approved PR", url="https://x/pull/106",
               is_draft=False, author=ME, created_at=created_3_5d,
               ready_for_review_at=None, files=["src/c.ts"],
               review_decision="APPROVED", review_threads=[]),
        OpenPR(number=111, title="Another approved PR", url="https://x/pull/111",
               is_draft=True, author=ME, created_at=created_9_5d,
               ready_for_review_at=None, files=["src/g.ts"],
               review_decision="APPROVED", review_threads=[]),
        # Mine with unaddressed threads: one owed, one resolved, one I answered
        # last, one I cannot resolve — only the first counts, and the count is
        # printed, so a triage disagreement shows up as a different row.
        OpenPR(number=107, title="My reviewed PR", url="https://x/pull/107",
               is_draft=False, author=ME, created_at=created_9_5d,
               ready_for_review_at=None, files=["src/d.ts"],
               review_decision="CHANGES_REQUESTED",
               review_threads=[
                   ReviewThread(is_resolved=False, viewer_can_resolve=True,
                                last_comment_author="reviewer"),
                   ReviewThread(is_resolved=True, viewer_can_resolve=True,
                                last_comment_author="reviewer"),
                   ReviewThread(is_resolved=False, viewer_can_resolve=True,
                                last_comment_author=ME),
                   ReviewThread(is_resolved=False, viewer_can_resolve=False,
                                last_comment_author="reviewer"),
               ]),
        # Mine, exactly ONE owed thread — the singular "1 open thread".
        OpenPR(number=108, title="One open thread", url="https://x/pull/108",
               is_draft=True, author=ME, created_at=created_5_5h,
               ready_for_review_at=None, files=["src/e.ts"],
               review_decision="CHANGES_REQUESTED",
               review_threads=[
                   ReviewThread(is_resolved=False, viewer_can_resolve=True,
                                last_comment_author="reviewer"),
               ]),
        # An UPPERCASE variant of my own login on the last comment: GitHub logins
        # are case-insensitive, so this thread is mine and must NOT count.
        OpenPR(number=109, title="Case-folded author", url="https://x/pull/109",
               is_draft=False, author=ME, created_at=created_3_5d,
               ready_for_review_at=None, files=["src/f.ts"],
               review_decision="CHANGES_REQUESTED",
               review_threads=[
                   ReviewThread(is_resolved=False, viewer_can_resolve=True,
                                last_comment_author=ME.upper()),
               ]),
    ]

    issues = [
        # External, unanswered, with labels → line3 lists them.
        OpenIssue(number=201, title="Crash on launch", url="https://x/issues/201",
                  author="outsider", author_association="NONE",
                  created_at=created_3_5d, updated_at=created_3_5d, comment_count=2,
                  assignees=[], labels=["bug", "needs-triage"], member_responded=False),
        # External, unanswered, NO labels → line3 is null, not "labels: ".
        OpenIssue(number=202, title="Docs typo", url="https://x/issues/202",
                  author="passerby", author_association="FIRST_TIME_CONTRIBUTOR",
                  created_at=created_9_5d, updated_at=created_9_5d, comment_count=0,
                  assignees=[], labels=[], member_responded=False),
        # A member already replied → addressed, so it must not appear.
        OpenIssue(number=203, title="Answered", url="https://x/issues/203",
                  author="outsider", author_association="NONE",
                  created_at=created_3_5d, updated_at=created_3_5d, comment_count=5,
                  assignees=[], labels=[], member_responded=True),
        # Assigned → addressed.
        OpenIssue(number=204, title="Assigned", url="https://x/issues/204",
                  author="outsider", author_association="NONE",
                  created_at=created_3_5d, updated_at=created_3_5d, comment_count=1,
                  assignees=["someone"], labels=[], member_responded=False),
        # Filed from inside the org → not external.
        OpenIssue(number=205, title="Internal", url="https://x/issues/205",
                  author="teammate", author_association="MEMBER",
                  created_at=created_3_5d, updated_at=created_3_5d, comment_count=0,
                  assignees=[], labels=[], member_responded=False),
    ]

    payload = {
        "me": ME,
        "prs": [
            {
                "number": p.number, "title": p.title, "url": p.url,
                "isDraft": p.is_draft, "author": p.author,
                "createdAt": _iso(p.created_at),
                "readyForReviewAt": _iso(p.ready_for_review_at) if p.ready_for_review_at else None,
                "files": p.files, "reviewDecision": p.review_decision,
                "mergeable": p.mergeable,
                "reviewThreads": [
                    {"isResolved": t.is_resolved,
                     "viewerCanResolve": t.viewer_can_resolve,
                     "lastCommentAuthor": t.last_comment_author}
                    for t in p.review_threads
                ],
            }
            for p in prs
        ],
        "issues": [
            {
                "number": i.number, "title": i.title, "url": i.url,
                "author": i.author, "authorAssociation": i.author_association,
                "createdAt": _iso(i.created_at), "updatedAt": _iso(i.updated_at),
                "commentCount": i.comment_count, "assignees": i.assignees,
                "labels": i.labels, "memberResponded": i.member_responded,
            }
            for i in issues
        ],
    }
    return payload, prs, issues


def _swift_rows(payload: dict) -> dict:
    proc = subprocess.run(
        [CORE_BIN, "tool-data"],
        input=json.dumps(payload).encode("utf-8"),
        capture_output=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, (
        f"diplomat-core tool-data failed: {proc.stderr.decode('utf-8', 'replace')}"
    )
    return json.loads(proc.stdout)


def _python_rows(prs, issues) -> dict:
    store = Store()
    store.me = ME
    store.prs = prs
    store.issues = issues
    store.has_loaded = True
    out = {}
    for tool_id in ("skillPRs", "installerPRs", "staleReady",
                    "unaddressedIssues", "myApproved", "myUnaddressed"):
        out[tool_id] = [
            {"id": d.id, "badge": d.badge, "title": d.title, "url": d.url,
             "line2": d.line2, "line3": d.line3}
            for d in store.items_for(tool_id)
        ]
    return out


@pytest.fixture(scope="module")
def rows():
    payload, prs, issues = _fixture()
    return _swift_rows(payload), _python_rows(prs, issues)


def test_both_platforms_cover_the_same_tools(rows):
    swift, python = rows
    assert set(swift) == set(python)


@pytest.mark.parametrize("tool_id", [
    "skillPRs", "installerPRs", "staleReady",
    "unaddressedIssues", "myApproved", "myUnaddressed",
])
def test_tool_rows_match_across_platforms(rows, tool_id):
    """Same rows, same order, same text. A failure here means one front-end shows
    the operator something the other doesn't."""
    swift, python = rows
    assert python[tool_id] == swift[tool_id], (
        f"{tool_id} differs between the Swift core and the Linux applet\n"
        f"  swift:  {json.dumps(swift[tool_id], indent=2)}\n"
        f"  python: {json.dumps(python[tool_id], indent=2)}"
    )


def test_the_fixture_actually_populates_every_list(rows):
    """A fixture that filtered everything out would make the parity assertions
    vacuous — six empty lists match six empty lists."""
    _swift, python = rows
    for tool_id, items in python.items():
        assert items, f"{tool_id} is empty — the fixture no longer exercises it"


def test_every_list_has_enough_rows_to_expose_an_ordering_drift(rows):
    """Order is part of the comparison, but only observably so with more than one
    row — a single-row list matches whichever way either side sorts."""
    _swift, python = rows
    for tool_id, items in python.items():
        assert len(items) >= 2, (
            f"{tool_id} has {len(items)} row(s); a sort-order drift would pass unseen"
        )


def test_the_fixture_exercises_the_optional_third_line(rows):
    """line3 is present on some rows and null on others; both must survive the
    round-trip, since a `null` vs `""` disagreement is exactly the kind of drift
    this guards."""
    _swift, python = rows
    issues = python["unaddressedIssues"]
    assert any(row["line3"] is None for row in issues)
    assert any(row["line3"] is not None for row in issues)
