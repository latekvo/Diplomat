"""Diplomat's answers to the six questions a SzpontNet node asks its host.

The library ships a working default for every one of them, which is what makes
this worth testing: a hook that silently stops being wired does not crash, it just
quietly reverts to "nobody is home" — a node on the canonical duty catalog,
keeping its state somewhere else, logging into the void. Each test here pins one
answer to Diplomat's, and the first pins the wiring itself.
"""

from __future__ import annotations

import json
import platform
from pathlib import Path

import pytest

from diplomat_runtime import activity, core, review, szponthost
from szpontnet import config as mesh_config
from szpontnet import host as szpont_host
from szpontnet import spawnjob

# ``conftest.no_host_agent_spawn`` swaps this out for a refusing stub; the macOS
# runner's own tests stub ``popen_detached`` underneath it instead, so they need
# the real callable, captured before the autouse fixture runs.
_real_spawn_macos = szponthost._spawn_macos


@pytest.fixture
def host():
    return szponthost.DiplomatHost()


# ---- the wiring ----------------------------------------------------------


def test_importing_diplomat_puts_it_behind_the_node():
    """``diplomat_app/__init__`` installs the host. Without it every hook below
    silently reverts to the library's default and nothing raises."""
    assert isinstance(szpont_host.host(), szponthost.DiplomatHost)


def test_the_effective_model_is_diplomats_duty_catalog(host):
    """The overlay actually reaches the library: the duties the node places are
    the ones ``assets/mesh.json`` names, with the presentation fields the panels
    render, not the bare ids the library ships."""
    catalog = {d["id"]: d for d in core.mesh()["duties"]}
    assert mesh_config.duty_ids() == list(catalog)
    for duty_id, duty in catalog.items():
        assert mesh_config.duty_by_id(duty_id) == duty
    assert "emoji" in mesh_config.duty_by_id("review")


def test_the_overlay_keeps_the_librarys_wire_constants(host):
    """One level of merge, both directions. Diplomat names duties and says nothing
    about the wire or the quota model, so those still come from ``netmodel.json`` —
    which is also the assertion that they are not restated in two files."""
    overlay = host.model()
    for key in ("protocol", "accounts", "dispatchStrategy"):
        assert key not in overlay, f"{key} belongs to the library, not the overlay"
        assert key in szpont_host.netmodel()
    assert mesh_config.protocol()["multicastPort"] == \
        szpont_host.netmodel()["protocol"]["multicastPort"]
    assert mesh_config.dispatch_strategy() == szpont_host.netmodel()["dispatchStrategy"]


# ---- state_dir: the one answer that cannot change -------------------------


def test_state_dir_is_where_this_machines_node_already_lives(host):
    """Every peer's trust allowlist is keyed to the device keypair in this
    directory. Adopting the library's own default would mint a fresh key and make
    the machine a stranger to its own fleet."""
    assert host.state_dir() == Path.home() / ".diplomat" / "mesh"
    assert host.state_dir() != szpont_host.Host().state_dir()


def test_the_node_resolves_its_state_dir_through_the_host(monkeypatch, tmp_path):
    """The hook is not decorative — it is what ``identity.mesh_dir`` returns when
    no explicit override is set."""
    from szpontnet import identity

    monkeypatch.delenv("SZPONTNET_DIR", raising=False)
    assert identity.mesh_dir() == szponthost.DiplomatHost().state_dir()

    monkeypatch.setenv("SZPONTNET_DIR", str(tmp_path))
    assert identity.mesh_dir() == tmp_path  # an explicit override still wins


# ---- log -----------------------------------------------------------------


def test_log_lands_in_the_shared_activity_feed(host):
    """Under the ``mesh`` source, so the panel's activity screen groups node
    events with everything else it shows."""
    host.log("mesh-up", "Mesh node up: testbox")

    entries = [json.loads(line) for line in
               activity.audit_path().read_text().splitlines() if line.strip()]
    assert entries[-1]["source"] == "mesh"
    assert entries[-1]["action"] == "mesh-up"
    assert entries[-1]["detail"] == "Mesh node up: testbox"


