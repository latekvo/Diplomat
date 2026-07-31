"""Foreign zero-trust execution: computing for a stranger, and holding one to
what it accepted.

Two contracts meet here. A machine asked to run an untrusted request never acts
under its own identity — it computes in the operator's sandbox and hands the
artifact back, and the *originator* performs any social action itself. And an
acceptance is the only promise a foreign device ever makes, so the originator
holds it to a deadline: deliver, plead for more time and convince a decider, or
be banned.

The sandbox itself is the operator's responsibility and deliberately outside the
protocol, so most tests here replace the confinement runner with one the test can
drive by hand. One test does not: it runs a real one, so the whole path — staged
prompt, scrubbed child, result file, signed return, handler under the
originator's identity — is proven end to end at least once.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from szpontnet import protocol, spawnjob


def _sandbox(monkeypatch) -> list[tuple[str, str]]:
    """Replace the confinement runner with a recorder the test drives.

    Returns the list it records ``(prompt, result_file)`` into — writing that
    file is how a test says "the sandbox finished".
    """
    calls: list[tuple[str, str]] = []

    def fake_spawn_confined(prompt: str, result_file: str) -> str:
        calls.append((prompt, result_file))
        return "/sim/staged-prompt.txt"

    monkeypatch.setattr(spawnjob, "spawn_confined", fake_spawn_confined)
    return calls


async def _confined_dispatch(simnet, a, b, sandbox, prompt="compute this",
                             **kw) -> str:
    """A→B under zero trust; returns the job id once B's sandbox is running."""
    results = await a.dispatch("review", prompt, target=b.id, **kw)
    assert results[0]["status"] == "spawned", results
    await simnet.until(lambda: bool(sandbox), 3.0, "the sandbox never started")
    return simnet.frames("dispatch", src=a, dst=b)[-1].payload()["job"]["id"]


# MARK: - the confined path


def test_a_foreign_request_runs_sandboxed_and_returns_its_artifact(
        simnet, monkeypatch):
    """The whole zero-trust shape in one run: B never acts, it computes and
    answers; A acts on the answer under its own identity."""
    sandbox = _sandbox(monkeypatch)

    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b", trust="foreign", FOREIGN_SPAWN="sandbox {prompt_file}")
        await simnet.linked(a, b)
        await simnet.all_verified(a, b)
        assert b.trust_of(a) == "foreign"

        job_id = await _confined_dispatch(simnet, a, b, sandbox, "review my PR")
        assert "review my PR" in sandbox[0][0]
        assert b.jobs == [], "a foreign request must never reach the host runner"

        Path(sandbox[0][1]).write_text("the computed artifact", encoding="utf-8")
        await simnet.until(lambda: simnet.frames("job-result", src=b, dst=a), 5.0,
                           "the artifact was never returned")
        result = simnet.frames("job-result", src=b, dst=a)[-1].payload()
        assert result["id"] == job_id and result["node"] == b.id
        assert result["result"]["output"] == "the computed artifact"
        assert result["result"]["ok"] is True

        # The originator acknowledges it, and only then stops hearing about it.
        await simnet.until(lambda: simnet.frames("job-ack", src=a, dst=b), 5.0,
                           "the originator never acknowledged the result")
        await simnet.until(lambda: not b.node._pending_results, 5.0,
                           "the executor kept the result pending after the ack")

    simnet.run(scenario())


