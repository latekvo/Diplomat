"""Regression tests for the shared-core logic the Linux UI renders.

Pure, offline — no gh, no display. Run with: ``python -m pytest linux/tests``
(or ``python linux/tests/test_logic.py`` for a dependency-free smoke run).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from diplomat_app import review  # noqa: E402
from diplomat_app.models import Filters, Fmt, OpenIssue, OpenPR, ReviewThread  # noqa: E402
from diplomat_app.prref import parse_pr_ref  # noqa: E402
from diplomat_app.prtarget import PRTarget  # noqa: E402
from diplomat_app.store import Store  # noqa: E402

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


def test_fmt_duration_held_time():
    # The in-use "held" label: sub-minute rounds to "just now", then m / h m / d h.
    assert Fmt.duration(0) == "just now"
    assert Fmt.duration(59) == "just now"
    assert Fmt.duration(12 * 60) == "12m"
    assert Fmt.duration(83 * 60) == "1h 23m"
    assert Fmt.duration(3600) == "1h"
    assert Fmt.duration(26 * 3600) == "1d 2h"
    assert Fmt.duration(-5) == "just now"  # never negative


def test_my_tools_empty_without_identity():
    prs = _prs()
    assert Filters.my_approved_prs(prs, "") == []
    assert Filters.my_unaddressed_review_prs(prs, "") == []


def test_store_settings_are_isolated_from_the_real_user(tmp_path):
    # conftest.py redirects QSettings into the per-test temp dir; a hidden-tools
    # write from a test must land there, never in the user's real settings (which
    # would also leak back in and break tests like test_store_lookup).
    s = Store()
    # Fresh settings: SKILL.md + Installer/CLI tools ship hidden by default.
    assert s.hidden_tools == {"skillPRs", "installerPRs"}
    s.hidden_tools = {"skillPRs"}
    assert s.hidden_tools == {"skillPRs"}
    s._settings.sync()
    assert list(tmp_path.rglob("*.ini")), "Store settings must land in tmp_path"


def test_store_lookup():
    s = Store()
    s.me = "latekvo"
    s.prs = _prs()
    s.issues = _issues()
    s.has_loaded = True
    s.hidden_tools = set()  # lookup only reports visible tools; show them all
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


def test_final_pass_never_applies_to_my_own_prs():
    # The approve/changes-requested verdict is a reviewer's call — I don't approve
    # my own work, so target=MINE drops the block even with the toggle on
    # (Swift: canFinalPass = disposition != .mine).
    mine = review.ReviewConfig(me="latekvo", final_pass=True)
    assert not mine.can_final_pass
    # The gating now lives in Swift (diplomat-core); assert the observable behavior.
    assert "FULL E2E pass" not in mine.build_prompt()

    # Someone else's PRs and a specific PR (author unknown) keep the escalation.
    other = review.ReviewConfig(
        target=PRTarget.SOMEONE, username="someuser", final_pass=True
    )
    assert other.can_final_pass
    assert "FULL E2E pass" in other.build_prompt()
    single = review.ReviewConfig(
        target=PRTarget.SPECIFIC, specific_pr="337", me="latekvo", final_pass=True
    )
    assert single.can_final_pass
    assert "FULL E2E pass" in single.build_prompt()

    # Off by default everywhere.
    assert "FULL E2E pass" not in review.ReviewConfig(
        target=PRTarget.SOMEONE, username="someuser"
    ).build_prompt()


def test_soft_approve_leaves_a_thank_you_but_no_verdict():
    # Soft-approve is ON by default: a review-only PR that comes back clean gets a
    # friendly thank-you comment, but never an APPROVE action. It never applies to my
    # own PRs (Swift: canSoftApprove = disposition != .mine).
    mine = review.ReviewConfig(me="latekvo")  # target MINE
    assert not mine.can_soft_approve
    assert "Thank you for contributing" not in mine.build_prompt()

    # Someone else's PRs: soft-approve on by default -> the thank-you comment, no APPROVE.
    other = review.ReviewConfig(target=PRTarget.SOMEONE, username="someuser")
    assert other.can_soft_approve
    other_prompt = other.build_prompt()
    assert "Thank you for contributing" in other_prompt
    assert "FULL E2E pass" not in other_prompt  # soft, not a hard verdict

    # Turning it off -> fully silent (no thank-you), and still no verdict.
    silent = review.ReviewConfig(
        target=PRTarget.SOMEONE, username="someuser", soft_approve=False
    ).build_prompt()
    assert "Thank you for contributing" not in silent

    # A real verdict (final_pass) outranks the default-on soft-approve.
    verdict = review.ReviewConfig(
        target=PRTarget.SOMEONE, username="someuser", final_pass=True
    ).build_prompt()
    assert "FULL E2E pass" in verdict
    assert "Thank you for contributing" not in verdict


def test_specific_pr_disposition_drives_toggles_and_prompt():
    # A specific PR's polled author (mine / theirs / unknown) picks the disposition,
    # which decides both the visible action toggles and the diplomat-core prompt -
    # mirroring ReviewConfig.disposition / canX in DiplomatCore/Review.swift.
    from diplomat_app.review import SpecificAuthor

    def cfg(author: SpecificAuthor) -> review.ReviewConfig:
        return review.ReviewConfig(
            target=PRTarget.SPECIFIC, specific_pr="337", me="latekvo",
            specific_author=author,
        )

    # MINE -> fix on branch: mark-ready + reply on, formal-review + final-verdict off.
    mine = cfg(SpecificAuthor.MINE)
    assert mine.disposition == SpecificAuthor.MINE
    assert (mine.can_mark_ready, mine.can_leave_reviews,
            mine.can_reply_to_reviews, mine.can_final_pass) == (True, False, True, False)
    mine_p = mine.build_prompt()
    assert "This PR is MINE" in mine_p
    assert "CASE A" not in mine_p  # a known-author prompt, not the author-gated split
    assert "No AI attribution" in mine_p  # we commit on my branch

    # THEIRS -> review only: formal-review + final-verdict on, mark-ready + reply off.
    theirs = cfg(SpecificAuthor.THEIRS)
    assert theirs.disposition == SpecificAuthor.THEIRS
    assert (theirs.can_mark_ready, theirs.can_leave_reviews,
            theirs.can_reply_to_reviews, theirs.can_final_pass) == (False, True, False, True)
    theirs_p = theirs.build_prompt()
    assert "someone else" in theirs_p.lower()
    assert "ABSOLUTELY DO NOT touch their branch" in theirs_p
    assert "No AI attribution" not in theirs_p  # never touch their branch

    # UNKNOWN (author still pending / poll failed) -> every toggle offered, and the
    # author-gated CASE A/B prompt (the agent resolves the author itself).
    unknown = cfg(SpecificAuthor.UNKNOWN)
    assert unknown.disposition == SpecificAuthor.UNKNOWN
    assert (unknown.can_mark_ready, unknown.can_leave_reviews,
            unknown.can_reply_to_reviews, unknown.can_final_pass) == (True, True, True, True)
    unknown_p = unknown.build_prompt()
    assert "CASE A" in unknown_p and "CASE B" in unknown_p

    # UNKNOWN is the default for a specific PR (back-compat with pre-detection config).
    assert review.ReviewConfig(
        target=PRTarget.SPECIFIC, specific_pr="337", me="latekvo"
    ).disposition == SpecificAuthor.UNKNOWN


def test_sweep_disposition_follows_target_not_specific_author():
    # For a whose-PRs sweep the disposition follows the target regardless of any
    # stale specific_author value (which only applies to a single PR).
    from diplomat_app.review import SpecificAuthor

    mine = review.ReviewConfig(me="latekvo", specific_author=SpecificAuthor.THEIRS)
    assert mine.disposition == SpecificAuthor.MINE
    other = review.ReviewConfig(
        target=PRTarget.SOMEONE, username="u", specific_author=SpecificAuthor.MINE
    )
    assert other.disposition == SpecificAuthor.THEIRS


def test_poll_dispatch_does_not_self_deadlock_on_the_overlap_lock():
    # Regression: run_autofix_poll_async holds _autofix_lock for the WHOLE poll
    # (the overlap guard), and a poll reaches dispatch_agent to spawn a fix. If
    # dispatch_agent re-acquired that same non-reentrant lock for its
    # _dispatching_prs dedup, the poll would deadlock on itself, never release the
    # lock, and every later tick would no-op ("no monitor has polled yet") forever.
    # dispatch_agent must guard _dispatching_prs with its OWN mutex.
    import threading

    from diplomat_app import autofix

    s = Store()
    s.me = "latekvo"
    # Keep dispatch offline and deterministic: no mesh, no real terminal spawn.
    s._route_via_mesh = lambda job: None
    s._spawn_tracked = lambda prompt, url, number: True
    s._in_flight = lambda url: False

    job = autofix.AgentJob(
        kind="conflicts",
        audit_action="conflicts",
        label="Resolve · #421",
        prompt="resolve",
        pr_url="u/421",
        pr_number=421,
        duty="conflicts",
        work_key="",
        counter="conflicts",
    )

    result: list[str] = []

    def poll_body() -> None:
        # Mirror run_autofix_poll_async: hold the overlap lock across the dispatch.
        assert s._autofix_lock.acquire(blocking=False)
        try:
            result.append(s.dispatch_agent(job, autofix.SOURCE_AUTO))
        finally:
            s._autofix_lock.release()

    t = threading.Thread(target=poll_body, daemon=True)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "dispatch under the poll's overlap lock deadlocked"
    assert result == ["spawned"]
    # And the lock is free again for the next tick (was released, not wedged).
    assert s._autofix_lock.acquire(blocking=False)
    s._autofix_lock.release()


def test_openpr_mergeable_and_has_conflicts():
    # mergeable defaults to UNKNOWN (fixtures/older payloads) and only the exact
    # CONFLICTING state reads as a conflict — mirrors OpenPR in Models.swift.
    assert _prs()[0].mergeable == "UNKNOWN"
    assert not _prs()[0].has_conflicts
    conflicting = OpenPR(106, "conflicting", "u/106", False, "dave", NOW, None,
                         ["c.ts"], None, [], mergeable="CONFLICTING")
    assert conflicting.has_conflicts
    clean = OpenPR(107, "clean", "u/107", False, "dave", NOW, None,
                   ["d.ts"], None, [], mergeable="MERGEABLE")
    assert not clean.has_conflicts


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

    # ASCII digits only, matching Swift's PRRef (which guards Int(_:) the same way):
    # non-ASCII digits ("٣٣٧".isdigit() is True in Python) and an explicit '+' sign
    # are both rejected on both sides.
    assert parse_pr_ref("٣٣٧", owner, repo).number is None
    assert parse_pr_ref("#٣٣٧", owner, repo).number is None
    assert parse_pr_ref("+337", owner, repo).number is None
    assert parse_pr_ref(
        f"https://github.com/{owner}/{repo}/pull/٣٣٧", owner, repo
    ).number is None


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
    from diplomat_app.conflicts import ConflictConfig, Target

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
    from diplomat_app.audit import AuditConfig

    # The whole-repo audit needs no input and the hard-repro bar is always present.
    base = AuditConfig().build_prompt()
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


def test_device_allocator_state_helpers():
    from diplomat_app import deviceallocator as da

    state = {"devices": [
        {"status": "ready", "owner": {"agentName": "a", "ownerPid": 1}},
        {"status": "booting", "owner": {"agentName": "b", "ownerPid": 2}},
        # A device under repair is out of the pool even though no live owner holds it.
        {"status": "repairing", "owner": {"agentName": "repair", "ownerPid": None},
         "brokenReason": "boot timeout"},
        {"status": "free", "owner": None},
        {"status": "running-free", "owner": None},
    ]}
    assert da.is_allocated(state["devices"][0]) is True
    assert da.is_allocated(state["devices"][1]) is True
    assert da.is_allocated(state["devices"][2]) is True   # repairing = not available
    assert da.is_allocated(state["devices"][3]) is False
    assert da.is_allocated(state["devices"][4]) is False
    assert da.allocated_count(state) == 3
    assert da.free_count(state) == 2

    # The bridge resolves a usable node + the package, and never raises on a missing
    # state file (the common "daemon never started" case just yields None).
    assert da.package_dir().endswith("device-allocator")
    assert da.read_state() is None or isinstance(da.read_state(), dict)


def test_is_skill_file_matches_filename_not_bare_suffix():
    # Regression: a bare endswith("skill.md") also matched "docs/reskill.md".
    # Mirrors Filters.isSkillFile in Models.swift (filename match).
    assert Filters.is_skill_file("skills/foo/SKILL.md") is True
    assert Filters.is_skill_file("a/my.skill.md") is True
    assert Filters.is_skill_file("docs/reskill.md") is False
    assert Filters.is_skill_file("SKILL.md") is True


def test_unaddressed_threads_login_compare_is_case_insensitive():
    # GitHub logins are case-insensitive; a thread last-touched by "Alice" is NOT
    # owed by "alice". Mirrors ThreadTriage.owed in Models.swift.
    t = ReviewThread(is_resolved=False, viewer_can_resolve=True, last_comment_author="Alice")
    pr = OpenPR(1, "t", "u", False, "bob", NOW, None, [], None, [t])
    assert pr.unaddressed_threads("alice") == []
    # a thread last-touched by someone else IS owed
    t2 = ReviewThread(is_resolved=False, viewer_can_resolve=True, last_comment_author="bob")
    pr2 = OpenPR(1, "t", "u", False, "bob", NOW, None, [], None, [t2])
    assert len(pr2.unaddressed_threads("alice")) == 1


def test_repo_path_resolution(tmp_path):
    """Settings → REPO ROOT drives the `cd` in every spawned session.

    It lives in the shared appconfig file rather than QSettings so a mesh node — its
    own stdlib-only process, no Qt, no Store — resolves the same value on its next
    spawn. DIPLOMAT_REPO still outranks it; ~/dev/<repo> is the fallback.
    """
    import shlex

    from diplomat_app import appconfig, core

    prior_repo = os.environ.pop("DIPLOMAT_REPO", None)
    prior_config = os.environ.get("DIPLOMAT_CONFIG")
    os.environ["DIPLOMAT_CONFIG"] = str(tmp_path / "config.json")
    try:
        # Nothing set anywhere: the conventional path for the configured target repo.
        assert review.default_repo_path() == os.path.expanduser(
            f"~/dev/{core.config()['repo']}"
        )
        assert review.repo_path() == review.default_repo_path()

        # The Settings pick wins over the default and lands in the shared file — the
        # exact bytes a node (which has no Store to ask) reads back.
        store = Store()
        picked = str(tmp_path / "clone")
        store.repo_path_override = picked
        assert store.repo_path_override == picked
        assert appconfig.read() == {"repoRoot": picked}
        assert review.stored_repo_path() == picked
        assert review.repo_path() == picked

        # A hand-typed "~/…" expands like the shell would — the spawn single-quotes
        # the path, so the shell itself never gets the chance.
        store.repo_path_override = "~/dev/typed"
        assert review.repo_path() == os.path.expanduser("~/dev/typed")
        assert review.shell_command("/tmp/p.txt").startswith(
            f"cd {shlex.quote(os.path.expanduser('~/dev/typed'))} 2>/dev/null;"
        )

        # Whitespace-only is blank; blank drops the key rather than storing "".
        store.repo_path_override = "  "
        assert review.repo_path() == review.default_repo_path()
        store.repo_path_override = ""
        assert appconfig.read() == {}
        assert review.repo_path() == review.default_repo_path()

        # A truncated / hand-edited file degrades to the default instead of breaking
        # every spawn.
        (tmp_path / "config.json").write_text("{ not json", encoding="utf-8")
        assert review.repo_path() == review.default_repo_path()

        # The env override outranks the stored pick.
        store.repo_path_override = picked
        os.environ["DIPLOMAT_REPO"] = str(tmp_path / "env-clone")
        assert review.repo_path() == str(tmp_path / "env-clone")
    finally:
        os.environ.pop("DIPLOMAT_REPO", None)
        if prior_repo is not None:
            os.environ["DIPLOMAT_REPO"] = prior_repo
        os.environ.pop("DIPLOMAT_CONFIG", None)
        if prior_config is not None:
            os.environ["DIPLOMAT_CONFIG"] = prior_config


# A meta-path finder that makes `import PySide6[...]` fail, so a subprocess simulates
# the stdlib-only mesh node — the deployment whose broken repo-root read this whole
# design exists to fix. Kept as source the child execs, not imported here.
_QT_LESS_CHILD = """
import sys

