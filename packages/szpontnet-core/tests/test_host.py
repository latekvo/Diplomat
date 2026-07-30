"""The host seam: what a node does with nobody behind it, and how one gets there.

This is the whole of the library's independence, so it is worth being blunt about
what each default *means*. A node with no host is not a broken node — it runs the
canonical v1 model, keeps its state under its own name, discards its narration and
declines work it has no runner for. Every one of those is a correct answer to
"nobody is home", and none of them is allowed to quietly become the answer when
somebody *is*.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from szpontnet import config, host, identity, spawnjob

_RENDER_DUTY = {"id": "render", "placement": {"tokenAware": False, "spread": []}}


# ---- the defaults --------------------------------------------------------


def test_a_node_with_no_host_runs_the_canonical_model():
    assert host.model() == host.netmodel()
    assert config.duty_ids() == [d["id"] for d in host.netmodel()["duties"]]


def test_the_canonical_model_is_the_v1_catalog():
    """Appendix B, so a bare node is conformant out of the box rather than
    advertising no duties and never taking work."""
    assert config.duty_ids() == ["review", "conflicts", "audit"]
    assert config.placement_for("audit").spread == (("linux", 1), ("macos", 1))
    assert config.tier_bounds() == (1, 5, 3)
    assert config.default_trust() == "foreign"


def test_state_lives_under_the_librarys_own_name(monkeypatch):
    monkeypatch.delenv("SZPONTNET_DIR", raising=False)
    assert host.Host().state_dir() == Path.home() / ".szpontnet"
    assert identity.mesh_dir() == Path.home() / ".szpontnet"


def test_narration_goes_nowhere():
    """A library is not entitled to pick a log file on a machine it knows nothing
    about, so the default sink is no sink — and it must not raise either."""
    host.log("mesh-up", "a node came up")


def test_a_machine_with_no_runner_declines_the_job(monkeypatch):
    """Not an error condition: it is how a node says "not me", and it is what makes
    the dispatcher fail over to the next candidate."""
    monkeypatch.delenv("SZPONTNET_SPAWN", raising=False)
    with pytest.raises(spawnjob.JobSpawnError):
        spawnjob.spawn_job("do a thing")


def test_a_node_with_no_host_dedups_on_its_own_book_alone():
    assert host.work_already_running("review:github.com/o/r#1@sha") is False


# ---- registering in-process ----------------------------------------------


class _Recording(host.Host):
    def __init__(self):
        self.logged: list[tuple[str, str]] = []
        self.jobs: list[tuple[str, str | None]] = []

    def model(self):
        return {"duties": [_RENDER_DUTY]}

    def state_dir(self):
        return Path("/var/tmp/somewhere-else")

    def log(self, action, detail):
        self.logged.append((action, detail))

    def run_job(self, prompt, done_path):
        self.jobs.append((prompt, done_path))
        return "/staged/prompt.txt"

    def work_already_running(self, work_key):
        return work_key.startswith("render:")


def test_a_registered_host_answers_all_five(monkeypatch):
    monkeypatch.delenv("SZPONTNET_DIR", raising=False)
    monkeypatch.delenv("SZPONTNET_SPAWN", raising=False)
    impl = _Recording()
    host.set_host(impl)

    assert config.duty_ids() == ["render"]
    assert identity.mesh_dir() == Path("/var/tmp/somewhere-else")
    host.log("mesh-up", "hello")
    assert impl.logged == [("mesh-up", "hello")]
    assert spawnjob.spawn_job("work", done_path="/d") == "/staged/prompt.txt"
    assert impl.jobs == [("work", "/d")]
    assert host.work_already_running("render:job") is True


def test_registering_late_still_takes_effect():
    """The resolved model is cached — it is read on every placement resolution and
    every advert — so registering after something has already asked must drop that
    cache, or the node runs on the library's catalog while the host believes its
    own is in force."""
    assert config.duty_ids() == ["review", "conflicts", "audit"]  # warm the cache
    host.set_host(_Recording())
    assert config.duty_ids() == ["render"]


def test_reset_puts_the_defaults_back():
    host.set_host(_Recording())
    host.reset_host()
    assert config.duty_ids() == ["review", "conflicts", "audit"]
    assert isinstance(host.host(), host.Host)


def test_the_suites_own_isolation_hands_a_registered_host_back(host_isolation):
    """This suite runs every test against a bare node, which means taking the host
    away from whatever registered one — and an application registers its host once,
    at import, so there is no second chance to put it back.

    Pinned here because the damage is silent and elsewhere: blanking instead of
    restoring only shows up in a session that runs both suites, as failures in the
    *other* one, on tests that never mention the host.
    """
    impl = _Recording()
    host.set_host(impl)

    with host_isolation():
        assert type(host.host()) is host.Host
        assert config.duty_ids() == ["review", "conflicts", "audit"]

    assert host.host() is impl
    assert config.duty_ids() == ["render"]


# ---- the model overlay ---------------------------------------------------


def _host_returning(overlay: dict) -> host.Host:
    impl = host.Host()
    impl.model = lambda: overlay  # type: ignore[method-assign]
    return impl


def test_an_overlay_merges_dicts_one_level_deep():
    """A deployment that relabels the tiers keeps the library's bounds; it did not
    mean to delete them by not mentioning them."""
    host.set_host(_host_returning({"tiers": {"labels": {"1": "Beefy"}}}))
    assert config.tier_label(1) == "Beefy"
    assert config.tier_bounds() == (1, 5, 3)


def test_an_overlay_replaces_a_list_wholesale():
    """A deployment that names its duties means *those* duties, not those plus
    ours — anything else silently routes work classes it never asked for."""
    host.set_host(_host_returning({"duties": [_RENDER_DUTY]}))
    assert config.duty_ids() == ["render"]


def test_an_empty_overlay_changes_nothing():
    host.set_host(_host_returning({}))
    assert host.model() == host.netmodel()


def test_the_library_keeps_what_the_overlay_is_silent_about():
    host.set_host(_host_returning({"duties": []}))
    netmodel = host.netmodel()
    assert config.protocol()["multicastPort"] == netmodel["protocol"]["multicastPort"]
    assert config.job_cost_units() == netmodel["accounts"]["jobCostUnits"]


# ---- registering out-of-process (SZPONTNET_HOST) --------------------------


def _install_module(monkeypatch, name: str, **attrs) -> None:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)


def test_the_env_names_a_module_whose_factory_is_used(monkeypatch):
    """How an application that *spawns* a node gets behind it: the node is a
    separate process, so there is nothing to call ``set_host`` on."""
    impl = _Recording()
    _install_module(monkeypatch, "fake_host_ok", host=lambda: impl)
    monkeypatch.setenv("SZPONTNET_HOST", "fake_host_ok")

    assert host.host() is impl


def test_an_explicit_registration_beats_the_env(monkeypatch):
    impl = _Recording()
    _install_module(monkeypatch, "fake_host_env", host=lambda: _Recording())
    monkeypatch.setenv("SZPONTNET_HOST", "fake_host_env")
    host.set_host(impl)

    assert host.host() is impl


@pytest.mark.parametrize("case,attrs", [
    ("no such module", None),
    ("module with no factory", {}),
    ("factory raises", {"host": lambda: (_ for _ in ()).throw(RuntimeError("boom"))}),
    ("factory returns the wrong thing", {"host": lambda: "not a host"}),
])
def test_a_broken_host_module_leaves_the_node_running(monkeypatch, case, attrs):
    """A node that runs and declines is a better failure than a node that won't
    start — and the operator finds out the moment it is handed work, rather than
    from a traceback at boot on a machine they are not looking at."""
    if attrs is not None:
        _install_module(monkeypatch, "fake_host_broken", **attrs)
        monkeypatch.setenv("SZPONTNET_HOST", "fake_host_broken")
    else:
        monkeypatch.setenv("SZPONTNET_HOST", "no.such.module.anywhere")

    resolved = host.host()
    assert type(resolved) is host.Host, case
    assert config.duty_ids() == ["review", "conflicts", "audit"]


def test_an_empty_env_value_is_no_host(monkeypatch):
    monkeypatch.setenv("SZPONTNET_HOST", "   ")
    assert type(host.host()) is host.Host


# ---- the guard around the dedup hook -------------------------------------


def test_a_host_that_raises_never_costs_the_node_the_job():
    """Fails open: a duplicate agent is recoverable, silently declined work is
    not."""
    impl = host.Host()
    impl.work_already_running = lambda wk: (_ for _ in ()).throw(RuntimeError("confused"))
    host.set_host(impl)

    assert host.work_already_running("review:github.com/o/r#1@sha") is False


def test_a_host_runner_that_raises_arrives_as_a_decline(monkeypatch):
    """Anything a host raises has to reach the node as "can't take it", or it
    escapes into the peer link that delivered the dispatch and takes the session
    down with it."""
    monkeypatch.delenv("SZPONTNET_SPAWN", raising=False)
    impl = host.Host()
    impl.run_job = lambda prompt, done: (_ for _ in ()).throw(ValueError("nope"))
    host.set_host(impl)

    with pytest.raises(spawnjob.JobSpawnError) as exc:
        spawnjob.spawn_job("work")
    assert "nope" in str(exc.value)
