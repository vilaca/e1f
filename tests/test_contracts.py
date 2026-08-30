"""Deterministic contract tests: README claims and DB schema must match code reality.

Catches drift that static analysis cannot: prose that refers to code shapes
(CLI surface, Python version) and silently diverges as code evolves. Also
freezes the SQLite schema as a data contract — column/type changes without a
recorded migration break this test.
"""

import ast
import re
import sqlite3
import tomllib
from contextlib import closing
from pathlib import Path
from urllib.parse import unquote

import yaml

from e1f.common import defaults
from e1f.cli import COMMANDS, STABLE_COMMANDS
from e1f.experimental.common import init_lookthrough_schema
from e1f.fetch import DataExtractor
from e1f.transactions import TradeRepublicImporter

ROOT = Path(__file__).resolve().parents[1]


def _documentation_files() -> list[Path]:
    return [
        ROOT / "README.md",
        ROOT / "CLAUDE.md",
        ROOT / "metrics-roadmap.md",
        ROOT / "data/glossary.md",
        *(ROOT / "specs").glob("*.md"),
        *(ROOT / "ADR").glob("*.md"),
    ]


def _open_mode(call: ast.Call) -> str | None:
    positional_index = 1 if isinstance(call.func, ast.Name) else 0
    if len(call.args) > positional_index:
        value = call.args[positional_index]
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
        return "?"
    for keyword in call.keywords:
        if keyword.arg == "mode":
            value = keyword.value
            return (
                value.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str)
                else "?"
            )
    return None


def test_production_file_writes_use_atomic_persistence_boundary() -> None:
    """Crash-sensitive file writes belong exclusively to common.persistence."""
    allowed = Path("src/e1f/common/persistence.py")
    violations: list[str] = []
    for path in (ROOT / "src/e1f").rglob("*.py"):
        relative = path.relative_to(ROOT)
        if relative == allowed:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            api: str | None = None
            if isinstance(function, ast.Attribute):
                if function.attr in {"write_text", "write_bytes"}:
                    api = function.attr
                elif (
                    isinstance(function.value, ast.Name)
                    and function.value.id == "yaml"
                    and function.attr in {"dump", "safe_dump"}
                ):
                    api = f"yaml.{function.attr}"
                elif (
                    isinstance(function.value, ast.Name)
                    and function.value.id in {"os", "tempfile"}
                    and function.attr in {"fdopen", "replace", "mkstemp", "NamedTemporaryFile"}
                ):
                    api = f"{function.value.id}.{function.attr}"
                elif function.attr == "open":
                    mode = _open_mode(node)
                    if mode == "?" or (mode and any(flag in mode for flag in "wax+")):
                        api = f"open(mode={mode})"
            elif isinstance(function, ast.Name) and function.id == "open":
                mode = _open_mode(node)
                if mode == "?" or (mode and any(flag in mode for flag in "wax+")):
                    api = f"open(mode={mode})"
            if api is not None:
                violations.append(f"{relative}:{node.lineno} uses {api}")

    assert violations == [], (
        "production file writes must use e1f.common.persistence.atomic_write_yaml:\n"
        + "\n".join(violations)
    )


def test_cli_commands_surface():
    """Freeze the public CLI surface — adding a command without updating this set fails."""
    assert set(COMMANDS.keys()) == {
        "autocomplete",
        "config",
        "fetch",
        "funds",
        "validate",
        "transactions",
        "portfolio",
        "performance",
        "benchmark",
        "deposits",
        "correlation",
        "rebalance",
        "scenario",
        "glossary",
        "concentration",
        "overlap",
        "backtest",
        "lookthrough",
        "seasonality",
    }


def test_layer_contracts_cover_every_stable_command() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    contracts = {
        contract["name"]: contract for contract in pyproject["tool"]["importlinter"]["contracts"]
    }
    expected = {f"e1f.{command}" for command in STABLE_COMMANDS}

    command_layer = contracts["Module layers: cli -> command modules -> common"]["layers"][1]
    assert {module.strip() for module in command_layer.split("|")} == expected
    forbidden_sources = set(
        contracts[
            "Experimental tier is isolated: only cli may import it (ADR-0024)"
        ]["source_modules"]
    )
    assert forbidden_sources == expected | {"e1f.common"}


