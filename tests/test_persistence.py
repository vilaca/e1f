"""Crash-safe YAML persistence."""

import pytest
import yaml

from e1f.common import persistence


def test_atomic_write_yaml_replaces_complete_document(tmp_path):
    path = tmp_path / "state.yaml"
    path.write_text("old: true\n")

    persistence.atomic_write_yaml(str(path), {"new": True}, sort_keys=True)

    assert yaml.safe_load(path.read_text()) == {"new": True}


def test_atomic_write_yaml_failure_preserves_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "state.yaml"
    original = "old: true\n"
    path.write_text(original)

    def fail_after_partial_write(_value, stream, **_kwargs):
        stream.write("partial:")
        raise OSError("simulated write failure")

    monkeypatch.setattr(persistence.yaml, "dump", fail_after_partial_write)
    with pytest.raises(OSError, match="simulated write failure"):
        persistence.atomic_write_yaml(str(path), {"new": True}, sort_keys=True)

    assert path.read_text() == original
    assert list(tmp_path.glob("*.tmp")) == []


def test_load_yaml_parses_once_per_version_and_hands_out_copies(tmp_path, monkeypatch):
    import os

    path = tmp_path / "c.yaml"
    path.write_text("etfs:\n  A: {name: Fund A}\n")
    calls = []
    real_load = yaml.load
    monkeypatch.setattr(persistence.yaml, "load",
                        lambda stream, Loader: calls.append(1) or real_load(stream, Loader))

    first = persistence.load_yaml(str(path))
    first["etfs"]["A"]["name"] = "edited"  # a caller's mutation stays its own
    assert persistence.load_yaml(str(path)) == {"etfs": {"A": {"name": "Fund A"}}}
    assert len(calls) == 1

    persistence.atomic_write_yaml(str(path), {"etfs": {"B": {"name": "Fund B"}}},
                                  sort_keys=False)
    assert persistence.load_yaml(str(path)) == {"etfs": {"B": {"name": "Fund B"}}}
    path.write_text("etfs:\n  C: {name: Fund C}\n")  # in-place rewrite, same inode
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))
    assert persistence.load_yaml(str(path)) == {"etfs": {"C": {"name": "Fund C"}}}
    assert len(calls) == 3


def test_config_manager_sees_its_own_writes(tmp_path):
    from e1f.common import ConfigManager

    path = str(tmp_path / "u.yaml")
    manager = ConfigManager(path)
    manager.config["etfs"]["IE00FUND000A0"] = {"name": "Fund A"}
    manager._save_config()
    assert ConfigManager(path).get("IE00FUND000A0") == {"name": "Fund A"}
    assert ConfigManager(path).config is not ConfigManager(path).config
