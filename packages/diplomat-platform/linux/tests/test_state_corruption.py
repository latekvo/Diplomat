"""Every persisted-state loader survives a corrupt file on disk.

These files are all best-effort: a peer cache is an accelerator, a trust store
that won't parse means "no entries", a snapshot that won't parse means "no live
node". Each loader's docstring says so, and the guard that has to hold it up is
easy to write *almost* right: ``(OSError, json.JSONDecodeError)`` looks
exhaustive but does not cover the ``UnicodeDecodeError`` that ``Path.read_text``
raises on non-UTF-8 bytes. Under that guard a single corrupt byte in
``~/.diplomat/mesh/peers.json`` propagates out of ``peercache.load()`` and kills
the node at startup — every restart, until the file is deleted by hand.

The loaders share :func:`szpontnet.atomicjson.read_object` now. This is
the end-to-end guard: drive each *real* loader over each way a file can be
corrupt and assert it returns its documented empty value instead of raising.
"""

from __future__ import annotations

import pytest

from diplomat_app import appconfig
from szpontnet import identity, wancache, peercache, statefile, stats, trust

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


@pytest.fixture
def mesh_dir(tmp_path, monkeypatch):
    """Point every mesh state path at a throwaway directory."""
    d = tmp_path / "mesh"
    d.mkdir()
    monkeypatch.setenv("SZPONTNET_DIR", str(d))
    return d


@pytest.mark.parametrize("body", CORRUPT_BODIES)
def test_peer_cache_resets_instead_of_raising(mesh_dir, body):
    (mesh_dir / "peers.json").write_bytes(body)
    assert peercache.load() == {}


@pytest.mark.parametrize("body", CORRUPT_BODIES)
def test_wan_cache_resets_instead_of_raising(mesh_dir, body):
    (mesh_dir / "wan.json").write_bytes(body)
    assert wancache.load() == {}


@pytest.mark.parametrize("body", CORRUPT_BODIES)
def test_trust_store_resets_instead_of_raising(mesh_dir, body):
    """A corrupt trust store must read as *no* trusted devices — never raise, and
    never accidentally trust anyone."""
    (mesh_dir / "trusted.json").write_bytes(body)
    assert trust.load() == {}


@pytest.mark.parametrize("body", CORRUPT_BODIES)
def test_state_snapshot_reads_as_no_live_node(mesh_dir, body):
    """``read_state`` returns ``None`` for a corrupt snapshot; every caller reads
    that as "no node is running here" and relaunches."""
    (mesh_dir / "state.json").write_bytes(body)
    assert statefile.read_state() is None


@pytest.mark.parametrize("body", CORRUPT_BODIES)
def test_stats_fall_back_to_a_fresh_account(mesh_dir, body):
    (mesh_dir / "stats.json").write_bytes(body)
    st = stats.load(now=1000.0)
    assert st.acc == 0.0
    assert st.quota_used == 0.0


@pytest.mark.parametrize("body", CORRUPT_BODIES)
def test_identity_is_regenerated_not_fatal(mesh_dir, body):
    """A corrupt ``node.json`` must not abort startup: the node mints a fresh
    identity instead. This is the loader that made the crash total — it runs
    before anything else the node does."""
    (mesh_dir / "node.json").write_bytes(body)
    node = identity.load()
    assert node.id


@pytest.mark.parametrize("body", CORRUPT_BODIES)
def test_app_config_falls_back_to_defaults(tmp_path, monkeypatch, body):
    cfg = tmp_path / "config.json"
    cfg.write_bytes(body)
    monkeypatch.setenv("DIPLOMAT_CONFIG", str(cfg))
    assert appconfig.read() == {}
    assert appconfig.get("repoRoot", "fallback") == "fallback"
