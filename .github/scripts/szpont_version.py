#!/usr/bin/env python3
"""The one version `szpont` carries, and every file in the tree that states it.

`szpont` is published under one name to two indexes out of one commit, so the
version is not a fact the release can look up anywhere - five files spell it out,
and a build made from a tree where they disagree is a package that lies about
itself. `show` refuses such a tree, `set` writes all five, and
`test_release_version.py` holds the list to the files that actually state a
version, so a sixth site cannot appear unnoticed.

`next-minor` is what a release off `main` publishes under. It is chosen over what
the indexes already hold rather than over what the tree says, because an upload is
irreversible and the tree is only ever a record of the last release that finished:
a run whose bump commit never landed, or one that reached PyPI and failed on npm,
must not hand the next run a number that is already spent. Taking the highest
`major.minor` either index holds and adding one to the minor also puts a
half-published release back on a single version, on both indexes, at the next push.
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Each pattern must match its file exactly once. A pattern that stops matching is
# a site that would keep the old version while the rest of the tree moves, which
# is the one thing this module exists to prevent - so it is an error here rather
# than a substitution that quietly does nothing.
SITES = (
    ("packages/szpont/pyproject.toml", r'(?m)^version = "(.+)"$'),
    ("packages/szpont/szpont_launcher.py", r'(?m)^__version__ = "(.+)"$'),
    ("packages/szpont/szpont/__init__.py", r'(?m)^__version__ = "(.+)"$'),
    ("packages/szpont-npm/package.json", r'(?m)^  "version": "(.+)",$'),
    ("packages/szpont-npm/src/launcher.js", r"(?m)^export const VERSION = '(.+)';$"),
)

NPM_URL = "https://registry.npmjs.org/szpont"
PYPI_URL = "https://pypi.org/pypi/szpont/json"


class VersionError(Exception):
    """The tree, or an index, is not in a state a release can be chosen from."""


def _sites(root: Path):
    for rel, pattern in SITES:
        path = root / rel
        text = path.read_text(encoding="utf-8")
        found = list(re.finditer(pattern, text))
        if len(found) != 1:
            raise VersionError(f"{rel}: {len(found)} lines match {pattern!r}, expected 1")
        yield rel, path, text, found[0]


def read(root: Path) -> str:
    """The version the tree states, refusing a tree that states more than one."""
    stated = {rel: match.group(1) for rel, _path, _text, match in _sites(root)}
    if len(set(stated.values())) != 1:
        raise VersionError("the tree disagrees with itself:\n" + json.dumps(stated, indent=2))
    return next(iter(stated.values()))


def write(root: Path, version: str) -> None:
    """Put `version` in every file that states one.

    Only a release off main writes, and it writes what `next-minor` chose, so
    anything else reaching here is a version that got lost on the way - an unset
    job output arrives as the empty string, and an empty string would be built,
    uploaded and unrecallable.
    """
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise VersionError(f"{version!r} is not a version to publish under")
    for _rel, path, text, match in _sites(root):
        path.write_text(text[: match.start(1)] + version + text[match.end(1) :], encoding="utf-8")


def next_minor(floors: list[str]) -> str:
    """One minor above the highest of `floors`, which are versions already taken.

    Highest by `(major, minor)` read as numbers, so 0.10.0 outranks 0.9.0 and a
    later major outranks any minor under an earlier one.

    A floor that is not `major.minor.patch` is one no release here produced - a
    prerelease, a date, something hand-uploaded - and only its `major.minor` is
    worth reading, so it is read leniently and never rejected: the point is a
    number above all of them, not a judgement about any one of them.
    """
    seen = [(int(m.group(1)), int(m.group(2)))
            for f in floors if (m := re.match(r"(\d+)\.(\d+)", f))]
    if not seen:
        raise VersionError(f"no version to count from in {floors}")
    major, minor = max(seen)
    return f"{major}.{minor + 1}.0"


def _fetch(url: str):
    """The index's answer, or None if it has never heard of the package.

    Any other failure is an index we could not read, and choosing a version without
    reading it is how a run picks one that is taken.
    """
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def published() -> list[str]:
    """Every version of `szpont` npm and PyPI hold, yanked ones included."""
    npm = _fetch(NPM_URL)
    pypi = _fetch(PYPI_URL)
    return list(npm["versions"] if npm else []) + list(pypi["releases"] if pypi else [])


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("show", help="print the version the tree states")
    setter = commands.add_parser("set", help="write a version into every file that states one")
    setter.add_argument("version")
    commands.add_parser("next-minor", help="print the next minor over the tree and both indexes")
    args = parser.parse_args(argv)

    if args.command == "show":
        print(read(ROOT))
    elif args.command == "set":
        write(ROOT, args.version)
    else:
        print(next_minor([read(ROOT), *published()]))


if __name__ == "__main__":
    try:
        main()
    except VersionError as exc:
        raise SystemExit(f"error: {exc}")
