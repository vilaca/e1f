"""Crash-safe file persistence primitives, and a parse-once YAML read."""

import copy
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

import yaml

_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)  # libyaml when installed
_YAML_CACHE: dict[str, tuple[tuple[int, int, int], Any]] = {}


def load_yaml(path: str) -> Any:
    """Parse the YAML at ``path`` once per on-disk version; each call gets its own copy.

    A command can read the universe or currency metadata once per row, so the parsed
    document is cached keyed by inode, size and mtime: a write (atomic or in place)
    changes the key and the next read re-parses. The copy keeps callers that mutate
    the result (``ConfigManager``) from editing the cache.
    """
    stat = os.stat(path)
    key = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
    cached = _YAML_CACHE.get(os.path.realpath(path))
    if cached is None or cached[0] != key:
        with open(path) as stream:
            cached = (key, yaml.load(stream, Loader=_LOADER))
        _YAML_CACHE[os.path.realpath(path)] = cached
    return copy.deepcopy(cached[1])


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