def test_the_returned_artifact_is_bound_to_the_executors_key(simnet, monkeypatch):
    """A result decides what the originator does under its own identity, so a
    third peer on the link — or a tamper in flight — must not be able to write
    it. A forged one is dropped, and pointedly NOT acked."""
    sandbox = _sandbox(monkeypatch)

    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b", trust="foreign", FOREIGN_SPAWN="sandbox {prompt_file}")
        c = await simnet.node("c")
        await simnet.linked(a, b, c)
        await simnet.all_verified(a, b, c)

        job_id = await _confined_dispatch(simnet, a, b, sandbox)
        payload = {"ok": True, "duty": "review", "output": "forged", "error": ""}

        # (a) another peer answering for the executor
        c.inject_to(a, {"t": "job-result", "id": job_id, "node": b.id,
                        "result": payload})
        # (b) the executor's own link, but with a signature that isn't its own
        forged_sig = c.node.key.sign(protocol.result_signing_bytes(
            {"id": job_id, "node": b.id, "result": payload}))
        b.inject_to(a, {"t": "job-result", "id": job_id, "node": b.id,
                        "result": payload, "sig": forged_sig})
        # (c) a correctly signed result whose payload was rewritten afterwards
        good_sig = b.node.key.sign(protocol.result_signing_bytes(
            {"id": job_id, "node": b.id, "result": payload}))
        b.inject_to(a, {"t": "job-result", "id": job_id, "node": b.id,
                        "result": {**payload, "output": "rewritten"},
                        "sig": good_sig})
        await simnet.quiet(0.3)

        assert job_id not in a.node._acted_results
        assert job_id in a.node._awaiting_result
        assert not simnet.frames("job-ack", src=a, dst=b), \
            "a forged result must not even be acknowledged"

    simnet.run(scenario())


def test_a_result_is_acted_on_exactly_once_however_often_it_arrives(
        simnet, monkeypatch):
    """Delivery is reliable, which means duplicates. The social action is not
    idempotent — a second one is a second review posted on somebody's PR — so a
    retry must be re-acked and never re-acted."""
    sandbox = _sandbox(monkeypatch)
    acted: list[dict] = []

    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b", trust="foreign", FOREIGN_SPAWN="sandbox {prompt_file}")
        await simnet.linked(a, b)
        await simnet.all_verified(a, b)
        monkeypatch.setattr(spawnjob, "run_result_handler",
                            lambda path: acted.append(json.loads(
                                Path(path).read_text(encoding="utf-8"))))

        job_id = await _confined_dispatch(simnet, a, b, sandbox)
        Path(sandbox[0][1]).write_text("once", encoding="utf-8")
        await simnet.until(lambda: len(acted) == 1, 5.0,
                           "the originator never acted on the result")
        assert acted[0]["output"] == "once" and acted[0]["jobId"] == job_id

        delivered = simnet.frames("job-result", src=b, dst=a)[-1]
        for _ in range(3):
            b.inject_to(a, delivered.raw)
        await simnet.quiet(0.3)

        assert len(acted) == 1, "the social action ran more than once"
        assert len(simnet.frames("job-ack", src=a, dst=b)) >= 4, \
            "a duplicate must still be acked, or the executor retries forever"

    simnet.run(scenario())


def test_a_result_is_re_sent_until_it_is_acknowledged(simnet, monkeypatch):
    """Reliable delivery, not fire-and-forget: the artifact is the whole product
    of the work, and the link it has to cross is the one that just flapped."""
    sandbox = _sandbox(monkeypatch)

    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b", trust="foreign", FOREIGN_SPAWN="sandbox {prompt_file}")
        await simnet.linked(a, b)
        await simnet.all_verified(a, b)

        await _confined_dispatch(simnet, a, b, sandbox)
        blocked = simnet.drop_kind("job-result", src=b, dst=a)
        Path(sandbox[0][1]).write_text("eventually", encoding="utf-8")

        await simnet.until(
            lambda: len(simnet.frames("job-result", src=b, dst=a,
                                      of=simnet.dropped)) >= 3, 5.0,
            "the executor gave up re-sending too early")
        assert not simnet.frames("job-result", src=b, dst=a)

        blocked.remove()
        await simnet.until(lambda: simnet.frames("job-ack", src=a, dst=b), 5.0,
                           "delivery never resumed once the path healed")
        await simnet.until(lambda: not b.node._pending_results, 5.0,
                           "retries did not stop after the ack")

    simnet.run(scenario())