class _NoQt:
    def find_spec(self, name, path=None, target=None):
        if name == "PySide6" or name.startswith("PySide6."):
            raise ImportError("PySide6 blocked (simulating a Qt-less mesh node)")
        return None

sys.meta_path.insert(0, _NoQt())
try:
    import PySide6  # noqa: F401
    print("QT_STILL_IMPORTABLE"); sys.exit(2)
except ImportError:
    pass

from diplomat_app import review
print(review.repo_path())
"""


def test_qt_less_node_reads_the_shared_config(tmp_path):
    """A mesh node is a separate, stdlib-only process — the exact case the first design
    got wrong (it read Qt settings, which failed silently with no PySide6). Prove a
    process that CANNOT import PySide6 still resolves the repo root the applet wrote,
    by reading it back out of the shared file rather than any Qt store."""
    import subprocess
    import sys

    cfg = tmp_path / "config.json"
    picked = str(tmp_path / "node-clone")
    # Write it the way the applet's Store setter does, from THIS (Qt-capable) process.
    prior = os.environ.get("DIPLOMAT_CONFIG")
    os.environ["DIPLOMAT_CONFIG"] = str(cfg)
    try:
        from diplomat_app import appconfig

        appconfig.set_value(appconfig.REPO_ROOT, picked)
        assert cfg.exists(), "the applet-side write must land on disk"

        linux_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = {**os.environ, "PYTHONPATH": linux_dir, "DIPLOMAT_CONFIG": str(cfg)}
        out = subprocess.run(
            [sys.executable, "-c", _QT_LESS_CHILD],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert out.returncode == 0, f"child failed: {out.stdout!r} {out.stderr!r}"
        # A separate process, with Qt import blocked, resolved the applet's pick.
        assert out.stdout.strip() == picked
    finally:
        os.environ.pop("DIPLOMAT_CONFIG", None)
        if prior is not None:
            os.environ["DIPLOMAT_CONFIG"] = prior


if __name__ == "__main__":
    # Standalone (no-pytest) mode bypasses conftest.py, so replicate its QSettings
    # isolation here — otherwise these tests would read (and one would WRITE) the
    # user's real settings, the exact bug class the isolation exists to prevent.
    import inspect
    import tempfile
    from pathlib import Path

    from PySide6.QtCore import QSettings

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        # Fresh isolated dir per test, exactly like conftest's autouse fixture —
        # it doubles as the `tmp_path` fixture for tests that take one (the
        # isolation test asserts settings land in ITS tmp_path).
        fresh = Path(tempfile.mkdtemp(prefix="diplomat-test-"))
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(fresh))
        kwargs = {"tmp_path": fresh} if "tmp_path" in inspect.signature(fn).parameters else {}
        fn(**kwargs)
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