def test_the_node_narrates_through_the_host(monkeypatch):
    """``szpontnet.host.log`` is the spelling the node uses; it has to reach the
    installed host rather than the null one."""
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(szponthost.DiplomatHost, "log",
                        lambda self, a, d: seen.append((a, d)))
    szpont_host.log("mesh-peer-up", "saw a peer")
    assert seen == [("mesh-peer-up", "saw a peer")]


# ---- run_job -------------------------------------------------------------


def test_run_job_uses_the_applets_terminal_spawner_on_linux(host, monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(review, "spawn",
                        lambda prompt, term, done_path=None: calls.append(
                            (prompt, term, done_path)) or "/tmp/p.txt")

    assert host.run_job("do a review", "/tmp/done") == "/tmp/p.txt"
    assert calls == [("do a review", None, "/tmp/done")]


def test_run_job_opens_a_terminal_through_osascript_on_macos(host, monkeypatch, tmp_path):
    launched: list[list] = []
    monkeypatch.setattr(szponthost, "_spawn_macos", _real_spawn_macos)
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(review, "repo_path", lambda: str(tmp_path))
    monkeypatch.setattr(review, "write_prompt", lambda prompt: str(tmp_path / "p.txt"))
    monkeypatch.setattr(review, "popen_detached", lambda argv: launched.append(argv))

    assert host.run_job("do a review", None) == str(tmp_path / "p.txt")
    assert launched[0][0] == "osascript"
    assert "Terminal" in launched[0][2]


def test_a_machine_that_cannot_open_a_terminal_declines_the_job(host, monkeypatch):
    """The failure has to arrive as the library's own :class:`JobSpawnError`, or
    the dispatcher reads "taken" and never fails over to the next candidate."""
    monkeypatch.setattr(platform, "system", lambda: "Linux")

    def refuse(*args, **kwargs):
        raise review.SpawnError("no terminal emulator found")

    monkeypatch.setattr(review, "spawn", refuse)

    with pytest.raises(spawnjob.JobSpawnError) as exc:
        host.run_job("do a review", None)
    assert "no terminal emulator" in str(exc.value)


def test_the_node_reaches_the_host_runner_when_no_template_is_set(monkeypatch):
    """``spawn_job`` only falls through to the host when the operator configured
    no ``SZPONTNET_SPAWN``; that fall-through is the whole integration."""
    monkeypatch.delenv("SZPONTNET_SPAWN", raising=False)
    seen: list[tuple] = []
    monkeypatch.setattr(szponthost.DiplomatHost, "run_job",
                        lambda self, prompt, done: seen.append((prompt, done)) or "/p")

    assert spawnjob.spawn_job("work", done_path="/d") == "/p"
    assert seen == [("work", "/d")]


# ---- work_already_running ------------------------------------------------


def _ps_output(monkeypatch, text: str):
    class Result:
        stdout = text

    monkeypatch.setattr(szponthost.subprocess, "run", lambda *a, **k: Result())


def test_a_live_agent_on_the_same_pr_is_reported(host, monkeypatch):
    _ps_output(monkeypatch, "claude review PR #7 owner/repo\n")
    monkeypatch.setattr("diplomat_runtime.autofix.live_pr_numbers",
                        lambda out, owner, repo: {7})
    assert host.work_already_running("review:github.com/owner/repo#7@abc123") is True


def test_a_different_pr_is_not(host, monkeypatch):
    _ps_output(monkeypatch, "claude review PR #9 owner/repo\n")
    monkeypatch.setattr("diplomat_runtime.autofix.live_pr_numbers",
                        lambda out, owner, repo: {9})
    assert host.work_already_running("review:github.com/owner/repo#7@abc123") is False


def test_an_unparseable_work_key_is_not_a_match(host):
    """A key from a newer/other implementation must read as "not seen", not raise
    into the executor's spawn path."""
    assert host.work_already_running("something-else-entirely") is False


def test_it_fails_open_on_undecodable_ps_output(host, monkeypatch):
    """The floor promises to FAIL OPEN like ``Store._live_pr_agents`` — a ps error
    reads as "not seen" so a transient failure never drops work. ``ps -Ao args=``
    under ``text=True`` decodes strict UTF-8, so any process on the box with a
    non-UTF-8 byte in its argv makes the output undecodable and raises
    UnicodeDecodeError — a ValueError, NOT an OSError/SubprocessError. Uncaught it
    escapes the guard, up through ``_spawn_local`` → ``_take_job``, tearing the
    dispatching peer's link (or failing a self-dispatch)."""
    def boom(*a, **k):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(szponthost.subprocess, "run", boom)
    assert host.work_already_running("review:github.com/owner/repo#7@abc123") is False


def test_a_host_that_raises_never_costs_the_node_the_job(monkeypatch):
    """The library's own guard around the hook: a broken second opinion must fail
    open, because a duplicate agent is recoverable and silently declined work is
    not."""
    def boom(self, work_key):
        raise RuntimeError("host is confused")

    monkeypatch.setattr(szponthost.DiplomatHost, "work_already_running", boom)
    assert szpont_host.work_already_running("review:github.com/o/r#1@sha") is False


# ---- at_job_capacity -----------------------------------------------------


def _agents_on(monkeypatch, prs: set[int]):
    """Pretend ``ps`` shows a live agent for each of ``prs`` (the matcher itself is
    covered by ``live_pr_numbers``' own tests)."""
    _ps_output(monkeypatch, "".join(f"claude … PR #{n} in o/r\n" for n in prs))
    monkeypatch.setattr("diplomat_runtime.autofix.live_pr_numbers",
                        lambda out, owner, repo: set(prs))


def test_a_busy_machine_declines_peer_routed_work(host, monkeypatch):
    """The applet caps the work it originates; this is the other half. Without it a
    peer ranks us best-surplus (quota, not concurrency) and lands job after job on a
    machine that is already running its cap."""
    _agents_on(monkeypatch, {1, 2})
    assert host.at_job_capacity([]) is True
    _agents_on(monkeypatch, {1})
    assert host.at_job_capacity([]) is False


def test_the_nodes_own_fresh_jobs_count_before_ps_can_see_them(host, monkeypatch):
    """A `claude` started seconds ago is not in `ps` yet, and a burst of dispatches
    is exactly when that gap decides whether the cap holds — so the node hands its
    own live work keys in."""
    _agents_on(monkeypatch, set())
    keys = ["review:github.com/o/r#5@aa", "conflicts:github.com/o/r#6@bb"]
    assert host.at_job_capacity(keys) is True
    # …and the same job seen from both sides is one job, not two.
    _agents_on(monkeypatch, {5})
    assert host.at_job_capacity(["review:github.com/o/r#5@aa"]) is False


def test_the_cap_is_the_one_the_applet_writes(host, monkeypatch):
    """Same file, same number: a device with two answers to "how many at once" has
    no cap at all."""
    from diplomat_runtime import appconfig

    _agents_on(monkeypatch, {1, 2})
    appconfig.set_int(appconfig.AUTO_TASK_LIMIT, 3)
    assert host.at_job_capacity([]) is False
    appconfig.set_int(appconfig.AUTO_TASK_LIMIT, 2)
    assert host.at_job_capacity([]) is True


def test_capacity_fails_open_on_an_unreadable_ps(host, monkeypatch):
    """Same trade as ``work_already_running``: a cap whose accounting broke must not
    become a node that refuses everything forever."""
    def boom(*a, **k):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(szponthost.subprocess, "run", boom)
    assert host.at_job_capacity([]) is False


def test_a_capacity_hook_that_raises_never_costs_the_node_the_job(monkeypatch):
    def boom(self, running_keys):
        raise RuntimeError("host is confused")

    monkeypatch.setattr(szponthost.DiplomatHost, "at_job_capacity", boom)
    assert szpont_host.at_job_capacity([]) is False
