"""Crash-safe file persistence primitives."""

import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

import yaml


def atomic_write_yaml(path: str, value: Any, *, sort_keys: bool) -> None:
    """Write YAML completely, then atomically replace the destination."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w") as stream:
            yaml.dump(value, stream, default_flow_style=False, sort_keys=sort_keys)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
        raise
