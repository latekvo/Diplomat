"""The free-disk floor's arithmetic and wording.

The same numbers the Swift smoke's "free-disk floor" section asserts of
``DiskFloor``: the two platforms read one floor from ``assets/config.json`` and
say why work is held in one sentence, so a drift on either side fails here or
there. What the floor does to dispatch and to the self-update, against a real
volume, is test_lowdisk.py.
"""

from __future__ import annotations

import tempfile

import pytest

from diplomat_runtime import diskfloor

_GIB = 1 << 30


def test_the_floor_is_the_shared_assets_five_gib():
    assert diskfloor.floor({}) == 5 * _GIB


def test_the_override_is_in_mib_and_zero_turns_it_off():
    assert diskfloor.floor({diskfloor.ENV_OVERRIDE: "64"}) == 64 << 20
    assert diskfloor.floor({diskfloor.ENV_OVERRIDE: "0"}) == 0


@pytest.mark.parametrize("junk", ["", "-1", "5GiB", "1.5"])
def test_an_override_that_is_not_a_whole_mib_count_falls_back(junk):
    assert diskfloor.floor({diskfloor.ENV_OVERRIDE: junk}) == 5 * _GIB


def test_the_override_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv(diskfloor.ENV_OVERRIDE, "7")
    assert diskfloor.floor() == 7 << 20


def test_a_path_not_made_yet_reads_its_nearest_existing_ancestor():
    tmp = tempfile.gettempdir()
    assert (diskfloor.free(tmp) or 0) > 0
    assert diskfloor.free(tmp + "/diplomat-no-such/deeper") is not None


def test_a_reading_names_its_path_and_floor():
    tmp = tempfile.gettempdir()
    r = diskfloor.lowest([tmp], {diskfloor.ENV_OVERRIDE: "1"})
    assert r is not None
    assert (r.path, r.floor, r.low) == (tmp, 1 << 20, False)


def test_the_lowest_volume_is_the_one_reported(monkeypatch):
    free = {"/a": 9 << 20, "/b": 3 << 20, "/c": 5 << 20}
    monkeypatch.setattr(diskfloor, "free", lambda p: free.get(p))
    r = diskfloor.lowest(["/a", "/b", "/c", "/unreadable"], {diskfloor.ENV_OVERRIDE: "4"})
    assert (r.path, r.free, r.low) == ("/b", 3 << 20, True)


def test_no_floor_or_nothing_readable_leaves_work_ungated(monkeypatch):
    monkeypatch.setattr(diskfloor, "free", lambda p: None)
    assert diskfloor.lowest(["/x"], {diskfloor.ENV_OVERRIDE: "4"}) is None


def test_sizes_and_the_reason_line():
    assert diskfloor.size(5 * _GIB) == "5.0 GiB"
    assert diskfloor.size(1_288_490_189) == "1.2 GiB"
    assert diskfloor.size(0) == "0 MiB"
    assert diskfloor.size(_GIB - 1) == "1023 MiB"
    assert (diskfloor.describe(diskfloor.Reading("/v", 3 << 20, 5 * _GIB))
            == "3 MiB free on /v, below the 5.0 GiB floor")


def test_exactly_the_floor_is_enough_and_a_byte_under_is_not():
    assert not diskfloor.Reading("/v", 5 * _GIB, 5 * _GIB).low
    assert diskfloor.Reading("/v", 5 * _GIB - 1, 5 * _GIB).low
