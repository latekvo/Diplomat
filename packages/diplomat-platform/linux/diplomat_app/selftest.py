"""Headless self-tests — the GUI's data layer, exercised end to end.

Mirrors the macOS ``Dump`` enum so the two front-ends can be cross-checked:

    DIPLOMAT_DUMP=1           full fetch+filter pipeline, prints all 6 tools
    DIPLOMAT_LOOKUP=337       reverse-lookup one number through the real Store
    DIPLOMAT_PRINT_PROMPT=... assemble + print a wizard's prompt: mine|user|single for
                              Review, conflicts[-user|-single], audit[-issues|-prs|-all],
                              issues[-single][-features]
    DIPLOMAT_COMMS_POLL=1     one real Slack tick: each new ping, its triage and the
                              effect it would have (nothing is queued)

None of these need a display; they only touch QtCore (QSettings) + gh.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

from diplomat_runtime import review, runner, usagescan
from diplomat_runtime.models import API, Filters, Fmt
from diplomat_runtime.prtarget import PRTarget
from diplomat_runtime.review import ReviewConfig
from .store import Store, tool_by_id


def run_dump() -> int:
    try:
        me = API.fetch_viewer_login()
        prs = API.fetch_open_prs()
        issues = API.fetch_open_issues()
    except Exception as exc:  # noqa: BLE001
        print(f"DUMP ERROR: {exc}")
        return 1

    print(f"== viewer: @{me} · open PRs: {len(prs)} · open issues: {len(issues)} ==\n")

    t1 = sorted(Filters.skill_prs(prs), key=lambda p: -p.number)
    print(f"TOOL 1 — SKILL.md PRs: {len(t1)}")
    for p in t1:
        s = ", ".join(Fmt.skill_name(f) for f in p.files if Filters.is_skill_file(f))
        print(f"  #{p.number} @{p.author} [{'draft' if p.is_draft else 'ready'}] → {s}")

    t2 = sorted(Filters.installer_prs(prs), key=lambda p: -p.number)
    print(f"\nTOOL 2 — installer/CLI PRs: {len(t2)}")
    for p in t2:
        f = [x for x in p.files if Filters.is_installer_file(x)]
        print(f"  #{p.number} @{p.author} ({len(f)}) → {', '.join(Fmt.short_path(x) for x in f)}")

    t3 = sorted(Filters.stale_ready_prs(prs), key=lambda p: p.ready_at)
    print(f"\nTOOL 3 — ready >10d: {len(t3)}")
    for p in t3:
        kind = "born-ready" if p.ready_for_review_at is None else "converted"
        print(f"  #{p.number} @{p.author} {Fmt.days(p.ready_at)}d ({kind})")

    t4 = sorted(Filters.unaddressed_external_issues(issues), key=lambda i: i.created_at)
    print(f"\nTOOL 4 — unaddressed external issues: {len(t4)}")
    for i in t4:
        print(
            f"  #{i.number} @{i.author} [{i.author_association}] {Fmt.days(i.created_at)}d "
            f"{i.comment_count}c labels:[{','.join(i.labels)}]"
        )

    t5 = sorted(Filters.my_approved_prs(prs, me), key=lambda p: -p.number)
    print(f"\nTOOL 5 — my approved PRs: {len(t5)}")
    for p in t5:
        print(f"  #{p.number} @{p.author} [{'draft' if p.is_draft else 'ready'}] {Fmt.age(p.created_at)}")

    t6 = sorted(Filters.my_unaddressed_review_prs(prs, me), key=lambda p: -p.number)
    print(f"\nTOOL 6 — my PRs w/ unaddressed reviews: {len(t6)}")
    for p in t6:
        print(f"  #{p.number} @{p.author} {len(p.unaddressed_threads(me))} open thread(s)")
    return 0


def run_lookup(n: int) -> int:
    try:
        me = API.fetch_viewer_login()
        prs = API.fetch_open_prs()
        issues = API.fetch_open_issues()
    except Exception as exc:  # noqa: BLE001
        print(f"LOOKUP ERROR: {exc}")
        return 1
    s = Store()
    s.me = me
    s.prs = prs
    s.issues = issues
    s.has_loaded = True
    r = s.lookup(n)
    print(f"#{n}: {r.presence}")
    if r.on_lists:
        names = ", ".join(tool_by_id(tid).title for tid in r.on_lists)
    else:
        names = "(none)"
    print(f"on lists: {names}")
    return 0


def _print_prompt_dump(header: str, prompt: str) -> int:
    """The shared body of the prompt dumps: the assembled prompt, then the shell
    command that would run it.

    Twin of ``Dump.printPromptDump`` in DiplomatApp.swift, which additionally prints
    the AppleScript - macOS spawns a terminal that way, Linux runs the shell command
    directly. ``prompt`` is passed in already built because building it shells out to
    the diplomat-core CLI, so it is worth doing exactly once.
    """
    print(f"== {header} ==\n")
    print("----- PROMPT -----")
    print(prompt)
    print("\n----- SHELL COMMAND -----")
    # An OpenCode 2.x spawn's command names a session id, minted here but never staged
    # or created: what the dump is for is the shape of the command.
    service = runner.selected() == runner.OPENCODE and usagescan.opencode_is_v2()
    print(review.shell_command(
        review.write_prompt(prompt),
        opencode_session=runner.new_opencode_session() if service else None))
    return 0


def run_print_prompt(mode: str) -> int:
    m = mode.lower()
    if m.startswith("conflict"):
        return _run_conflict_prompt(m)
    if m.startswith("audit"):
        return _run_audit_prompt(m)
    if m.startswith("issues"):
        return _run_issue_prompt(m)
    is_user = m.startswith("user")
    is_single = m.startswith("single")
    target = (
        PRTarget.SPECIFIC if is_single else (PRTarget.SOMEONE if is_user else PRTarget.MINE)
    )
    cfg = ReviewConfig(
        depth="max",
        target=target,
        username="someuser" if is_user else "",
        me="latekvo",
        mark_ready=True,
        leave_reviews=True,
        reply_to_reviews=True,
        specific_pr="337" if is_single else "",
        final_pass="final" in m,
    )
    label = "single PR #337" if is_single else ("someone else's PRs" if is_user else "my PRs")
    depth = review.depth_by_id(cfg.depth)["title"]
    return _print_prompt_dump(f"ReviewConfig: {label} · depth={depth}", cfg.build_prompt())


def _run_conflict_prompt(m: str) -> int:
    """Resolve-conflicts variant: conflicts-user / conflicts-single / conflicts(-mine)."""
    from .conflicts import ConflictConfig, Target

    is_user = "user" in m
    is_single = "single" in m
    target = Target.SPECIFIC if is_single else (Target.SOMEONE if is_user else Target.MINE)
    cfg = ConflictConfig(
        target=target,
        username="someuser" if is_user else "",
        me="latekvo",
        specific_pr="337" if is_single else "",
    )
    label = "single PR #337" if is_single else ("someone else's PRs" if is_user else "my PRs")
    return _print_prompt_dump(f"ConflictConfig: {label}", cfg.build_prompt())


def _run_audit_prompt(m: str) -> int:
    """Full-E2E-test variant: audit / audit-issues / audit-prs / audit-all."""
    from .audit import AuditConfig

    cfg = AuditConfig(
        fix_issues="issues" in m or "all" in m,
        open_prs="prs" in m or "all" in m,
    )
    flags = f"fixIssues={cfg.fix_issues} openPRs={cfg.open_prs}"
    return _print_prompt_dump(f"AuditConfig: full-repo E2E test · {flags}", cfg.build_prompt())


def _run_issue_prompt(m: str) -> int:
    """Fix-issues variant: issues[-single][-features].

    Every Fix-issues run works ONE issue, so the mode picks between the two shapes one
    comes in: "issues-single" (an issue named by hand in the wizard) and anything else
    (e.g. "issues") = one issue of a sweep, which carries the re-checks a named one
    does not. Mirrors ``printIssuePrompt`` in DiplomatApp.swift."""
    from .issues import IssueConfig, Target, depth_by_id

    named = "single" in m or "specific" in m
    wizard = IssueConfig(
        target=Target.SPECIFIC if named else Target.ALL,
        me="latekvo",
        specific_issue="421" if named else "",
        include_features="features" in m,
    )
    cfg = wizard if named else wizard.for_issue(421)
    label = "issue #421, named by hand" if named else "issue #421, out of a sweep"
    depth = depth_by_id(cfg.depth)["title"]
    return _print_prompt_dump(f"IssueConfig: {label} · depth={depth}", cfg.build_prompt())


#: How far back a dry run into a scratch dir looks (``Dump.commsPollLookback``).
COMMS_POLL_LOOKBACK_SECS = 3600


def run_comms_poll() -> int:
    """``DIPLOMAT_COMMS_POLL=1`` (twin of ``Dump.commsPoll``). Nothing is queued.

    Unless ``DIPLOMAT_COMMS_DIR`` is set, it ticks a scratch dir whose watermark is
    :data:`COMMS_POLL_LOOKBACK_SECS` ago, removed afterwards, so the live applet's own
    directory is untouched. 1 when the tick failed, a missing Slack login included."""
    import json

    from diplomat_runtime import comms

    if os.environ.get("DIPLOMAT_COMMS_DIR"):
        return _comms_poll(comms)
    scratch = Path(tempfile.mkdtemp(prefix="diplomat-commspoll-"))
    since = int(time.time()) - COMMS_POLL_LOOKBACK_SECS
    fd = os.open(scratch / "state.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"watermark": f"{since}.000000", "seen": []}, f)
    print(f"(scratch: the last {COMMS_POLL_LOOKBACK_SECS // 60} min of Slack)")
    os.environ["DIPLOMAT_COMMS_DIR"] = str(scratch)
    try:
        return _comms_poll(comms)
    finally:
        del os.environ["DIPLOMAT_COMMS_DIR"]
        shutil.rmtree(scratch, ignore_errors=True)


def _comms_poll(comms) -> int:
    import json

    store = Store()
    store.fetch_me()
    settings = store.comms_settings_payload()
    print(f"== comms tick · dir {comms.comms_dir()} · model {settings['model']} ==")
    result = comms.tick(settings)
    me = result.get("me") or {}
    if me:
        print(f"as @{me.get('handle', '')} ({me.get('userID', '')} in {me.get('teamID', '')})")
    items = result.get("items") or []
    print(f"{len(items)} new ping{'' if len(items) == 1 else 's'}")
    for it in items:
        m, d = it["message"], it["decision"]
        print(f"\n- {m['id']}  {Store.comms_place(m)} from {Store.comms_sender(m)}"
              f" [{', '.join(m.get('reasons') or [])}]")
        print(f"  text:     {Store.comms_snippet(m.get('text', ''), 200)}")
        number = f" #{d['number']}" if d.get("number") else ""
        print(f"  decision: {d.get('action')}{number} — {d.get('summary', '')}")
        print(f"  reason:   {d.get('reason', '')}")
        try:
            effect = comms.effect(d, m, store.comms_toggles(), settings)
        except Exception as exc:  # noqa: BLE001 — say what the core said and go on
            print(f"  effect:   (could not plan: {exc})")
            continue
        if effect.get("kind") == "investigate":
            cfg = effect["config"]
            print(f"  effect:   would queue {cfg.get('label')!r}, report {cfg.get('reportPath')}")
        else:
            print(f"  effect:   {json.dumps(effect)} (not applied)")
    if result.get("error"):
        print(f"\nerror: {result['error']}")
        return 1
    return 0
