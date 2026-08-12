"""The release tooling that writes the version every `szpont` file states.

`test_launcher.py` pins the version sites to each other; this pins
`.github/scripts/szpont_version.py` to the same files, because a release off
`main` writes the version rather than reading one a human typed. A site the writer
does not know about keeps the old version, and the package that goes to the two
indexes then disagrees with itself about what it is.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / ".github" / "scripts" / "szpont_version.py"


def _load():
    spec = importlib.util.spec_from_file_location("szpont_version", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


version_tool = _load()


@pytest.fixture
def tree(tmp_path):
    """A copy of the real version files, at their real paths, to rewrite."""
    for rel, _pattern in version_tool.SITES:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / rel, tmp_path / rel)
    return tmp_path


def test_the_tree_states_one_version():
    assert re.fullmatch(r"\d+\.\d+\.\d+", version_tool.read(REPO))


def test_set_moves_every_file_that_states_a_version(tree):
    was = version_tool.read(tree)
    version_tool.write(tree, "9.9.9")

    assert version_tool.read(tree) == "9.9.9"
    for rel, _pattern in version_tool.SITES:
        before = (REPO / rel).read_text(encoding="utf-8").splitlines()
        after = (tree / rel).read_text(encoding="utf-8").splitlines()
        moved = [(a, b) for a, b in zip(before, after) if a != b]
        assert len(before) == len(after) and len(moved) == 1, f"{rel} changed elsewhere: {moved}"
        assert was in moved[0][0] and "9.9.9" in moved[0][1]


@pytest.mark.parametrize("version", ["", "1.0", "v1.0.0", "1.0.0rc1", "$VERSION"])
def test_a_version_that_is_not_one_is_never_written(tree, version):
    was = version_tool.read(tree)

    with pytest.raises(version_tool.VersionError, match="not a version"):
        version_tool.write(tree, version)
    assert version_tool.read(tree) == was


def test_a_tree_that_disagrees_with_itself_is_not_a_version(tree):
    rel = version_tool.SITES[0][0]
    path = tree / rel
    path.write_text(path.read_text(encoding="utf-8").replace(version_tool.read(tree), "9.9.9", 1),
                    encoding="utf-8")

    with pytest.raises(version_tool.VersionError, match="disagrees with itself"):
        version_tool.read(tree)


def test_a_site_that_states_no_version_is_not_a_version(tree):
    rel = version_tool.SITES[0][0]
    (tree / rel).write_text("", encoding="utf-8")

    with pytest.raises(version_tool.VersionError, match="0 lines match"):
        version_tool.read(tree)


def test_the_sites_are_every_file_that_states_the_version():
    """Nothing outside the list declares it - a sixth site would go stale in silence.

    A declaration, not the digits: a test that asserts on `"1.0.0"` is not a site,
    and a release must not be held up by one that happens to name the number the
    release is about to take.
    """
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "packages/szpont", "packages/szpont-npm"],
        cwd=REPO, capture_output=True, text=True, check=True).stdout.split("\0")
    declares = re.compile(
        rf"""(?im)^.*version_*['"]?\s*[:=]\s*['"]{re.escape(version_tool.read(REPO))}['"]""")

    carriers = {rel for rel in tracked if rel
                and declares.search((REPO / rel).read_text(encoding="utf-8", errors="ignore"))}
    assert carriers == {rel for rel, _pattern in version_tool.SITES}


# --- what a release off main publishes under --------------------------------


def test_next_major_clears_every_version_already_taken():
    assert version_tool.next_major(["0.2.0"]) == "1.0.0"
    assert version_tool.next_major(["3.0.0", "2.0.0", "0.2.0"]) == "4.0.0"
    assert version_tool.next_major(["9.4.7"]) == "10.0.0"


def test_next_major_counts_from_a_version_it_did_not_write():
    """A hand-uploaded prerelease still takes its major; it must not be reused."""
    assert version_tool.next_major(["1.0.0", "2.0.0rc1"]) == "3.0.0"
    assert version_tool.next_major(["1.0.0", "2.0.0-rc.1"]) == "3.0.0"
    assert version_tool.next_major(["1.0.0", "not-a-version"]) == "2.0.0"


def test_nothing_to_count_from_is_not_a_version():
    with pytest.raises(version_tool.VersionError):
        version_tool.next_major(["not-a-version"])
