"""Deterministic contract tests: README claims and DB schema must match code reality.

Catches drift that static analysis cannot: prose that refers to code shapes
(CLI surface, Python version) and silently diverges as code evolves. Also
freezes the SQLite schema as a data contract — column/type changes without a
recorded migration break this test.
"""

import re
import sqlite3
import tomllib
from contextlib import closing
from pathlib import Path

import yaml

from e1f.cli import COMMANDS
from e1f.fetch import DataExtractor

ROOT = Path(__file__).resolve().parents[1]


def test_cli_commands_surface():
    """Freeze the public CLI surface — adding a command without updating this set fails."""
    assert set(COMMANDS.keys()) == {"config", "fetch"}


def test_readme_python_version_matches_pyproject():
    """README 'Requires Python X.Y+' must agree with pyproject.toml requires-python."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    requires = pyproject["project"]["requires-python"]  # e.g. ">=3.11"
    min_version = re.search(r"[\d.]+", requires).group()  # type: ignore[union-attr]

    readme = (ROOT / "README.md").read_text()
    assert re.search(
        rf"Requires Python.{{0,5}}{re.escape(min_version)}", readme
    ), (
        f"README does not mention 'Requires Python ... {min_version}' "
        f"(pyproject requires-python = {requires!r})"
    )


def test_prices_schema_contract(tmp_path: Path) -> None:
    """prices table schema is a data contract — column/type/PK changes break this test.

    To change the schema: update this assertion AND add a migration note to ADR-0002.
    """
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.dump({"etfs": {}}))
    db = tmp_path / "prices.db"

    DataExtractor(
        config_path=str(cfg),
        db_path=str(db),
        currency_meta_path=str(tmp_path / "meta.yaml"),
    )

    with closing(sqlite3.connect(str(db))) as conn:
        # (cid, name, type, notnull, dflt_value, pk)
        cols = conn.execute("PRAGMA table_info(prices)").fetchall()

    schema = {row[1]: {"type": row[2], "pk": row[5]} for row in cols}
    assert schema == {
        "isin":  {"type": "TEXT", "pk": 1},
        "date":  {"type": "TEXT", "pk": 2},
        "close": {"type": "REAL", "pk": 0},
    }
