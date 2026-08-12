"""``szpont`` on the command line: the whole install story for Diplomat.

    pip install szpont     # or:  npx szpont
    szpont

Diplomat is not a program an index can ship. Both front-ends are built out of the
checkout they run from - a SwiftUI bundle on macOS, a PySide6 package plus the
``diplomat-core`` Swift binary on Linux - and both keep *being* that checkout: the
Settings ▸ Update button, the 06:00 timer and the commit the panel reports are all
git operations on it. A wheel carrying a copy would be a second Diplomat that no
update could ever reach.

So what an index can usefully carry is the steps in between: fetch the checkout
when it isn't there, then run the platform's own build and launch scripts - the
same ones `README.md` tells a human to type. Nothing here builds anything itself.

``packages/szpont-npm`` is these same steps in JavaScript, for ``npx szpont``. The
two agree by construction on :func:`plan`, a pure function from what was probed to
what will be run, and ``parity-with-python.mjs`` compares their answers fact for
fact.

A top-level module rather than ``szpont.launcher``: reaching a submodule imports
the bindings, which cost the protocol library that a launcher has no use for -
and keeping the CLI outside the package leaves "the bindings import the library
and nothing else" a claim CI can still make.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Sequence

__version__ = "0.3.0"

DEFAULT_REPO_URL = "https://github.com/latekvo/Diplomat.git"
SUPPORTED = ("darwin", "linux")
MIN_PYTHON = (3, 10)

# The venv and the managed checkout live under the state directory Diplomat
# already owns (agent records, mesh state, the allocator's socket), so uninstalling
# is one `rm -rf ~/.diplomat` rather than a hunt.
STATE_DIR = ".diplomat"


def _which(name: str, env: Mapping[str, str]) -> bool:
    return shutil.which(name, path=env.get("PATH")) is not None


def _python3_version(env: Mapping[str, str]) -> str | None:
    """``python3 --version``, or None when there is no python3 to ask.

    Only ever called on Linux. A stock macOS has a ``/usr/bin/python3`` that is a
    stub for the Command Line Tools: it is on PATH, and *running* it pops the
    graphical "install developer tools" dialog at whoever typed ``szpont``.
    """
    if not _which("python3", env):
        return None
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["python3", "--version"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.split()[-1] if proc.returncode == 0 and proc.stdout.split() else None


def _requirements_digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def probe(app_args: Sequence[str] = (), *, update: bool = True,
          env: Mapping[str, str] | None = None) -> dict:
    """Everything :func:`plan` decides from, read off this machine once.

    Split from the planning so the decisions are testable without a machine in the
    shape being tested, and so the JavaScript twin can be handed the same facts.
    """
    env = os.environ if env is None else env
    home = Path(env.get("HOME") or Path.home())
    platform = (
        "darwin" if sys.platform == "darwin"
        else "linux" if sys.platform.startswith("linux")
        else sys.platform
    )

    # DIPLOMAT_SELF_REPO is what the applet itself calls the checkout it lives in
    # (selfupdate.repo_root), so pointing the launcher at a working copy and
    # pointing the running applet at one are the same act.
    explicit = env.get("DIPLOMAT_SELF_REPO")
    checkout = Path(explicit) if explicit else home / STATE_DIR / "checkout"
    if not checkout.exists():
        state = "absent"
    elif (checkout / "packages" / "diplomat-platform").is_dir():
        state = "checkout"
    else:
        state = "foreign"

    venv = home / STATE_DIR / "venv"
    requirements = checkout / "packages" / "diplomat-platform" / "linux" / "requirements.txt"
    stamp = venv / ".szpont-requirements"
    digest = _requirements_digest(requirements)

    return {
        "platform": platform,
        "path": env.get("PATH", ""),
        "checkout": str(checkout),
        "checkout_state": state,
        # Only a checkout this launcher created is one it may move: a working copy
        # someone named themselves is theirs, and a launcher that fast-forwarded it
        # would be editing a tree with work in it.
        "managed": explicit is None,
        "repo_url": env.get("DIPLOMAT_REPO_URL") or DEFAULT_REPO_URL,
        "update": bool(update),
        "git": _which("git", env),
        "swift": _which("swift", env),
        # Linux-only questions, and asked only there - see _python3_version.
        "python3": _python3_version(env) if platform == "linux" else None,
        "core_bin": _core_bin_present(home, env) if platform == "linux" else None,
        "venv": str(venv),
        "venv_python": (venv / "bin" / "python").exists(),
        "venv_current": digest is not None and _read(stamp) == digest,
        "args": list(app_args),
    }


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _core_bin_present(home: Path, env: Mapping[str, str]) -> bool:
    """Whether the applet would find a ``diplomat-core`` to shell out to.

    The three candidates and their order are ``promptcore.core_bin``'s; asking a
    different question here would rebuild a binary the applet can already see, or
    skip building one it cannot.
    """
    override = env.get("DIPLOMAT_CORE_BIN")
    if override and Path(override).exists():
        return True
    if _which("diplomat-core", env):
        return True
    data = Path(env.get("XDG_DATA_HOME") or home / ".local" / "share")
    return (data / "diplomat" / "diplomat-core").exists()


def _step(sid: str, cmd: list[str], *, cwd: str | None = None,
          env: dict | None = None, needs: str | None = None,
          optional: bool = False) -> dict:
    return {"id": sid, "cmd": cmd, "cwd": cwd, "env": env or {},
            "needs": needs, "optional": optional}


def plan(facts: Mapping) -> dict:
    """What running :func:`probe`'s machine would do, as data.

    Pure: same facts in, same plan out, on either implementation and either OS.
    That is what makes ``--plan`` worth printing before anything is cloned or
    built, and what the parity test compares.
    """
    checkout = facts["checkout"]
    # Nothing is worth planning past these two: a machine that cannot run Diplomat
    # and a directory that is not it are both answers, and a plan that offered to
    # clone anyway would be describing a download that ends in the same message.
    stop = _unrunnable(facts)
    if stop:
        return {"platform": facts["platform"], "checkout": checkout, "steps": [], "blocked": stop}

    macos = os.path.join(checkout, "packages", "diplomat-platform", "macos")
    linux = os.path.join(checkout, "packages", "diplomat-platform", "linux")
    args = list(facts["args"])
    steps: list[dict] = []

    if facts["checkout_state"] == "absent":
        steps.append(_step("clone", ["git", "clone", facts["repo_url"], checkout], needs="git"))
    elif facts["managed"] and facts["update"]:
        # Best-effort and fast-forward-only. The applet updates itself daily and on
        # a button, so this is not the update path - it is what keeps a one-shot
        # `npx szpont` from launching last month's commit.
        steps.append(_step("update", ["git", "-C", checkout, "pull", "--ff-only", "--quiet"],
                           needs="git", optional=True))

    if facts["platform"] == "darwin":
        steps.append(_step("build", [os.path.join(macos, "install", "build-app.sh")],
                           cwd=macos, needs="swift"))
        # build-app.sh rebuilds unconditionally, so the bundle always matches the
        # source it was just cloned or pulled from; `open` then detaches it from
        # this terminal, which is where a menu-bar app belongs.
        launch = ["open", os.path.join(macos, "Diplomat.app")]
        if args:
            launch += ["--args", *args]
        steps.append(_step("launch", launch, cwd=macos))
    elif facts["platform"] == "linux":
        if not facts["core_bin"]:
            steps.append(_step("build-core", [os.path.join(linux, "install", "build-core.sh")],
                               cwd=linux, needs="swift"))
        # A system python3 is wanted for exactly one thing, creating the venv;
        # everything after it runs on the venv's own interpreter.
        if not facts["venv_python"]:
            steps.append(_step("venv", ["python3", "-m", "venv", facts["venv"]], needs="python3"))
        if not facts["venv_current"]:
            steps.append(_step("deps", [os.path.join(facts["venv"], "bin", "python"),
                                        "-m", "pip", "install", "--upgrade", "--quiet",
                                        "-r", os.path.join(linux, "requirements.txt")]))
        # The checkout's own launcher, with the venv's interpreter in front of it -
        # so PySide6 resolves without the user's python having heard of Qt, and the
        # two spellings of "start the applet" stay one script.
        steps.append(_step("launch", [os.path.join(linux, "diplomat"), *args], cwd=linux,
                           env={"PATH": os.path.join(facts["venv"], "bin") + os.pathsep + facts["path"]}))

    return {"platform": facts["platform"], "checkout": checkout,
            "steps": [s for s in steps if not _dropped(s, facts)],
            "blocked": _blocked(facts, steps)}


def _dropped(step: Mapping, facts: Mapping) -> bool:
    """An optional step whose tool is missing is skipped, never a blocker: a
    checkout that cannot be fast-forwarded is still a checkout that runs."""
    return bool(step["optional"] and step["needs"] and not facts[step["needs"]])


def _unrunnable(facts: Mapping) -> dict | None:
    """What no step could fix: the wrong OS, or the wrong directory."""
    if facts["platform"] not in SUPPORTED:
        return {"tool": None, "reason": f"Diplomat runs on macOS and Linux, not {facts['platform']}",
                "fix": "run it on one of those"}
    if facts["checkout_state"] == "foreign":
        return {"tool": None,
                "reason": f"{facts['checkout']} exists but is not a Diplomat checkout",
                "fix": "remove it, or point DIPLOMAT_SELF_REPO at a real one"}
    return None


def _blocked(facts: Mapping, steps: Sequence[Mapping]) -> dict | None:
    """The first tool this plan needs and this machine has not got, or None.

    Reported rather than raised, so ``--plan`` on a machine missing a toolchain
    still prints the whole plan and names what it is short of.
    """
    for step in steps:
        need = step["needs"]
        if not need or _dropped(step, facts) or facts[need]:
            continue
        return {"tool": need, "reason": _MISSING[need], "fix": _FIX[need][facts["platform"]]}
    # Only the interpreter a venv is about to be *made* from: an existing venv is
    # whatever it was built with, and this launcher does not get to re-open that.
    if any(s["id"] == "venv" for s in steps) and _too_old(facts["python3"]):
        return {"tool": "python3",
                "reason": f"the applet needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+, "
                          f"and python3 is {facts['python3']}",
                "fix": "install a newer python3"}
    return None


def _too_old(version: str | None) -> bool:
    try:
        parts = tuple(int(p) for p in (version or "").split(".")[:2])
    except ValueError:
        return False  # unreadable is not evidence of old; let the applet speak
    return len(parts) == 2 and parts < MIN_PYTHON


_MISSING = {
    "git": "git is needed to fetch Diplomat",
    "swift": "a Swift toolchain is needed to build Diplomat",
    "python3": "python3 is needed to run the Linux applet",
}
_FIX = {
    "git": {"darwin": "xcode-select --install", "linux": "install git from your package manager"},
    "swift": {"darwin": "install Xcode, or the Command Line Tools (xcode-select --install)",
              "linux": "https://swift.org/install - swiftly is the easy path"},
    "python3": {"darwin": "install python3", "linux": "install python3 from your package manager"},
}


def run(steps: Sequence[Mapping], *, checkout: str, venv: str) -> int:
    """Run a plan's steps in order, announcing each; the last one replaces us.

    ``exec`` for the launch rather than a child: the applet is what the user asked
    for, and a parent left holding it would swallow the Ctrl-C meant for it.
    """
    for step in steps:
        env = dict(os.environ, **step["env"])
        print(f"→ {' '.join(step['cmd'])}", file=sys.stderr)
        try:
            if step is steps[-1]:
                if step["cwd"]:
                    os.chdir(step["cwd"])  # exec keeps the cwd it is given, so set it
                os.execvpe(step["cmd"][0], step["cmd"], env)  # no return
            proc = subprocess.run(step["cmd"], cwd=step["cwd"], env=env)  # noqa: S603 - argv list
        except OSError as exc:
            if step["optional"]:
                print(f"  (skipped: {exc})", file=sys.stderr)
                continue
            print(f"szpont: {step['id']} could not run: {exc}", file=sys.stderr)
            return 1
        if proc.returncode != 0:
            if step["optional"]:
                print(f"  (skipped: {step['id']} exited {proc.returncode})", file=sys.stderr)
                continue
            print(f"szpont: {step['id']} failed (exit {proc.returncode})", file=sys.stderr)
            return proc.returncode
        # The venv now matches the requirements it was just given, and recording
        # that is what makes every later launch skip the install. Hashed here
        # rather than at probe time: on a first run the file arrives with the clone,
        # two steps after the only chance probe had to read it.
        if step["id"] == "deps":
            digest = _requirements_digest(
                Path(checkout, "packages", "diplomat-platform", "linux", "requirements.txt"))
            try:
                Path(venv, ".szpont-requirements").write_text(digest or "", encoding="utf-8")
            except OSError:
                pass  # a re-install next time is the whole cost of not recording it
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="szpont",
        description="Fetch Diplomat if it isn't here, build it, and start it.",
        epilog="Everything after -- is passed to the applet. "
               "DIPLOMAT_SELF_REPO points at your own checkout (never updated); "
               "DIPLOMAT_REPO_URL clones from somewhere other than GitHub.",
    )
    parser.add_argument("--plan", action="store_true",
                        help="print what would be run, as JSON, and do none of it")
    parser.add_argument("--no-update", action="store_true",
                        help="launch the checkout as it stands, without fast-forwarding it")
    parser.add_argument("--version", action="version", version=f"szpont {__version__}")
    parser.add_argument("app_args", nargs="*", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    facts = probe(args.app_args, update=not args.no_update)
    steps = plan(facts)

    if args.plan:
        print(json.dumps({"version": __version__, "facts": facts, "plan": steps},
                         indent=2, sort_keys=True))
        return 0
    if steps["blocked"]:
        print(f"szpont: {steps['blocked']['reason']}\n  → {steps['blocked']['fix']}", file=sys.stderr)
        return 2
    return run(steps["steps"], checkout=facts["checkout"], venv=facts["venv"])


if __name__ == "__main__":
    raise SystemExit(main())
