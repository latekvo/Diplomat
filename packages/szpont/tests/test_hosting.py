"""Registering a host from functions.

The load-bearing test here is the last group: a model this package builds is fed
to the library's own resolver and read back through it. A helper that emits a
placement the library silently skips would still look correct from the outside -
the duty would exist, it would just staff one machine instead of the platforms
asked for - so the assertion has to be what the library *resolved*, never what
the helper returned.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from szpontnet import config, host as _host, spawnjob

import szpont
from szpont import NoRunner, build_host, duty_model, register_host, unregister_host


# ---- the defaults are real answers ----------------------------------------


def test_a_host_that_answers_nothing_is_the_librarys_own_node():
    register_host()

    assert _host.model() == _host.netmodel()
    assert config.duty_ids() == ["review", "conflicts", "audit"]


def test_no_runner_still_declines_rather_than_pretending():
    """"This machine cannot take the job" is a correct answer, and the one that
    makes the dispatcher fail over to the next candidate."""
    register_host(log=lambda action, detail: None)

    with pytest.raises(NoRunner):
        _host.host().run_job("prompt", None)


def test_the_no_runner_decline_is_the_one_the_dispatcher_handles():
    """It has to be a JobSpawnError, or the fail-over path does not catch it and
    a machine with no runner takes down the dispatch instead of passing it on."""
    assert issubclass(NoRunner, spawnjob.JobSpawnError)


def test_an_omitted_answer_falls_through_and_a_given_one_does_not(tmp_path):
    register_host(state_dir=tmp_path / "elsewhere")

    assert _host.host().state_dir() == tmp_path / "elsewhere"
    assert _host.host().work_already_running("anything") is False


# ---- answers as functions and as values -----------------------------------


def test_an_answer_can_be_a_function_or_the_value_itself(tmp_path):
    from_value = build_host(state_dir=tmp_path / "a")
    from_callable = build_host(state_dir=lambda: tmp_path / "b")

    assert from_value.state_dir() == tmp_path / "a"
    assert from_callable.state_dir() == tmp_path / "b"


def test_a_state_dir_given_as_a_string_still_arrives_as_a_path(tmp_path):
    """Everything downstream builds paths off this answer with ``/``, which a
    string does not support."""
    host = build_host(state_dir=str(tmp_path / "as-text"))

    assert isinstance(host.state_dir(), Path)
    assert host.state_dir() == tmp_path / "as-text"


def test_run_job_receives_the_prompt_and_the_completion_sentinel():
    seen = []
    register_host(run_job=lambda prompt, done: seen.append((prompt, done)) or "handle")

    assert _host.host().run_job("do the thing", "/tmp/done") == "handle"
    assert seen == [("do the thing", "/tmp/done")]


def test_log_lines_reach_the_function_that_asked_for_them():
    lines = []
    register_host(log=lambda action, detail: lines.append((action, detail)))

    _host.log("peer-up", "tower")
    assert lines == [("peer-up", "tower")]


def test_work_already_running_is_asked_and_its_answer_coerced():
    asked = []
    register_host(work_already_running=lambda key: asked.append(key) or "truthy")

    assert _host.work_already_running("review:o/r#1") is True
    assert asked == ["review:o/r#1"]


def test_a_host_that_raises_never_costs_the_node_the_job():
    """The library fails this question open on purpose - a duplicate agent is
    recoverable and silently dropped work is not. Registering through this
    package must not change that."""
    def boom(_key):
        raise RuntimeError("the application is broken")

    register_host(work_already_running=boom)
    assert _host.work_already_running("anything") is False


# ---- the duty model -------------------------------------------------------


def test_a_named_duty_catalog_replaces_the_canonical_one():
    """Wholesale, not merged: a deployment that names its duties means those, not
    those plus review/conflicts/audit."""
    register_host(duties=["render", "encode"])

    assert config.duty_ids() == ["render", "encode"]


def test_a_spread_survives_the_librarys_own_parser():
    """The regression this file exists for. A spread entry has to be a
    ``{"platform", "count"}`` object; the library skips anything else, so a pair
    emitted as a two-element list would not be rejected - the duty would resolve
    with no spread at all and quietly staff one machine."""
    register_host(model=duty_model(["bundle"], spread=[("linux", 1), ("macos", 2)]))

    assert config.placement_for("bundle").spread == (("linux", 1), ("macos", 2))


def test_a_duty_with_no_spread_asks_for_one_node():
    register_host(duties=["render"])

    assert config.placement_for("render").spread == ()
    assert config.placement_for("render").token_aware is True


def test_token_awareness_is_carried_through():
    register_host(model=duty_model(["render"], token_aware=False))

    assert config.placement_for("render").token_aware is False


def test_a_duty_without_a_strategy_inherits_the_meshs_default():
    """Naming one per duty would pin every deployment to surplus-first even after
    the model's default moves."""
    register_host(duties=["render"])

    assert config.placement_for("render").strategy == _host.model()["defaultStrategy"]


def test_each_duty_gets_its_own_placement():
    """Sharing one dict between duties makes editing one duty's spread edit them
    all, which is the kind of thing that is only noticed in production."""
    model = duty_model(["a", "b"], spread=[("linux", 1)])
    model["duties"][0]["placement"]["spread"][0]["count"] = 9

    assert model["duties"][1]["placement"]["spread"] == [{"platform": "linux", "count": 1}]


def test_duties_and_model_are_not_both_accepted():
    """One is shorthand for the other. Silently letting one win would leave a
    caller looking at a duty catalog they did not ask for."""
    with pytest.raises(ValueError, match="not both"):
        build_host(duties=["a"], model={"duties": []})


# ---- registration lifecycle ------------------------------------------------


def test_registering_puts_the_host_behind_the_node_and_returns_it():
    host = register_host(duties=["render"])

    assert _host.host() is host
    assert isinstance(host, szpont.Host)


def test_the_model_cache_is_dropped_when_a_host_arrives():
    """The resolved model is cached because it is read on every placement and
    every advert. A registration that landed after something read one would leave
    the node running the library's catalog while the host believed its own."""
    assert config.duty_ids() == ["review", "conflicts", "audit"]

    register_host(duties=["render"])
    assert config.duty_ids() == ["render"]


def test_unregistering_goes_back_to_the_librarys_defaults():
    register_host(duties=["render"])
    unregister_host()

    assert config.duty_ids() == ["review", "conflicts", "audit"]
    assert _host.host().state_dir() == Path.home() / ".szpontnet"


def test_registering_again_replaces_rather_than_stacks():
    register_host(duties=["first"])
    register_host(duties=["second"])

    assert config.duty_ids() == ["second"]
