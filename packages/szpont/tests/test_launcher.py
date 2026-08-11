"""The ``szpont`` console script: what it decides, and what it then runs.

The decisions are all in :func:`szpont_launcher.plan`, a pure function of the
facts, so nearly everything here hands it a machine that does not exist - no
Swift, a stale venv, someone else's checkout - and reads back the steps. What a
real machine looks like is :func:`szpont_launcher.probe`'s job and is tested
separately against a temporary HOME.

The end of the file is the only test that runs anything: a real clone from a real
(local) git repository, through the real steps, into a stub build script and a
stub launch. It is the one that would catch a plan that is right on paper and
unrunnable in practice - a wrong cwd, an argument list the shell never sees, an
exec that does not happen.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import szpont_launcher as launcher  # noqa: E402

PKG_DIR = Path(__file__).resolve().parents[1]


def facts(**overrides) -> dict:
    """A machine with everything on it, ready to be taken apart one fact at a time."""
    base = {
        "platform": "linux",
        "path": "/usr/bin:/bin",
        "checkout": "/home/u/.diplomat/checkout",
        "checkout_state": "checkout",
        "managed": True,
        "repo_url": launcher.DEFAULT_REPO_URL,
        "update": True,
        "git": True,
        "swift": True,
        "python3": "3.12.3",
        "core_bin": True,
        "venv": "/home/u/.diplomat/venv",
        "venv_python": True,
        "venv_current": True,
        "args": [],
    }
    return {**base, **overrides}


def ids(plan: dict) -> list[str]:
    return [s["id"] for s in plan["steps"]]


def step(plan: dict, sid: str) -> dict:
    return next(s for s in plan["steps"] if s["id"] == sid)


# --- one version, four places ----------------------------------------------


def test_every_szpont_carries_the_same_version():
    """PyPI and npm publish the same name from this one commit.

    Four files say what version that is, and the release workflow refuses a tag
    that disagrees with them - so a bump that misses one of them has to fail here,
    while it is still cheap. An index version, once taken, is taken forever.
    """
    pyproject = (PKG_DIR / "pyproject.toml").read_text(encoding="utf-8")
    npm = json.loads((PKG_DIR.parent / "szpont-npm" / "package.json").read_text(encoding="utf-8"))
    js = (PKG_DIR.parent / "szpont-npm" / "src" / "launcher.js").read_text(encoding="utf-8")

    import szpont

    assert re.search(r'^version = "(.+)"$', pyproject, re.M).group(1) == launcher.__version__
    assert szpont.__version__ == launcher.__version__
    assert npm["version"] == launcher.__version__
    assert re.search(r"^export const VERSION = '(.+)';$", js, re.M).group(1) == launcher.__version__


# --- what gets the checkout ------------------------------------------------


def test_a_missing_checkout_is_cloned_and_not_pulled():
    plan = launcher.plan(facts(platform="darwin", checkout_state="absent"))
    assert ids(plan) == ["clone", "build", "launch"]
    assert step(plan, "clone")["cmd"] == [
        "git", "clone", launcher.DEFAULT_REPO_URL, "/home/u/.diplomat/checkout"
    ]


def test_a_checkout_this_launcher_owns_is_fast_forwarded_first():
    plan = launcher.plan(facts(platform="darwin"))
    assert ids(plan)[0] == "update"
    assert step(plan, "update")["cmd"] == [
        "git", "-C", "/home/u/.diplomat/checkout", "pull", "--ff-only", "--quiet"
    ]
    assert step(plan, "update")["optional"] is True


def test_someone_elses_checkout_is_never_touched():
    """DIPLOMAT_SELF_REPO names a working copy, which may have work in it."""
    plan = launcher.plan(facts(platform="darwin", managed=False))
    assert "update" not in ids(plan)


def test_no_update_leaves_even_the_managed_checkout_alone():
    plan = launcher.plan(facts(platform="darwin", update=False))
    assert "update" not in ids(plan)


def test_a_clone_needs_git_but_a_launch_does_not():
    absent = launcher.plan(facts(platform="darwin", checkout_state="absent", git=False))
    assert absent["blocked"]["tool"] == "git"

    # The same machine with the checkout already there still runs: the pull is the
    # only thing git was wanted for, and it drops out rather than blocking.
    present = launcher.plan(facts(platform="darwin", git=False))
    assert present["blocked"] is None
    assert "update" not in ids(present)


def test_a_directory_that_is_not_a_checkout_stops_everything():
    plan = launcher.plan(facts(platform="darwin", checkout_state="foreign"))
    assert plan["steps"] == []  # nothing is built on top of the wrong directory
    assert plan["blocked"]["tool"] is None
    assert "/home/u/.diplomat/checkout" in plan["blocked"]["reason"]
    assert "DIPLOMAT_SELF_REPO" in plan["blocked"]["fix"]


# --- macOS -----------------------------------------------------------------


def test_macos_builds_the_bundle_then_opens_it():
    plan = launcher.plan(facts(platform="darwin", checkout_state="absent"))
    macos = "/home/u/.diplomat/checkout/packages/diplomat-platform/macos"
    assert step(plan, "build")["cmd"] == [f"{macos}/install/build-app.sh"]
    assert step(plan, "build")["cwd"] == macos
    assert step(plan, "launch")["cmd"] == ["open", f"{macos}/Diplomat.app"]


def test_macos_hands_the_applet_its_arguments_behind_open():
    plan = launcher.plan(facts(platform="darwin", args=["--prefill", "337"]))
    assert step(plan, "launch")["cmd"][-3:] == ["--args", "--prefill", "337"]


def test_without_swift_macos_says_where_a_mac_gets_one():
    plan = launcher.plan(facts(platform="darwin", swift=False))
    assert plan["blocked"]["tool"] == "swift"
    assert "xcode-select" in plan["blocked"]["fix"]
    assert "swift.org" not in plan["blocked"]["fix"]  # that is the Linux answer


# --- Linux -----------------------------------------------------------------


def test_linux_builds_the_prompt_binary_only_when_the_applet_would_not_find_one():
    with_bin = launcher.plan(facts(core_bin=True))
    assert "build-core" not in ids(with_bin)

    without = launcher.plan(facts(core_bin=False))
    linux = "/home/u/.diplomat/checkout/packages/diplomat-platform/linux"
    assert step(without, "build-core")["cmd"] == [f"{linux}/install/build-core.sh"]
    assert without["blocked"] is None  # swift is present in the base facts


def test_without_swift_linux_points_at_swift_org():
    plan = launcher.plan(facts(core_bin=False, swift=False))
    assert plan["blocked"]["tool"] == "swift"
    assert "swift.org" in plan["blocked"]["fix"]


def test_a_ready_venv_is_left_alone():
    assert ids(launcher.plan(facts())) == ["update", "launch"]


def test_a_venv_whose_requirements_moved_is_reinstalled_into():
    plan = launcher.plan(facts(venv_current=False))
    assert ids(plan) == ["update", "deps", "launch"]
    linux = "/home/u/.diplomat/checkout/packages/diplomat-platform/linux"
    assert step(plan, "deps")["cmd"] == [
        "/home/u/.diplomat/venv/bin/python", "-m", "pip", "install", "--upgrade",
        "--quiet", "-r", f"{linux}/requirements.txt",
    ]


def test_a_missing_venv_is_created_before_it_is_installed_into():
    plan = launcher.plan(facts(venv_python=False, venv_current=False))
    assert ids(plan) == ["update", "venv", "deps", "launch"]
    assert step(plan, "venv")["cmd"] == ["python3", "-m", "venv", "/home/u/.diplomat/venv"]


def test_the_applet_starts_on_the_venvs_interpreter():
    """The checkout's own `diplomat` script resolves `python3` off PATH, so the
    venv going in front of it is the whole of "run it with Qt available"."""
    plan = launcher.plan(facts(args=["--dump"]))
    launch = step(plan, "launch")
    linux = "/home/u/.diplomat/checkout/packages/diplomat-platform/linux"
    assert launch["cmd"] == [f"{linux}/diplomat", "--dump"]
    assert launch["cwd"] == linux
    assert launch["env"]["PATH"] == "/home/u/.diplomat/venv/bin:/usr/bin:/bin"


def test_python3_is_wanted_for_the_venv_and_nothing_else():
    missing = launcher.plan(facts(python3=None, venv_python=False, venv_current=False))
    assert missing["blocked"]["tool"] == "python3"

    # An existing venv already has an interpreter; the system one is irrelevant.
    ready = launcher.plan(facts(python3=None))
    assert ready["blocked"] is None


def test_a_venv_is_never_built_from_a_python_the_applet_cannot_run_on():
    plan = launcher.plan(facts(python3="3.9.6", venv_python=False, venv_current=False))
    assert plan["blocked"]["tool"] == "python3"
    assert "3.9.6" in plan["blocked"]["reason"]
    assert "3.10" in plan["blocked"]["reason"]


def test_an_existing_venv_is_not_re_opened_over_its_python_version():
    assert launcher.plan(facts(python3="3.9.6"))["blocked"] is None


def test_an_unreadable_python_version_is_not_treated_as_old():
    plan = launcher.plan(facts(python3="3.14.0a1+", venv_python=False, venv_current=False))
    assert plan["blocked"] is None


# --- neither platform ------------------------------------------------------


def test_an_unsupported_platform_plans_nothing_and_says_so():
    plan = launcher.plan(facts(platform="win32"))
    assert plan["steps"] == []
    assert "win32" in plan["blocked"]["reason"]


# --- probe -----------------------------------------------------------------


def bin_dir(tmp_path: Path, *tools: str) -> str:
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    for tool in tools:
        (d / tool).write_text("#!/bin/sh\n", encoding="utf-8")
        (d / tool).chmod(0o755)
    return str(d)


def test_probe_defaults_the_checkout_into_the_state_directory(tmp_path):
    found = launcher.probe(env={"HOME": str(tmp_path), "PATH": ""})
    assert found["checkout"] == str(tmp_path / ".diplomat" / "checkout")
    assert found["venv"] == str(tmp_path / ".diplomat" / "venv")
    assert found["managed"] is True
    assert found["checkout_state"] == "absent"


def test_probe_follows_the_applets_own_checkout_variable(tmp_path):
    (tmp_path / "src" / "packages" / "diplomat-platform").mkdir(parents=True)
    found = launcher.probe(env={"HOME": str(tmp_path), "PATH": "",
                                "DIPLOMAT_SELF_REPO": str(tmp_path / "src")})
    assert found["checkout"] == str(tmp_path / "src")
    assert found["managed"] is False
    assert found["checkout_state"] == "checkout"


def test_probe_tells_a_checkout_from_any_other_directory(tmp_path):
    (tmp_path / "elsewhere").mkdir()
    found = launcher.probe(env={"HOME": str(tmp_path), "PATH": "",
                                "DIPLOMAT_SELF_REPO": str(tmp_path / "elsewhere")})
    assert found["checkout_state"] == "foreign"


def test_probe_finds_the_tools_on_the_path_it_is_given(tmp_path):
    found = launcher.probe(env={"HOME": str(tmp_path), "PATH": bin_dir(tmp_path, "git")})
    assert found["git"] is True
    assert found["swift"] is False


def test_probe_takes_a_fork_from_the_environment(tmp_path):
    found = launcher.probe(env={"HOME": str(tmp_path), "PATH": "",
                                "DIPLOMAT_REPO_URL": "/srv/diplomat.git"})
    assert found["repo_url"] == "/srv/diplomat.git"


def test_a_venv_is_current_only_against_the_requirements_it_was_built_from(tmp_path):
    requirements = (tmp_path / ".diplomat" / "checkout" / "packages"
                    / "diplomat-platform" / "linux" / "requirements.txt")
    requirements.parent.mkdir(parents=True)
    requirements.write_text("PySide6>=6.5\n", encoding="utf-8")
    (tmp_path / ".diplomat" / "checkout" / "packages" / "diplomat-platform").mkdir(exist_ok=True)
    venv = tmp_path / ".diplomat" / "venv"
    venv.mkdir(parents=True)
    stamp = venv / ".szpont-requirements"

    env = {"HOME": str(tmp_path), "PATH": ""}
    assert launcher.probe(env=env)["venv_current"] is False  # no stamp at all

    stamp.write_text(launcher._requirements_digest(requirements), encoding="utf-8")
    assert launcher.probe(env=env)["venv_current"] is True

    requirements.write_text("PySide6>=6.5\ncryptography>=41\n", encoding="utf-8")
    assert launcher.probe(env=env)["venv_current"] is False


# --- end to end ------------------------------------------------------------


def fake_checkout(root: Path, platform: str, marker: Path) -> None:
    """A tree shaped like Diplomat's, whose build and launch are stubs that report.

    Only the two scripts the launcher actually reaches are real files; everything
    else about the layout is what `plan` navigates by.
    """
    pkg = root / "packages" / "diplomat-platform" / platform
    (pkg / "install").mkdir(parents=True)
    if platform == "macos":
        script = pkg / "install" / "build-app.sh"
        script.write_text(f'#!/bin/sh\necho "built in $PWD" >> {marker}\n', encoding="utf-8")
        script.chmod(0o755)
    else:
        (pkg / "requirements.txt").write_text("", encoding="utf-8")
        launch = pkg / "diplomat"
        launch.write_text(
            f'#!/bin/sh\necho "launched in $PWD with $* using $(command -v python3)" >> {marker}\n',
            encoding="utf-8",
        )
        launch.chmod(0o755)
        core = root / "packages" / "diplomat-platform" / "linux" / "install" / "build-core.sh"
        core.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")  # never reached: core_bin is faked
        core.chmod(0o755)


@pytest.mark.skipif(sys.platform not in ("darwin", "linux"),
                    reason="the launcher only plans for macOS and Linux")
def test_it_clones_builds_and_launches_for_real(tmp_path):
    """Everything a first run does, against a git repository on this disk.

    The stub `open` (macOS) and stub `diplomat` (Linux) are what the last step
    execs, and the marker file is the only evidence that it did - which is the
    point: a plan whose launch never runs looks identical from the plan side.
    """
    origin = tmp_path / "origin"
    fake_checkout(origin, "macos" if sys.platform == "darwin" else "linux",
                  marker=tmp_path / "marker.txt")
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "-C", str(origin), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(origin), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "--quiet", "-m", "tree"], check=True)

    home = tmp_path / "home"
    home.mkdir()
    env = dict(os.environ, HOME=str(home), DIPLOMAT_REPO_URL=str(origin),
               PYTHONPATH=str(PKG_DIR))
    env.pop("DIPLOMAT_SELF_REPO", None)
    if sys.platform == "darwin":
        # `open` would really open Finder on a bundle that is not there; the stub
        # in front of it records the argv the launch step exec'd instead.
        stub = tmp_path / "stub"
        stub.mkdir()
        (stub / "open").write_text(
            f'#!/bin/sh\necho "launched in $PWD with $*" >> {tmp_path / "marker.txt"}\n',
            encoding="utf-8")
        (stub / "open").chmod(0o755)
        env["PATH"] = f"{stub}{os.pathsep}{env['PATH']}"
    else:
        env["DIPLOMAT_CORE_BIN"] = str(tmp_path / "origin")  # exists, so no Swift build

    proc = subprocess.run([sys.executable, "-m", "szpont_launcher", "--", "--dump"],
                          env=env, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr

    checkout = home / ".diplomat" / "checkout"
    assert (checkout / "packages" / "diplomat-platform").is_dir(), proc.stderr
    marker = (tmp_path / "marker.txt").read_text(encoding="utf-8")
    if sys.platform == "darwin":
        assert "built in" in marker
        assert "--args --dump" in marker
    else:
        assert "--dump" in marker
        # The venv's interpreter, not the one that started the launcher.
        assert str(home / ".diplomat" / "venv" / "bin" / "python3") in marker
        assert (home / ".diplomat" / "venv" / ".szpont-requirements").is_file()

    # Second run: nothing left to clone, nothing left to install, and it says so.
    plan = json.loads(subprocess.run(
        [sys.executable, "-m", "szpont_launcher", "--plan"],
        env=env, capture_output=True, text=True, check=True).stdout)["plan"]
    assert [s["id"] for s in plan["steps"]] == (
        ["update", "build", "launch"] if sys.platform == "darwin" else ["update", "launch"]
    )