def test_an_originator_that_gave_up_on_us_can_still_be_answered(simnet, monkeypatch):
    """Retrying forever is a leak; forgetting is how a machine gets banned for
    work it actually did. So a given-up delivery becomes a tombstone, and a
    reminder from an originator that is clearly back revives it."""
    sandbox = _sandbox(monkeypatch)

    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b", trust="foreign", FOREIGN_SPAWN="sandbox {prompt_file}",
                              RESULT_MAX_SECS="0.3")
        await simnet.linked(a, b)
        await simnet.all_verified(a, b)

        job_id = await _confined_dispatch(simnet, a, b, sandbox)
        blocked = simnet.drop_kind("job-result", src=b, dst=a)
        Path(sandbox[0][1]).write_text("done long ago", encoding="utf-8")
        await simnet.until(
            lambda: any(p.gave_up for p in b.node._pending_results.values()), 5.0,
            "the executor never stopped retrying")

        blocked.remove()
        await simnet.quiet(0.3)
        assert not simnet.frames("job-result", src=b, dst=a), \
            "a given-up delivery must stay stopped until it is asked for"

        a.inject_to(b, {"t": "job-reminder", "id": job_id, "node": a.id})
        await simnet.until(lambda: simnet.frames("job-result", src=b, dst=a), 5.0,
                           "the reminder did not revive the delivery")

    simnet.run(scenario())


def test_an_over_large_artifact_is_trimmed_and_says_so(simnet, monkeypatch):
    """The originator acts on this text under its own identity, so an artifact
    clipped to a size limit must not arrive looking like the whole answer."""
    sandbox = _sandbox(monkeypatch)
    acted: list[dict] = []

    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b", trust="foreign", FOREIGN_SPAWN="sandbox {prompt_file}")
        await simnet.linked(a, b)
        await simnet.all_verified(a, b)
        monkeypatch.setattr(spawnjob, "run_result_handler",
                            lambda path: acted.append(json.loads(
                                Path(path).read_text(encoding="utf-8"))))

        await _confined_dispatch(simnet, a, b, sandbox)
        Path(sandbox[0][1]).write_text("x" * (600 * 1024), encoding="utf-8")

        await simnet.until(lambda: simnet.frames("job-result", src=b, dst=a), 8.0,
                           "the over-large artifact never arrived at all")
        result = simnet.frames("job-result", src=b, dst=a)[-1].payload()["result"]
        assert result["ok"] is True, "a clipped artifact is still a computed one"
        assert len(result["output"]) == 400 * 1024
        assert "truncated" in result["error"]

        await simnet.until(lambda: acted, 5.0, "the originator never acted")
        assert "truncated" in acted[0]["error"], \
            "the caveat must reach whatever acts on the artifact"

    simnet.run(scenario())


def test_an_artifact_that_only_escaping_makes_too_large_still_fits_the_wire(
        simnet, monkeypatch):
    """The size limits are two, not one: an artifact well under the read cap can
    still double under JSON escaping and overrun the frame — and a frame over the
    limit is not a big answer, it is no answer at all, plus a ban for silence."""
    sandbox = _sandbox(monkeypatch)

    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b", trust="foreign", FOREIGN_SPAWN="sandbox {prompt_file}")
        await simnet.linked(a, b)
        await simnet.all_verified(a, b)

        await _confined_dispatch(simnet, a, b, sandbox)
        # Every character escapes to two, so 300 KiB of these is 600 KiB on the
        # wire — past the frame limit, while the artifact itself is not.
        Path(sandbox[0][1]).write_text('"' * (300 * 1024), encoding="utf-8")

        await simnet.until(lambda: simnet.frames("job-result", src=b, dst=a), 8.0,
                           "the escaped-oversize artifact never arrived")
        frame = simnet.frames("job-result", src=b, dst=a)[-1]
        assert len(frame.raw) <= protocol.MAX_LINE_BYTES
        result = frame.payload()["result"]
        assert result["output"], "the artifact was trimmed away to nothing"
        assert "truncated" in result["error"]
        await simnet.until(lambda: simnet.frames("job-ack", src=a, dst=b), 5.0,
                           "the fitted result was never accepted by the originator")

    simnet.run(scenario())


