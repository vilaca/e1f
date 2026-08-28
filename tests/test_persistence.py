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
