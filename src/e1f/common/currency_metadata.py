"""Typed boundary for pinned fund-currency and FX-pair metadata."""

import os
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from .defaults import DEFAULT_CURRENCY_META
from .persistence import atomic_write_yaml


_FX_PAIR_RE = re.compile(r"^[A-Z]{6}$")


class CurrencyMetadataError(ValueError):
    """The currency metadata file has an invalid shape."""


@dataclass
class CurrencyMetadata:
    """Fund pins and reserved FX pins kept in separate typed collections."""

    funds: dict[str, dict[str, Any]] = field(default_factory=dict)
    fx_pairs: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str = DEFAULT_CURRENCY_META) -> "CurrencyMetadata":
        if not os.path.exists(path):
            return cls()
        with open(path) as stream:
            loaded = yaml.safe_load(stream)
        raw = {} if loaded is None else loaded
        if not isinstance(raw, dict):
            raise CurrencyMetadataError(f"{path}: root must be a mapping")

        funds: dict[str, dict[str, Any]] = {}
        fx_pairs: dict[str, dict[str, Any]] = {}
        for key, value in raw.items():
            name = str(key)
            if name == "fx_pairs":
                if not isinstance(value, dict):
                    raise CurrencyMetadataError(f"{path}: 'fx_pairs' must be a mapping")
                for pair, pin in value.items():
                    pair_name = str(pair)
                    if not _FX_PAIR_RE.fullmatch(pair_name) or not isinstance(pin, dict):
                        raise CurrencyMetadataError(
                            f"{path}: invalid FX-pair metadata entry {pair_name!r}"
                        )
                    fx_pairs[pair_name] = dict(pin)
                continue
            if not name or not isinstance(value, dict):
                raise CurrencyMetadataError(f"{path}: invalid fund metadata entry {name!r}")
            funds[name] = dict(value)
        return cls(funds=funds, fx_pairs=fx_pairs)

    def as_yaml(self) -> dict[str, Any]:
        body: dict[str, Any] = dict(self.funds)
        if self.fx_pairs:
            body["fx_pairs"] = dict(self.fx_pairs)
        return body

    def save(self, path: str = DEFAULT_CURRENCY_META) -> None:
        atomic_write_yaml(path, self.as_yaml(), sort_keys=True)