# MARK: - accountability: deadline, reminder, extension or ban


def test_an_executor_that_goes_silent_is_reminded_and_then_banned(
        simnet, monkeypatch):
    """An acceptance is a promise. A device that takes work and neither delivers
    nor explains is the one case the mesh handles by itself."""
    sandbox = _sandbox(monkeypatch)

    async def scenario():
        a = await simnet.node("a", trust="foreign")
        b = await simnet.node("b", trust="foreign", FOREIGN_SPAWN="sandbox {prompt_file}")
        await simnet.linked(a, b)
        await simnet.all_verified(a, b)
        assert a.trust_of(b) == "foreign"
        simnet.drop_kind("job-progress", src=b, dst=a)
        simnet.drop_kind("job-result", src=b, dst=a)

        await _confined_dispatch(simnet, a, b, sandbox)
        await simnet.until(lambda: simnet.frames("job-reminder", src=a, dst=b), 4.0,
                           "the completion deadline never produced a reminder")
        await simnet.until(lambda: a.trust_of(b) == "banned", 6.0,
                           "a silent executor was never held to its acceptance")

        entry = a.node.snapshot()["banned"][0]
        assert entry["fingerprint"] == b.node.fingerprint
        assert "failed to deliver" in entry["reason"]

    simnet.run(scenario())


def test_a_personal_executor_is_never_put_on_the_clock(simnet, monkeypatch):
    """Trusting a device and holding it to the foreign contract are contradictory.
    The operator vouched for this machine; a slow job on it is not a broken
    promise."""
    sandbox = _sandbox(monkeypatch)

    async def scenario():
        a = await simnet.node("a")  # personal default: B is one of ours
        b = await simnet.node("b", trust="foreign", FOREIGN_SPAWN="sandbox {prompt_file}")
        await simnet.linked(a, b)
        await simnet.all_verified(a, b)
        simnet.drop_kind("job-result", src=b, dst=a)

        await _confined_dispatch(simnet, a, b, sandbox)
        await simnet.quiet(1.5)  # well past the completion deadline and grace

        assert a.trust_of(b) == "personal"
        assert not simnet.frames("job-reminder", src=a, dst=b)
        assert a.node.snapshot()["banned"] == []

    simnet.run(scenario())


def test_a_fire_and_forget_acceptance_arms_no_deadline(simnet, monkeypatch):
    """Asymmetric trust is normal: B runs A's request directly because B trusts
    A, while A classifies B foreign. B owes no result then, and banning it for
    keeping one it never promised would break every such pair."""
    async def scenario():
        a = await simnet.node("a", trust="foreign")
        b = await simnet.node("b")  # B trusts A, so it runs the job directly
        await simnet.linked(a, b)
        await simnet.all_verified(a, b)

        results = await a.dispatch("review", "run it", target=b.id)
        assert results[0]["status"] == "spawned"
        status = simnet.frames("job-status", src=b, dst=a)[-1].payload()
        assert status["direct"] is True
        assert a.node._awaiting_result == {} or all(
            aw.deadline is None for aw in a.node._awaiting_result.values())

        await simnet.quiet(1.5)
        assert a.trust_of(b) == "foreign" and a.node.snapshot()["banned"] == []
        assert not simnet.frames("job-reminder", src=a, dst=b)

    simnet.run(scenario())


def test_a_still_running_executor_answers_the_reminder_truthfully(
        simnet, monkeypatch):
    """Honest lateness has an answer: the executor says the work is still going,
    and the *originator's* decider — never the executor — rules on it."""
    sandbox = _sandbox(monkeypatch)

    async def scenario():
        a = await simnet.node("a", trust="foreign")
        b = await simnet.node("b", trust="foreign", FOREIGN_SPAWN="sandbox {prompt_file}")
        await simnet.linked(a, b)
        await simnet.all_verified(a, b)

        await _confined_dispatch(simnet, a, b, sandbox)
        await simnet.until(lambda: simnet.frames("job-progress", src=b, dst=a), 4.0,
                           "a running executor never answered the reminder")
        note = simnet.frames("job-progress", src=b, dst=a)[-1].payload()["note"]
        assert "still running" in note

        # With no decider configured, a plea cannot save it — the zero-trust
        # default is that nothing but delivery does.
        await simnet.until(lambda: a.trust_of(b) == "banned", 6.0,
                           "an unanswerable plea did not resolve into a ban")
        assert "extension decider" in a.node.snapshot()["banned"][0]["reason"]

    simnet.run(scenario())


