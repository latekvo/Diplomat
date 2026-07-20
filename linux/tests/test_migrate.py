"""Tests for the pre-rename state-dir migration (``~/.argent/mesh`` →
``~/.diplomat/mesh``).

The property that matters most: the mesh **identity** (``device.key`` + ``node.json``)
survives the rename byte-for-byte, because peers pin their trust to it — a
regenerated identity would silently break this node across the whole fleet. The
migration must also leave the shared ``~/.argent`` parent (and the daemon-owned
subdirs it does not own) untouched.
"""

from __future__ import annotations

import os

import pytest

from diplomat_app.migrate import migrate_legacy_state_dir


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("DIPLOMAT_MESH_DIR", raising=False)
    # Path.home() caches nothing; it re-reads $HOME each call on POSIX.
    assert os.path.expanduser("~") == str(tmp_path)
    return tmp_path


def _seed_legacy_mesh(home):
    d = home / ".argent" / "mesh"
    d.mkdir(parents=True)
    (d / "device.key").write_text("SECRET-ED25519-KEY")
    (d / "node.json").write_text('{"id": "abc-123"}')
    (d / "trusted.json").write_text('{"keys": ["peer-fp"]}')
    return d


def test_migrates_identity_preserving_bytes(fake_home):
    _seed_legacy_mesh(fake_home)
    migrate_legacy_state_dir()

    new = fake_home / ".diplomat" / "mesh"
    assert (new / "device.key").read_text() == "SECRET-ED25519-KEY"
    assert (new / "node.json").read_text() == '{"id": "abc-123"}'
    assert (new / "trusted.json").read_text() == '{"keys": ["peer-fp"]}'
    # legacy mesh dir is emptied and removed
    assert not (fake_home / ".argent" / "mesh").exists()


def test_leaves_shared_parent_and_daemon_dirs_untouched(fake_home):
    _seed_legacy_mesh(fake_home)
    # The OTHER Argent tool's file + the daemon-owned subdirs (migrated elsewhere,
    # by install.js) must not be touched by the applet's mesh-only migration.
    (fake_home / ".argent" / "tool-server.json").write_text("other-argent")
    (fake_home / ".argent" / "device-allocator").mkdir()
    (fake_home / ".argent" / "device-allocator" / "state.json").write_text("{}")

    migrate_legacy_state_dir()

    assert (fake_home / ".argent" / "tool-server.json").read_text() == "other-argent"
    assert (fake_home / ".argent" / "device-allocator" / "state.json").exists()


def test_merge_does_not_clobber_newer_data(fake_home):
    _seed_legacy_mesh(fake_home)
    # An applet that started once post-update may have already minted a fresh
    # node.json at the new path; migration must keep that and NOT overwrite it,
    # while still bringing over the files it lacks (device.key).
    new = fake_home / ".diplomat" / "mesh"
    new.mkdir(parents=True)
    (new / "node.json").write_text('{"id": "NEW"}')

    migrate_legacy_state_dir()

    assert (new / "node.json").read_text() == '{"id": "NEW"}'  # kept
    assert (new / "device.key").read_text() == "SECRET-ED25519-KEY"  # brought over


def test_idempotent_and_noop_on_clean_machine(fake_home):
    # No legacy dir at all → no error, nothing created.
    migrate_legacy_state_dir()
    assert not (fake_home / ".diplomat").exists()

    # After a real migration, a second run is a clean no-op.
    _seed_legacy_mesh(fake_home)
    migrate_legacy_state_dir()
    migrate_legacy_state_dir()
    assert (fake_home / ".diplomat" / "mesh" / "device.key").read_text() == "SECRET-ED25519-KEY"


def test_env_override_skips_migration(fake_home, monkeypatch):
    # A DIPLOMAT_MESH_DIR override means the caller owns the path — never migrate.
    _seed_legacy_mesh(fake_home)
    monkeypatch.setenv("DIPLOMAT_MESH_DIR", str(fake_home / "custom"))
    migrate_legacy_state_dir()
    assert (fake_home / ".argent" / "mesh" / "device.key").exists()  # left in place
    assert not (fake_home / ".diplomat").exists()
