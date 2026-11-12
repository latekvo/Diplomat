"""The shared config survives a corrupt file on disk.

``~/.diplomat/config.json`` is best-effort: a file that will not parse means "no
settings", and the guard that has to hold that up is easy to write *almost* right.
``(OSError, json.JSONDecodeError)`` looks exhaustive but does not cover the
``UnicodeDecodeError`` that ``Path.read_text`` raises on non-UTF-8 bytes, and under
that guard a single corrupt byte raises out of every spawn.
"""

from __future__ import annotations

import pytest

from diplomat_runtime import appconfig

# The ways a state file goes bad in the field: a truncated write, a hand-edit
# that left the wrong shape, and bytes that aren't UTF-8 at all (a partially
# overwritten file, a bad disk block, a half-flushed rename).
CORRUPT_BODIES = [
    pytest.param(b'{"addr": "10.0.0.1\xff"}', id="non-utf8-bytes"),
    pytest.param(b'{"truncated": ', id="truncated-json"),
    pytest.param(b"[1, 2, 3]", id="json-array-not-object"),
    pytest.param(b'"just a string"', id="json-scalar-not-object"),
    pytest.param(b"", id="empty-file"),
    pytest.param(b"\x00\x00\x00\x00", id="nul-bytes"),
]


@pytest.mark.parametrize("body", CORRUPT_BODIES)
def test_app_config_falls_back_to_defaults(tmp_path, monkeypatch, body):
    cfg = tmp_path / "config.json"
    cfg.write_bytes(body)
    monkeypatch.setenv("DIPLOMAT_CONFIG", str(cfg))
    assert appconfig.read() == {}
    assert appconfig.get("repoRoot", "fallback") == "fallback"