def test_a_granted_extension_re_arms_the_deadline_instead_of_banning(
        simnet, monkeypatch):
    """The verdict is the originator's alone, and it is an agent's call — here a
    decider that says yes, so the executor gets its time."""
    sandbox = _sandbox(monkeypatch)

    async def scenario():
        a = await simnet.node("a", trust="foreign", EXTEND_DECIDER="true")
        b = await simnet.node("b", trust="foreign", FOREIGN_SPAWN="sandbox {prompt_file}")
        await simnet.linked(a, b)
        await simnet.all_verified(a, b)

        job_id = await _confined_dispatch(simnet, a, b, sandbox)
        await simnet.until(
            lambda: a.node._awaiting_result.get(job_id)
            and a.node._awaiting_result[job_id].extensions >= 1, 6.0,
            "the plea was never granted an extension")
        assert a.trust_of(b) == "foreign", "an extended executor must not be banned"

        entry = a.node._awaiting_result[job_id]
        assert entry.reminded_at is None, "the reminder clock must be reset"
        assert entry.deadline is not None, "the completion deadline must be re-armed"

        # And the work still lands: the extension bought time, not amnesty.
        Path(sandbox[0][1]).write_text("finished after all", encoding="utf-8")
        await simnet.until(lambda: simnet.frames("job-ack", src=a, dst=b), 6.0,
                           "the late artifact was never delivered")
        assert a.node.snapshot()["banned"] == []

    simnet.run(scenario())


def test_a_reminder_answered_with_a_failure_is_a_broken_promise(
        simnet, monkeypatch):
    """A response that does not fulfil the task is not a fulfilment. A timely
    failure is honest and forgiven; one produced only under a reminder, hours
    late, is the promise broken."""
    sandbox = _sandbox(monkeypatch)

    async def scenario():
        a = await simnet.node("a", trust="foreign", COMPLETION_DEADLINE_SECS="0.3",
                              REMINDER_GRACE_SECS="4.0")
        b = await simnet.node("b", trust="foreign", FOREIGN_SPAWN="sandbox {prompt_file}",
                              FOREIGN_TIMEOUT_SECS="1.0")
        await simnet.linked(a, b)
        await simnet.all_verified(a, b)
        # The plea is not what is on trial here — silence it so the verdict can
        # only come from the answer the executor actually delivers.
        simnet.drop_kind("job-progress", src=b, dst=a)

        await _confined_dispatch(simnet, a, b, sandbox)
        await simnet.until(lambda: simnet.frames("job-reminder", src=a, dst=b), 4.0,
                           "the deadline never produced a reminder")
        # The sandbox writes nothing, so B's own compute budget runs out and it
        # returns an explicit failure — well inside the grace window.
        await simnet.until(lambda: simnet.frames("job-result", src=b, dst=a), 6.0,
                           "the executor never answered at all")
        assert simnet.frames("job-result", src=b, dst=a)[-1].payload()[
            "result"]["ok"] is False

        await simnet.until(lambda: a.trust_of(b) == "banned", 6.0,
                           "a non-fulfilling answer to a reminder was accepted")
        assert "non-fulfilling" in a.node.snapshot()["banned"][0]["reason"]

    simnet.run(scenario())


