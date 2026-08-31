"""``DIPLOMAT_AGENTS=1 python -m diplomat_app`` — why the applet thinks what it thinks.

The point of this whole subsystem is that a wrong verdict about an agent should be
diagnosable rather than argued about. So one command prints the entire chain, in the
order the resolver walks it: what is registered, what each probe answered, what every
run resolved to and the single fact that decided it.

It runs the real probes against the real machine and reads the real registry, but it
resolves rather than acts: nothing is retired, nothing is written, nothing is spawned.
Safe to run beside a live applet.
"""

from __future__ import annotations

import time

from diplomat_runtime import agentregistry, agentstate, apiwatch
from . import probes


def run() -> int:
    now = time.time()
    records = agentregistry.adopt_pids(agentregistry.load())
    evidence = probes.gather(records, now, tokens=probes.tokens_left())
    limit, deadline = _limit(), _deadline()
    t = agentstate.tick(records, evidence, now, limit, deadline)

    print(f"registry: {agentregistry.runs_path()}")
    cutoff = ("off" if deadline is None
              else f"give up after {apiwatch.human_interval(deadline)}")
    print(f"{len(records)} registered run(s), cap {limit}, {cutoff}\n")

    print("PROBES")
    for name, obs in (("processes", evidence.processes),
                      ("sentinels", evidence.sentinels),
                      ("screens", evidence.tails),
                      ("mesh claims", evidence.claims),
                      ("merged PRs", evidence.merged_prs),
                      ("agent scan", evidence.live_agents),
                      ("agent sessions", evidence.sessions),
                      ("token budget", evidence.tokens_left)):
        print(f"  {name:<15} {_describe(obs)}")
    read, seen = probes.marker_stats()
    if read:
        markers = " / ".join(f"“{m}”" for m in apiwatch.BUSY_MARKERS)
        print(f"  {'busy marker':<15} seen on {seen} of {read} screen(s) read "
              f"({markers})")
    print()

    print("RUNS")
    if not t.rows:
        print("  (none)")
    for r, s in t.rows:
        who = r.label or (f"#{r.pr_number}" if r.pr_number else r.run_id)
        where = "" if r.placement == agentstate.PLACEMENT_LOCAL else f" [{r.placement}]"
        age = f"{(now - r.dispatched_at) / 60:.0f}m" if not r.untracked else "?"
        print(f"  {s.state:<14} {who}{where}")
        print(f"  {'':<14}   because: {s.reason}")
        print(f"  {'':<14}   run {r.run_id} · pid {r.pid or '-'} · tty {r.tty or '-'} "
              f"· {r.source} · up {age}")
    print()

    print("ANSWERS")
    print(f"  bays held        {len(t.cap_load)} of {limit}  {sorted(t.cap_load)}")
    print(f"  free slots       {t.free_slots}")
    print(f"  retirable now    {sorted(r.run_id for r in t.retirable)}")
    # The subset whose window goes with the record — "why did my terminal close".
    print(f"  windows reaped   {sorted(r.run_id for r in t.reapable)}")
    prs = sorted({r.pr_number for r in t.records if r.pr_number is not None})
    if prs:
        blocked = [pr for pr in prs if t.in_flight(pr)]
        print(f"  PRs read as in-flight  {blocked}")
    return 0


def _describe(obs: agentstate.Observation) -> str:
    """One probe's answer, with the size of it — an empty PRESENT and an UNAVAILABLE
    look alike in a summary, and telling them apart is the whole exercise."""
    if obs.status == agentstate.PRESENT:
        if not hasattr(obs.value, "__len__"):
            return f"ok ({obs.value})"  # a scalar answer — count it and it reads "1 item"
        n = len(obs.value)
        return f"ok ({n} item{'' if n == 1 else 's'})"
    return f"{obs.status.upper()} — {obs.reason or 'no reason given'}"


def _limit() -> int:
    """The configured cap, read without building a Store (which would start timers)."""
    from diplomat_runtime import appconfig
    return appconfig.auto_task_limit()


def _deadline() -> float | None:
    """The configured run deadline, or None with the backstop switched off. Read the
    same Store-free way the cap is."""
    from diplomat_runtime import appconfig
    return appconfig.run_deadline()
