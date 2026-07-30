"""Putting your application behind a node, without writing a class.

A node asks its **host** the five things it cannot answer alone: which duties this
deployment routes, where its state lives, where its events go, what running a job
means here, and whether that work is already under way on this machine
(:mod:`szpontnet.host`). The library's own way to answer is to subclass
:class:`~szpontnet.host.Host` and override what you answer differently.

Most hosts answer one or two of those - usually just :func:`run_job` - and a class
for that is ceremony. :func:`register` takes the answers as functions:

    import szpont

    szpont.register_host(
        duties=["render"],
        run_job=lambda prompt, done_path: my_queue.submit(prompt, done_path),
    )

Whatever you leave out keeps the library's default, which is a real answer and not
a placeholder: no runner means this machine declines work and the dispatcher fails
over to the next candidate, which is exactly what a machine with nothing to run it
should say.

This registers **in-process**. A node your application *spawns* is a separate
process and cannot see it - point that one at a module with ``SZPONTNET_HOST``,
as :mod:`szpontnet.host` describes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

from szpontnet import host as _host
from szpontnet.host import Host, NoRunner

__all__ = ["Host", "NoRunner", "build_host", "register_host", "unregister_host",
           "duty_model"]


def duty_model(duties: Iterable[str], *, token_aware: bool = True,
               spread: Iterable[tuple[str, int]] = ()) -> dict:
    """A network model that routes exactly ``duties``, for the common case.

    The duty catalog is replaced wholesale rather than merged, so a deployment
    that names its duties gets *those* duties and not those plus the canonical
    ``review``/``conflicts``/``audit``. Every duty here shares one placement;
    a deployment that needs them to differ writes the model out by hand and
    passes it as ``model`` instead.

    ``token_aware`` excludes machines that are out of tokens, and ``spread`` is
    the ``(platform, count)`` staffing a duty requires - an empty spread means one
    slot on whichever machine ranks best.

    Each spread pair is written out as the ``{"platform", "count"}`` object the
    schema defines. That shape is load-bearing rather than cosmetic: placements
    arrive over gossip too, so the library skips any spread entry that is not an
    object - a pair emitted as a two-element list would not be rejected, it would
    silently resolve to no spread at all, and the duty would staff one machine
    instead of the platforms asked for.
    """
    slots = [{"platform": platform, "count": int(count)} for platform, count in spread]
    # A fresh placement per duty, so a caller that edits one duty's spread in the
    # returned model does not silently edit every other duty's too.
    return {"duties": [{"id": duty,
                        "placement": {"tokenAware": bool(token_aware),
                                      "spread": [dict(s) for s in slots]}}
                       for duty in duties]}


def build_host(
    *,
    model: Callable[[], dict] | dict | None = None,
    duties: Iterable[str] | None = None,
    state_dir: Callable[[], Path | str] | Path | str | None = None,
    log: Callable[[str, str], None] | None = None,
    run_job: Callable[[str, str | None], str] | None = None,
    work_already_running: Callable[[str], bool] | None = None,
) -> Host:
    """A :class:`~szpontnet.host.Host` answering only what you gave it.

    Each argument takes either a callable, which the node calls when it needs the
    answer, or a plain value, which is treated as a callable returning it. Anything
    omitted falls through to the library's default.

    ``duties`` is shorthand for ``model=duty_model(duties)`` and cannot be combined
    with an explicit ``model``.
    """
    if duties is not None:
        if model is not None:
            raise ValueError("pass either `duties` or `model`, not both - "
                             "`duties` is shorthand for a model that routes them")
        model = duty_model(duties)

    return _CallableHost(
        model=_as_callable(model),
        state_dir=_as_path_callable(state_dir),
        log=log,
        run_job=run_job,
        work_already_running=work_already_running,
    )


def register_host(**answers) -> Host:
    """Build a host from :func:`build_host`'s arguments and put it behind the
    node in this process. Returns it, so a caller can hold on to it.

    The host is process-global and there is one: registering replaces whoever was
    there. It also overrides ``SZPONTNET_HOST``, since an application driving the
    node's modules directly is more specific than the environment it inherited.
    """
    host = build_host(**answers)
    _host.set_host(host)
    return host


def unregister_host() -> None:
    """Take the registered host away - back to the library's own defaults."""
    _host.reset_host()


def _as_callable(value):
    if value is None or callable(value):
        return value
    return lambda: value


def _as_path_callable(value):
    if value is None:
        return None
    if callable(value):
        return lambda: Path(value())
    resolved = Path(value)
    return lambda: resolved


class _CallableHost(Host):
    """A host whose answers are the functions it was given.

    Each method delegates when it has a function for that question and falls
    through to :class:`~szpontnet.host.Host`'s own answer when it does not - so
    an omitted ``run_job`` still raises :class:`~szpontnet.host.NoRunner`, which
    the dispatcher handles as the ordinary decline it is.
    """

    def __init__(self, **answers) -> None:
        self._answers = {name: fn for name, fn in answers.items() if fn is not None}

    def model(self) -> dict:
        fn = self._answers.get("model")
        return fn() if fn else super().model()

    def state_dir(self) -> Path:
        fn = self._answers.get("state_dir")
        return fn() if fn else super().state_dir()

    def log(self, action: str, detail: str) -> None:
        fn = self._answers.get("log")
        if fn:
            fn(action, detail)

    def run_job(self, prompt: str, done_path: str | None) -> str:
        fn = self._answers.get("run_job")
        return fn(prompt, done_path) if fn else super().run_job(prompt, done_path)

    def work_already_running(self, work_key: str) -> bool:
        fn = self._answers.get("work_already_running")
        return bool(fn(work_key)) if fn else super().work_already_running(work_key)
