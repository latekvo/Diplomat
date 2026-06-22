"""Headless self-tests — the GUI's data layer, exercised end to end.

Mirrors the macOS ``Dump`` enum so the two front-ends can be cross-checked:

    ARGENT_UTILS_DUMP=1           full fetch+filter pipeline, prints all 6 tools
    ARGENT_UTILS_LOOKUP=337       reverse-lookup one number through the real Store
    ARGENT_UTILS_PRINT_PROMPT=... assemble + print a Review-PRs prompt (mine|user|single)

None of these need a display; they only touch QtCore (QSettings) + gh.
"""

from __future__ import annotations

from . import review
from .models import API, Filters, Fmt
from .review import ReviewConfig
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


def run_print_prompt(mode: str) -> int:
    m = mode.lower()
    is_user = m.startswith("user")
    is_single = m.startswith("single")
    cfg = ReviewConfig(
        depth="max",
        target_is_mine=not is_user,
        username="someuser" if is_user else "",
        me="latekvo",
        mark_ready=True,
        leave_reviews=True,
        reply_to_reviews=True,
        include_drafts=not is_single,
        include_ready=not is_single,
        specific_pr="337" if is_single else "",
    )
    label = "single PR #337" if is_single else ("someone else's PRs" if is_user else "my PRs")

    print(f"== ReviewConfig: {label} · depth={review.depth_by_id(cfg.depth)['title']} ==\n")
    print("----- PROMPT -----")
    print(cfg.build_prompt())
    print("\n----- SHELL COMMAND -----")
    file = review.write_prompt(cfg.build_prompt())
    print(review.shell_command(file))
    return 0
