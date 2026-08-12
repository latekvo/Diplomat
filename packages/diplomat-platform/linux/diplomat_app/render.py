"""Headless UI render — snapshot a panel state to PNG and exit.

The Linux analogue of macOS Render.swift. Lets us verify the rendered UI without
a real display by grabbing the widget's own pixels:

    DIPLOMAT_RENDER=panel DIPLOMAT_RENDER_OUT=/tmp/p.png \
        QT_QPA_PLATFORM=offscreen python -m diplomat_app

what ∈ {panel, lookup, wizard, conflicts, settings[-explain], devices, mesh,
telemetry}.
With DIPLOMAT_RENDER_LIVE=1 it fetches real data first; otherwise it uses a small
synthetic fixture.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

from PySide6.QtWidgets import QApplication

from . import agentregistry, agentstate, probes, szpont
from .models import OpenIssue, OpenPR
from .panel import Panel
from .store import Store


def _fixture(store: Store) -> None:
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=15)
    store.me = "latekvo"
    store.prs = [
        OpenPR(389, "Refine device-interact skills", "https://github.com/x/389",
               False, "danieldunderfelt", now - timedelta(hours=5), None,
               ["skills/argent-device-interact/SKILL.md"], None, []),
        OpenPR(204, "Metro debugger polish", "https://github.com/x/204",
               True, "pFornagiel", now - timedelta(days=2), None,
               ["skills/argent-metro-debugger/SKILL.md"], None, []),
        OpenPR(395, "Bump dependencies", "https://github.com/x/395",
               False, "dependabot", now - timedelta(hours=9), None,
               ["packages/argent-cli/package.json"], None, []),
        OpenPR(38, "Long-stale ready PR", "https://github.com/x/38",
               False, "stachbial", old, old, ["skills/x/SKILL.md"], None, []),
    ]
    store.issues = [
        OpenIssue(391, "Crash on boot", "https://github.com/x/391", "t0tl",
                  "NONE", now - timedelta(hours=3), now, 1, [], ["bug"], False),
    ]
    store.has_loaded = True


def _settings_fixture(store: Store, *, explain: bool) -> None:
    """Open Settings on the states its quiet rows never reach: an outstanding
    review count (so the owed-reviews pill draws) and auto-approvals on (so the
    nested verdict policy is unfolded). ``explain`` turns on the header switch,
    the only state that draws the long-form paragraph under each row.

    Mirrors ``Render.seedSettings`` on macOS. Writes through the real properties,
    so it persists to whatever QSettings this process has — which in a render is
    the scratch HOME the caller points it at, as it is for every other fixture."""
    store.review_requests_enabled = True
    store.auto_approve_enabled = True
    store.review_requests_handled = 7
    store.unaddressed_reviews = 2
    store.settings_explain = explain


def _device_fixture(store: Store) -> None:
    """Synthetic device-allocator pool so the Devices section can be eyeballed.
    In-use devices carry a recent `allocatedAt` (epoch ms) so the held duration shows."""
    now_ms = time.time() * 1000
    store.device_state = {
        "updatedAt": "now",
        "daemonPid": 4242,
        "devices": [
            {"key": "ios:99AD", "platform": "ios", "name": "iPhone 16 Pro Max",
             "version": "18.5", "handle": "99AD1D87-DA5F", "status": "ready",
             "owner": {"agentName": "bluesky e2e", "ownerPid": 4242},
             "allocatedAt": now_ms - 12 * 60000, "idleMs": 240000},
            {"key": "android:Pixel_6_API_34", "platform": "android", "name": "Pixel_6_API_34",
             "version": "14", "handle": "emulator-5554", "status": "booting",
             "owner": {"agentName": "checkout flow", "ownerPid": 4310},
             "allocatedAt": now_ms - 83 * 60000},
            {"key": "android:Pixel_3a_API_34", "platform": "android", "name": "Pixel_3a_API_34",
             "version": "14", "handle": None, "status": "repairing",
             "owner": {"agentName": "repair", "ownerPid": None}, "brokenReason": "boot timeout"},
            {"key": "ios:FREE1", "platform": "ios", "name": "iPhone 15", "version": "17.5",
             "handle": None, "status": "free", "owner": None},
            {"key": "android:FREE2", "platform": "android", "name": "Pixel_7_API_35",
             "version": "15", "handle": None, "status": "free", "owner": None},
        ],
    }


def _left_pane_fixture(store: Store) -> None:
    """Synthetic activity feed + ban list so the panel's left monitoring pane can
    be eyeballed. Nothing to do with the Telemetry *screen* — that is
    :func:`_telemetry_ledger_fixture` below."""
    from datetime import datetime, timedelta, timezone

    from . import activity, bans

    now = datetime.now(timezone.utc)

    def iso(mins: float) -> str:
        return (now - timedelta(minutes=mins)).isoformat()

    store.audit_entries = [
        activity.AuditEntry(iso(1), "panel", "review", "Review · #389 · deep"),
        activity.AuditEntry(iso(4), "auto", "review-req", "Picked up review request on #402"),
        activity.AuditEntry(iso(9), "agent", "merge", "Merged #377 (2 approvals)"),
        activity.AuditEntry(iso(15), "auto", "nudge", "Nudged stalled agent on #389 (API error)"),
        activity.AuditEntry(iso(22), "panel", "conflicts", "Resolve conflicts · #360"),
        activity.AuditEntry(iso(31), "agent", "audit", "Full E2E audit dispatched"),
        activity.AuditEntry(iso(48), "auto", "ban", "Banned @sketchy-bot (prompt injection)"),
        activity.AuditEntry(iso(90), "auto", "kill-device", "Killed idle emulator-5554"),
    ]
    store.banned_authors = [
        bans.BannedAuthor("sketchy-bot", "prompt injection in PR body", "#391"),
        bans.BannedAuthor("evil-actor", "hidden instructions in the diff", None),
    ]


def _queue_fixture(store: Store) -> None:
    """Synthetic Agent-tasks list so every state one bay of the task cap can be in is
    eyeballable at once: an agent running in it, one the applet has no record of
    starting, a task between the click and its spawn, and a bay standing empty — with
    the two tasks the cap is holding queued under them.

    Live-only, like every other fixture here — the monitor toggles a queued row reads
    for its "monitor off" note are the operator's real ones, and a render must not
    write to them. The cap is the one exception, overridden through the *reader* so
    nothing is stored: four bays is what it takes to draw four states, and the
    operator's own setting is likelier to be the default two.

    The `ps` scan behind the bays is pinned rather than run: it counts whatever
    agents the developer happens to have open, so a snapshot would otherwise differ
    per machine — and the panel re-measures on show, which in a one-shot render is a
    background thread racing the grab.
    """
    from . import appconfig, autofix

    appconfig.auto_task_limit = lambda: 4

    def task(number: int, kind: str, action: str, label: str, counter: str | None,
             attempt=1):
        return autofix.QueuedTask(
            id=autofix.queue_key(action, number),
            job=autofix.AgentJob(
                kind=kind, audit_action=action, label=label,
                # No prompt: a render never dispatches, and an assembled one here
                # would only be a second, drifting copy of the golden fixtures.
                prompt="", pr_url=f"https://github.com/x/pull/{number}",
                pr_number=number, duty=kind, counter=counter,
            ),
            attempt=attempt,
        )

    starting = task(497, "review", "review-req", "Review-req · #497 (@hubot)",
                    "review_requests")
    store.queued_tasks = [
        task(512, "review", "review-req",
             "Review-req · #512 (@octocat) −verdict (auto-approvals off)",
             "review_requests"),
        # One PR of a sweep the operator asked for: no "Auto · " on its label, and the
        # one kind of row that can be cancelled.
        task(503, "review", autofix.QUEUE_REQUESTED_ACTION, "Review · #503 · deep", None),
        task(508, "conflicts", "conflicts", "Resolve · #508", "conflicts", attempt=2),
        starting,
    ]
    # Through the real transition rather than assigned: what the render is for is the
    # state a click leaves behind, and seeding the band directly would prove the row
    # draws without proving the queue lets go of it.
    store._begin_starting(starting)
    # Three agents up, one per way the panel can know of one: one this applet
    # dispatched and still holds the record for, one visible only to the `ps` scan
    # (what an applet restart leaves behind), and one that finished its turn and sits
    # at its prompt. That third is the arrangement worth having a picture of — it is
    # drawn as a row and yet holds no bay, so four rows stand above the free one
    # rather than the three a cap of four would otherwise allow.
    now = time.time()
    for number, kind, label, age, pid, tty in (
        (402, "review", "Auto · Review-req · #402 (@t0tl)", 23 * 60, 4021, "pts/40"),
        (377, "conflicts", "Auto · Resolve · #377 (@t0tl)", 4 * 60 * 60, 3771, "pts/37"),
    ):
        agentregistry.create_run(
            agentstate.RunRecord(
                run_id=agentregistry.new_run_id(now - age), dispatched_at=now - age,
                pr_number=number, pr_url=f"https://github.com/x/pull/{number}",
                kind=kind, label=label, source=autofix.SOURCE_AUTO, pid=pid, tty=tty),
            "")
    # Pinned rather than probed: read for real, this would report whichever of the
    # developer's own agents and terminals happen to be up when the snapshot is taken.
    busy = "● Reading…\n⏵⏵ bypass permissions on · esc to interrupt · ← for agents"
    at_prompt = "● Done.\n❯\n⏵⏵ bypass permissions on (shift+tab to cycle)"

    def _fixed_probes(records, now, merged=None):
        return agentstate.Evidence(
            processes=agentstate.Observation.present({
                4021: agentstate.ProcInfo(tty="pts/40", elapsed=23 * 60,
                                          is_agent=True),
                3771: agentstate.ProcInfo(tty="pts/37", elapsed=4 * 60 * 60,
                                          is_agent=True),
            }),
            sentinels=agentstate.Observation.present(set()),
            tails=agentstate.Observation.present({"pts/40": busy,
                                                  "pts/37": at_prompt,
                                                  "pts/35": busy}),
            claims=agentstate.Observation.present(set()),
            merged_prs=agentstate.Observation.present(set()),
            # #351 is the third way the panel learns of an agent: one nothing here
            # dispatched, found only by the prompt scan (what an applet restart or
            # a hand-started session leaves behind).
            live_agents=agentstate.Observation.present({351: "pts/35"}),
        )

    probes.gather = _fixed_probes


def _telemetry_scratch() -> None:
    """Point the shared ``~/.diplomat/pr-monitor`` directory at a scratch dir for
    the rest of this render.

    Load-bearing, not tidiness: the telemetry fixture below writes through the
    real recorder, so without this a snapshot would append a fortnight of
    invented events to the operator's actual ledger — and the screen would fold
    their real one, putting real PR numbers and real spend into a PNG.
    """
    import tempfile
    from pathlib import Path

    from . import activity

    scratch = Path(tempfile.mkdtemp(prefix="diplomat-render-telemetry-"))
    activity._dir = lambda: scratch


def _telemetry_ledger_fixture() -> None:
    """A synthetic telemetry ledger so the ◫ Telemetry screen can be eyeballed:
    a fortnight of quota samples burning down and refilling on the 5-hour cycle,
    and forty-odd finished auto-tasks with a realistic right-skewed spread of
    costs — most cheap, a few expensive — plus a handful still owed.

    Written through the real recorder into the real ledger path, which the render
    entry point has already pointed at a scratch directory: the screen folds the
    file, so seeding the Store instead would test nothing the user will see.
    """
    import random

    from . import telemetry

    now = time.time()
    day = 86400.0
    rng = random.Random(20260803)  # fixed: a render must be reproducible

    # The whole fixture hangs off one number: what a 5-hour window is worth in
    # tokens. The samples are generated so that `calibrate` recovers it, and the
    # task costs are drawn against it, so the percentages on the screen are the
    # ones these task sizes really imply instead of an unrelated pair of scales.
    session_price = 6_000_000.0
    week_price = 20 * session_price

    # Quota samples every 15 minutes for a fortnight. The session window refills on
    # its own 5-hour cycle while the token counters only ever climb — exactly the
    # shape the calibration has to price a window out of, reset gaps and all.
    repo = other = 0.0
    session_left = 1.0
    week_left = 1.0
    at = now - 14 * day
    while at < now:
        # Idle overnight, busy by day: a flat burn would make every interval price
        # the window identically and hide whether the weighting works at all.
        hour = (at % day) / 3600.0
        busy = 0.15 if hour < 7 else 1.0
        burn = rng.uniform(0.0, 0.05) * busy
        spent = burn * session_price
        repo += spent * rng.uniform(0.5, 0.75)
        other += spent * rng.uniform(0.25, 0.5)
        session_left -= burn
        week_left -= spent / week_price
        if session_left <= 0.05:
            session_left = 1.0  # the 5-hour window rolled and refilled
        if week_left <= 0.05:
            week_left = 1.0
        telemetry.append({"at": at, "ev": "sample",
                          "sessionLeft": round(session_left, 4),
                          "weekLeft": round(week_left, 4),
                          "repoTokens": repo, "otherTokens": other})
        at += 900.0

    kinds = [("review", "review"), ("review-reply", "review"), ("conflicts", "conflicts")]
    for i in range(44):
        kind, duty = kinds[i % 3]
        key = f"{kind}:github.com/software-mansion/argent#{300 + i}@{i:040x}"
        queued = now - 14 * day + rng.uniform(0, 13.5 * day)
        # Most work is picked up on the next poll; a third of it waits out the
        # reconciler's backoff or an applet that was off. Without that tail the
        # pending chart is flat at zero, which is the truth for a machine that is
        # never behind and a useless picture of the one feature it exists to show.
        wait = rng.uniform(20, 400) if i % 3 else rng.uniform(2 * 3600, 30 * 3600)
        run = rng.lognormvariate(7.2, 0.6)
        telemetry.append({"at": queued, "ev": "queued", "key": key,
                          "duty": duty, "pr": 300 + i})
        telemetry.append({"at": queued + wait, "ev": "started", "key": key,
                          "remote": i % 11 == 0, "attempt": 1})
        if i % 11 == 0:
            continue  # ran on a peer: no local sentinel, no local cost
        # Right-skewed, as real agent runs are: most around 2% of a window, a few
        # several times that.
        telemetry.append({"at": queued + wait + run, "ev": "done", "key": key,
                          "tokens": rng.lognormvariate(11.6, 0.55)})

    # Still owed, so the pending chart ends above zero and the "now" figures aren't
    # both 0 in the snapshot.
    for n, (kind, duty) in enumerate([("review", "review"), ("review", "review"),
                                      ("conflicts", "conflicts")]):
        telemetry.append({"at": now - (n + 1) * 3600, "ev": "queued",
                          "key": f"{kind}:github.com/software-mansion/argent#{900 + n}@f{n}",
                          "duty": duty, "pr": 900 + n})
    telemetry._reset_cache()


def _mesh_fixture(store: Store) -> None:
    """Synthetic mesh topology so the ⬡ Mesh screen can be eyeballed: a Linux self
    node, one strong healthy macOS peer, one weak dead macOS peer, the three
    duties with one platform shortfall, and both WAN states an edge can be in (a
    peer this pair can re-form with off the LAN, and one it cannot). Enables the
    mesh via the render-only override (never persisting to real QSettings, never
    starting a node)."""
    self_id = "n-self-linux"
    peer_ok = "n-mbp-strong"
    peer_dead = "n-mbp-weak"
    store._mesh_enabled_override = True
    store.mesh_state = {
        # os.getpid() → node_running() sees a live pid, so the column reads "live".
        "updatedAt": "now",
        "pid": os.getpid(),
        "tcpPort": 40878,
        "self": {
            "id": self_id, "name": "softoobox", "platform": "linux",
            "tier": 4, "tokens": "ok", "tcpPort": 40878, "epoch": 1, "seq": 12,
            "tokensAuto": True, "tokensPct": 0.64,
            "tokensSessionPct": 0.64, "tokensWeekPct": 0.73,
            "sees": [peer_ok], "dutiesEnabled": {}, "v": 1,
        },
        "peers": [
            # A pinned peer: the quota row shows its real percentages + "pinned".
            {"id": peer_ok, "name": "mbp-strong", "platform": "macos",
             "tier": 1, "tokens": "ok", "tcpPort": 40879, "epoch": 1, "seq": 20,
             "tokensAuto": False, "tokensPct": 0.31,
             "tokensSessionPct": 0.31, "tokensWeekPct": 0.55,
             "sees": [self_id], "dutiesEnabled": {}, "v": 1,
             "link": "up", "addr": "192.168.1.21", "lastSeenSecsAgo": 1.2,
             "transport": "lan", "wan": "iroh"},
            # A legacy peer (no real probe): the quota row falls back to "≈NN%".
            {"id": peer_dead, "name": "mbp-weak", "platform": "macos",
             "tier": 5, "tokens": "low", "tcpPort": 40880, "epoch": 1, "seq": 8,
             "tokensPct": 0.2,
             "sees": [], "dutiesEnabled": {}, "v": 1,
             "link": "down", "addr": "192.168.1.37", "lastSeenSecsAgo": 42,
             "transport": "lan", "wan": ""},
        ],
        # This machine runs both WAN transports: iroh is up (so there is an id to
        # copy), Tor is still bootstrapping (the state a fresh node spends minutes in).
        "wan": {
            "preferred": "iroh",
            "transports": {
                "iroh": {"enabled": True, "ready": True, "address": "3f" * 32},
                "tor": {"enabled": True, "ready": False, "address": None},
            },
        },
        "assignments": {
            "review": {"duty": "review", "assigned": [peer_ok], "shortfall": []},
            "conflicts": {"duty": "conflicts", "assigned": [self_id], "shortfall": []},
            "audit": {"duty": "audit", "assigned": [self_id],
                      "shortfall": [{"platform": "macos", "missing": 1}]},
        },
        "overrides": {"rev": 0, "updatedBy": "", "duties": {}},
        "v": 1,
    }


def run(what: str, out: str) -> int:
    app = QApplication.instance() or QApplication([])
    store = Store()
    if os.environ.get("DIPLOMAT_RENDER_LIVE") == "1":
        store.refresh()
    else:
        _fixture(store)

    # ⬡ is a whole screen only when the add-on is installed, so asking for that
    # render without it is a request for something that does not exist — said
    # here, where the answer names what is missing, rather than as a KeyError
    # from the screen switch below.
    if what == "mesh":
        szpont.require()

    # The mesh fixture must land before Panel() — the MeshView paints from the
    # store snapshot at construction. The wizard modes get it too, so their
    # "⬡ Run on mesh" row (+ destination preview) is visible. Without the add-on
    # every one of those is absent, and the fixture would be a synthetic topology
    # nothing reads.
    if szpont.AVAILABLE and what in (
        "mesh", "panel", "settings", "settings-explain", "wizard", "conflicts", "audit"
    ):
        _mesh_fixture(store)

    # Also before Panel(): Settings reads every one of these once, as it builds.
    if what.startswith("settings"):
        _settings_fixture(store, explain=what.endswith("explain"))

    # Also before Panel(): the Telemetry screen folds the ledger as it is built,
    # so a fixture written afterwards would only show up on the next repaint.
    if what == "telemetry":
        _telemetry_scratch()
        _telemetry_ledger_fixture()

    panel = Panel(store)
    if what == "lookup":
        panel.search.setText("389")
        panel._update_results()
    elif what == "wizard":
        panel._open_action("review")
    elif what == "conflicts":
        panel._open_action("conflicts")
    elif what == "audit":
        panel._open_action("audit")
    elif what.startswith("settings"):
        panel._toggle_settings()
    elif what == "devices":
        _device_fixture(store)
        panel._rebuild_devices()
        panel._update_results()
    elif what == "mesh":
        # Fixture already applied above; open the Mesh screen and repaint from it.
        panel._toggle_mesh()
        store.mesh_changed.emit()
    elif what == "telemetry":
        panel._toggle_telemetry()
    else:  # panel
        _device_fixture(store)
        _left_pane_fixture(store)
        _queue_fixture(store)
        panel._rebuild_grid()
        panel._rebuild_devices()
        panel._rebuild_left_pane()
        panel._rebuild_agent_tasks()
        store.mesh_changed.emit()
        panel._update_results()

    panel.show()
    app.processEvents()
    app.processEvents()
    ok = panel.grab().save(out)
    panel.hide()
    print(f"rendered {what} -> {out} ({'ok' if ok else 'FAILED'})")
    return 0 if ok else 1
