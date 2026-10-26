"""The device-allocator daemon's two idle limits, as Settings reads and writes them in
the shared config. The daemon is the only consumer, so a value the UI misreads is a
limit the operator sees one way and the daemon enforces another.
"""

from __future__ import annotations

import json

import pytest

from diplomat_runtime import appconfig

LIMITS = [
    (appconfig.DEVICE_IDLE_FREE_MINUTES, appconfig.device_idle_free_minutes,
     "device_idle_free_minutes", 5, 60),
    (appconfig.DEVICE_IDLE_LEASED_MINUTES, appconfig.device_idle_leased_minutes,
     "device_idle_leased_minutes", 30, 240),
]


def _hand_edit(key: str, value) -> None:
    appconfig.path().parent.mkdir(parents=True, exist_ok=True)
    appconfig.path().write_text(json.dumps({key: value}), encoding="utf-8")


def test_the_keys_match_the_daemon_and_the_macos_twin():
    assert appconfig.DEVICE_IDLE_FREE_MINUTES == "deviceIdleFreeMinutes"
    assert appconfig.DEVICE_IDLE_LEASED_MINUTES == "deviceIdleLeasedMinutes"


@pytest.mark.parametrize("key,getter,_attr,default,_maximum", LIMITS)
def test_an_absent_key_reads_as_its_default(key, getter, _attr, default, _maximum):
    assert getter() == default


@pytest.mark.parametrize("key,getter,_attr,default,_maximum", LIMITS)
@pytest.mark.parametrize("junk", [True, False, -1, "10", None, [3]])
def test_a_hand_edit_that_is_not_a_non_negative_integer_reads_as_the_default(
        key, getter, _attr, default, _maximum, junk):
    """``true`` is the trap: a bool is an int in Python, and would read as 1 minute."""
    _hand_edit(key, junk)
    assert getter() == default


@pytest.mark.parametrize("key,getter,_attr,_default,maximum", LIMITS)
def test_zero_reads_as_never_and_a_large_value_is_clamped_for_display(
        key, getter, _attr, _default, maximum):
    _hand_edit(key, 0)
    assert getter() == 0
    _hand_edit(key, 17)
    assert getter() == 17
    _hand_edit(key, 100_000)
    assert getter() == maximum


@pytest.mark.parametrize("key,_getter,attr,default,maximum", LIMITS)
def test_the_store_writes_the_key_clamped(key, _getter, attr, default, maximum):
    pytest.importorskip("PySide6")
    from diplomat_app.store import Store

    store = Store()
    assert getattr(store, attr) == default
    setattr(store, attr, 10)
    assert appconfig.read()[key] == 10
    assert getattr(store, attr) == 10
    setattr(store, attr, -3)
    assert appconfig.read()[key] == 0
    setattr(store, attr, maximum + 1)
    assert appconfig.read()[key] == maximum