def test_readme_python_version_matches_pyproject():
    """README 'Requires Python X.Y+' must agree with pyproject.toml requires-python."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    requires = pyproject["project"]["requires-python"]  # e.g. ">=3.14"
    min_version = re.search(r"[\d.]+", requires).group()  # type: ignore[union-attr]

    readme = (ROOT / "README.md").read_text()
    assert re.search(
        rf"Requires Python.{{0,5}}{re.escape(min_version)}", readme
    ), (
        f"README does not mention 'Requires Python ... {min_version}' "
        f"(pyproject requires-python = {requires!r})"
    )


def test_markdown_relative_links_resolve() -> None:
    broken: list[str] = []
    for document in _documentation_files():
        for line_number, line in enumerate(document.read_text().splitlines(), start=1):
            for match in re.finditer(r"\]\(([^)]+)\)", line):
                raw_target = match.group(1).strip().strip("<>")
                target = raw_target.split(maxsplit=1)[0]
                if (
                    not target
                    or target.startswith(("#", "http://", "https://", "mailto:"))
                ):
                    continue
                relative = unquote(target.split("#", 1)[0])
                if not (document.parent / relative).resolve().exists():
                    broken.append(
                        f"{document.relative_to(ROOT)}:{line_number} -> {raw_target}"
                    )
    assert broken == [], "broken relative Markdown links:\n" + "\n".join(broken)


def test_adr_sequence_and_identity() -> None:
    paths = sorted((ROOT / "ADR").glob("ADR-*.md"))
    numbered = [
        (int(match.group(1)), path)
        for path in paths
        if (match := re.fullmatch(r"ADR-(\d{4})_.+\.md", path.name))
    ]
    assert len(numbered) == len(paths)
    assert [number for number, _path in numbered] == list(range(1, len(paths) + 1))
    for number, path in numbered:
        text = path.read_text()
        assert text.startswith(f"# ADR-{number:04d} —"), path.name
        assert "**Scope:**" in text, path.name
        assert "## Context" in text, path.name
        assert re.search(r"^## Decisions?$", text, re.MULTILINE), path.name


def test_readme_and_claude_reference_every_cli_command() -> None:
    readme = (ROOT / "README.md").read_text()
    claude = (ROOT / "CLAUDE.md").read_text()
    missing_readme = sorted(command for command in COMMANDS if f"e1f {command}" not in readme)
    missing_claude = sorted(command for command in COMMANDS if f"{command}.py" not in claude)
    assert missing_readme == []
    assert missing_claude == []


def test_claude_check_gates_match_check_script() -> None:
    check_script = (ROOT / "scripts/check.sh").read_text()
    assignments = re.findall(r"gates=\(([^)]+)\)", check_script)
    default = next(value for value in assignments if value.strip().startswith("lint "))
    gates = default.split()
    claude = (ROOT / "CLAUDE.md").read_text()
    running_checks = claude.split("## Running checks", 1)[1].split("## ", 1)[0]
    missing = [gate for gate in gates if gate not in running_checks]
    assert missing == []


def test_wheel_data_files_match_packaged_runtime_defaults():
    """Immutable packaged defaults must agree with their runtime path constants."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    shipped = {
        Path(path).name
        for path in pyproject["tool"]["setuptools"]["data-files"]["share/e1f"]
    }
    expected = {
        Path(defaults.DEFAULT_CONFIG).name,
        Path(defaults.DEFAULT_CURRENCY_META).name,
        Path(defaults.DEFAULT_GLOSSARY).name,
    }
    assert shipped == expected


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

    schema = {
        row[1]: {"type": row[2], "notnull": bool(row[3]), "pk": row[5]}
        for row in cols
    }
    assert schema == {
        "broker": {"type": "TEXT", "notnull": True, "pk": 1},
        "transaction_id": {"type": "TEXT", "notnull": True, "pk": 2},
        "datetime": {"type": "TEXT", "notnull": False, "pk": 0},
        "symbol": {"type": "TEXT", "notnull": False, "pk": 0},
        "side": {"type": "TEXT", "notnull": True, "pk": 0},
        "shares": {"type": "REAL", "notnull": True, "pk": 0},
        "price": {"type": "REAL", "notnull": True, "pk": 0},
        "fee": {"type": "REAL", "notnull": False, "pk": 0},
        "tax": {"type": "REAL", "notnull": False, "pk": 0},
    }


def test_canonical_sort_tokens_agree_across_commands() -> None:
    """Same quantity → same --sort token (ADR-0037); retired nicknames are gone."""
    from e1f import (
        benchmark,
        config,
        deposits,
        funds,
        performance,
        portfolio,
        rebalance,
        transactions,
    )

    identity = {"isin", "name"}
    assert identity <= set(portfolio.SORT_FIELDS)
    assert identity <= set(performance.SORT_FIELDS)
    assert identity <= set(deposits.SORT_FIELDS)
    assert identity <= set(benchmark.SORT_FIELDS)
    assert "twr" in benchmark.SORT_FIELDS
    assert identity <= set(funds.SORT_FIELDS)
    assert {"twr", "vol", "maxdd", "ter", "class", "n"} <= set(funds.SORT_FIELDS)
    assert identity <= set(rebalance.SORT_FIELDS)
    assert identity <= set(config.SORT_FIELDS)
    assert "isin" in transactions.SORT_FIELDS

    money = {"value", "cost"}
    assert money <= set(portfolio.SORT_FIELDS)
    assert money <= set(performance.SORT_FIELDS)
    assert money <= set(deposits.SORT_FIELDS)
    assert "value" in rebalance.SORT_FIELDS

    assert {"pnl", "pnl_pct", "pnl_ctr"} <= set(performance.SORT_FIELDS)
    assert {"pnl", "pnl_pct", "pnl_ctr"} <= set(deposits.SORT_FIELDS)
    assert "weight" in portfolio.SORT_FIELDS
    assert "weight" in performance.SORT_FIELDS
    assert "weight" in rebalance.SORT_FIELDS
    assert "class" in portfolio.SORT_FIELDS
    assert "class" in config.SORT_FIELDS
    assert "units" in portfolio.SORT_FIELDS
    assert "units" in transactions.SORT_FIELDS
    assert "date" in deposits.SORT_FIELDS
    assert "date" in transactions.SORT_FIELDS

    assert "total" not in portfolio.SORT_FIELDS
    assert "gain" not in deposits.SORT_FIELDS
    assert "amount" not in deposits.SORT_FIELDS
    assert "ret" not in deposits.SORT_FIELDS
