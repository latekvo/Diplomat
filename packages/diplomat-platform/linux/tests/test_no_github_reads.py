"""The conftest's refusal of the real ``gh`` holds past the test that started a poll."""

from __future__ import annotations

import threading

import pytest

from diplomat_runtime import gh

_test_over = threading.Event()
_outcome: list[BaseException | None] = []


def _late_poll() -> None:
    _test_over.wait(5)
    try:
        gh.run(["--version"])
        _outcome.append(None)
    except BaseException as exc:  # noqa: BLE001 - the outcome is the assertion
        _outcome.append(exc)


@pytest.fixture(scope="module")
def poll_outliving_its_test():
    thread = threading.Thread(target=_late_poll, daemon=True)
    thread.start()
    yield
    # Module teardown: every function-scoped patch of this module's test is undone.
    _test_over.set()
    thread.join(5)
    assert _outcome and isinstance(_outcome[0], AssertionError), (
        f"a background poll reached the real gh after its test ended: {_outcome}")


def test_a_poll_that_outlives_its_test_is_still_refused(poll_outliving_its_test):
    pass
