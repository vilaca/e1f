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
from e1f.experimental.common import init_lookthrough_schema
from e1f.fetch import DataExtractor
from e1f.transactions import TradeRepublicImporter

ROOT = Path(__file__).resolve().parents[1]


def test_cli_commands_surface():
    """Freeze the public CLI surface — adding a command without updating this set fails."""
    assert set(COMMANDS.keys()) == {
        "autocomplete",
        "config",
        "fetch",
        "validate",
        "transactions",
        "portfolio",
        "performance",
        "correlation",
        "rebalance",
        "scenario",
        "concentration",
        "overlap",
        "backtest",
        "lookthrough",
    }


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


def test_fx_rates_schema_contract(tmp_path: Path) -> None:
    """fx_rates table schema is a data contract — changes need an ADR-0010 note."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.dump({"etfs": {}}))
    db = tmp_path / "fx.db"

    DataExtractor(
        config_path=str(cfg),
        db_path=str(db),
        currency_meta_path=str(tmp_path / "meta.yaml"),
    )

    with closing(sqlite3.connect(str(db))) as conn:
        cols = conn.execute("PRAGMA table_info(fx_rates)").fetchall()

    schema = {row[1]: {"type": row[2], "pk": row[5]} for row in cols}
    assert schema == {
        "base":  {"type": "TEXT", "pk": 1},
        "quote": {"type": "TEXT", "pk": 2},
        "date":  {"type": "TEXT", "pk": 3},
        "rate":  {"type": "REAL", "pk": 0},
    }


def _lookthrough_schema(tmp_path: Path) -> sqlite3.Connection:
    # Look-through schema now belongs to the experimental tier (ADR-0024); stable
    # `fetch` no longer creates it, so build it directly for the contract check.
    db = tmp_path / "lookthrough.db"
    conn = sqlite3.connect(str(db))
    init_lookthrough_schema(conn)
    conn.commit()
    return conn


def test_holdings_snapshot_schema_contract(tmp_path: Path) -> None:
    """holdings_snapshot schema is a data contract — changes need an ADR-0012 note."""
    with closing(_lookthrough_schema(tmp_path)) as conn:
        cols = conn.execute("PRAGMA table_info(holdings_snapshot)").fetchall()

    schema = {row[1]: {"type": row[2], "pk": row[5]} for row in cols}
    assert schema == {
        "id": {"type": "INTEGER", "pk": 1},
        "fund_id": {"type": "TEXT", "pk": 0},
        "as_of": {"type": "TEXT", "pk": 0},
        "source": {"type": "TEXT", "pk": 0},
        "tier": {"type": "TEXT", "pk": 0},
        "retrieved_at": {"type": "TEXT", "pk": 0},
        "reported_holding_count": {"type": "INTEGER", "pk": 0},
    }


def test_holding_schema_contract(tmp_path: Path) -> None:
    """holding schema is a data contract — changes need an ADR-0012 note.

    The ``dimension`` discriminator carries all three look-through dimensions
    (security / sector / asset_class) within the ADR-0012 three-table design.
    """
    with closing(_lookthrough_schema(tmp_path)) as conn:
        cols = conn.execute("PRAGMA table_info(holding)").fetchall()

    schema = {row[1]: {"type": row[2], "pk": row[5]} for row in cols}
    assert schema == {
        "snapshot_id": {"type": "INTEGER", "pk": 0},
        "dimension": {"type": "TEXT", "pk": 0},
        "raw_name": {"type": "TEXT", "pk": 0},
        "normalized_name": {"type": "TEXT", "pk": 0},
        "weight": {"type": "REAL", "pk": 0},
        "rank": {"type": "INTEGER", "pk": 0},
    }


def test_security_alias_schema_contract(tmp_path: Path) -> None:
    """security_alias schema is a data contract — empty in v1a, filled by v1b."""
    with closing(_lookthrough_schema(tmp_path)) as conn:
        cols = conn.execute("PRAGMA table_info(security_alias)").fetchall()

    schema = {row[1]: {"type": row[2], "pk": row[5]} for row in cols}
    assert schema == {
        "raw_name": {"type": "TEXT", "pk": 1},
        "canonical_name": {"type": "TEXT", "pk": 0},
        "canonical_key": {"type": "TEXT", "pk": 0},
        "reviewed_at": {"type": "TEXT", "pk": 0},
    }


def test_transactions_schema_contract(tmp_path: Path) -> None:
    """transactions table schema is a data contract — changes need ADR-0004 note."""
    db = tmp_path / "transactions.db"
    TradeRepublicImporter(db_path=str(db))

    with closing(sqlite3.connect(str(db))) as conn:
        cols = conn.execute("PRAGMA table_info(transactions)").fetchall()

    schema = {row[1]: {"type": row[2], "pk": row[5]} for row in cols}
    assert schema == {
        "broker": {"type": "TEXT", "pk": 1},
        "transaction_id": {"type": "TEXT", "pk": 2},
        "datetime": {"type": "TEXT", "pk": 0},
        "symbol": {"type": "TEXT", "pk": 0},
        "side": {"type": "TEXT", "pk": 0},
        "shares": {"type": "REAL", "pk": 0},
        "price": {"type": "REAL", "pk": 0},
        "fee": {"type": "REAL", "pk": 0},
        "tax": {"type": "REAL", "pk": 0},
    }