def test_a_ban_survives_the_device_reconnecting(simnet, monkeypatch):
    """A ban recorded against an id alone is defeated by presenting any key, so
    it binds to the fingerprint the executor proved when it accepted the work —
    including when the peer is long gone by the time the verdict lands."""
    sandbox = _sandbox(monkeypatch)

    async def scenario():
        a = await simnet.node("a", trust="foreign")
        b = await simnet.node("b", trust="foreign", FOREIGN_SPAWN="sandbox {prompt_file}")
        await simnet.linked(a, b)
        await simnet.all_verified(a, b)
        simnet.drop_kind("job-progress", src=b, dst=a)
        simnet.drop_kind("job-result", src=b, dst=a)

        await _confined_dispatch(simnet, a, b, sandbox)
        await simnet.until(lambda: a.trust_of(b) == "banned", 6.0,
                           "the silent executor was never banned")

        await b.restart()
        await simnet.linked(a, b)
        await simnet.all_verified(a, b)
        assert a.trust_of(b) == "banned", "a reconnect shed the ban"

        refused = await a.dispatch("review", "one more time", target=b.id)
        assert refused[0]["reason"] == "target is banned here"

    simnet.run(scenario())


# MARK: - the real runner, once, end to end


def test_the_real_confinement_runner_carries_a_request_and_its_answer(simnet):
    """Everything the other tests stub: the prompt is staged to a file, the
    operator's own command runs on it, its artifact comes back signed, and the
    originator's handler runs on the result under the originator's identity.

    The command is a plain ``cp``, so what is proven is the plumbing rather than
    any particular sandbox — which is the whole point, since the isolation is the
    operator's to provide.
    """
    async def scenario(tmp: Path):
        landed = tmp / "handled.json"
        a = await simnet.node(
            "a", ON_RESULT=f"cp {{result_file}} {landed}")
        b = await simnet.node(
            "b", trust="foreign",
            FOREIGN_SPAWN="sh -c 'cp \"$1\" \"$2\"' _ {prompt_file} {result_file}")
        await simnet.linked(a, b)
        await simnet.all_verified(a, b)

        results = await a.dispatch("review", "the untrusted prompt", target=b.id)
        assert results[0]["status"] == "spawned", results

        await simnet.until(landed.exists, 10.0,
                           "the artifact never reached the originator's handler")
        handled = json.loads(landed.read_text(encoding="utf-8"))
        assert handled["from"] == b.id and handled["duty"] == "review"
        assert "the untrusted prompt" in handled["output"]
        assert "zero-trust execution" in handled["output"], \
            "the sandbox must have been told the rules of the road"
        assert b.jobs == [], "a foreign request must never reach the host runner"

    tmp = Path(simnet._tmp) / "e2e"
    tmp.mkdir(parents=True, exist_ok=True)
    simnet.run(scenario(tmp))


def test_the_confined_child_is_not_handed_this_machines_credentials(simnet):
    """Defence in depth, not the boundary — but a mis-built sandbox must not be
    the only thing standing between a stranger's prompt and a GitHub token."""
    async def scenario(tmp: Path):
        dumped = tmp / "env.txt"
        a = await simnet.node("a")
        b = await simnet.node(
            "b", trust="foreign",
            FOREIGN_SPAWN=f"sh -c 'env > {dumped}'")
        await simnet.linked(a, b)
        await simnet.all_verified(a, b)

        import os
        os.environ["GH_TOKEN"] = "ghp_secret_value"
        os.environ["MY_API_KEY"] = "also-secret"
        try:
            results = await a.dispatch("review", "peek at the environment",
                                       target=b.id)
            assert results[0]["status"] == "spawned", results
            await simnet.until(dumped.exists, 10.0, "the sandbox never ran")
            await simnet.quiet(0.2)
            body = dumped.read_text(encoding="utf-8")
        finally:
            os.environ.pop("GH_TOKEN", None)
            os.environ.pop("MY_API_KEY", None)

        assert "ghp_secret_value" not in body and "also-secret" not in body
        assert "SZPONTNET_CONFINED=1" in body
        assert "SZPONTNET_RESULT_FILE=" in body

    tmp = Path(simnet._tmp) / "envdump"
    tmp.mkdir(parents=True, exist_ok=True)
    simnet.run(scenario(tmp))
