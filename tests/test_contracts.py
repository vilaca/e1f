"""Deterministic contract tests: README claims must match code reality.

Catches drift that static analysis cannot: prose that refers to code shapes
(CLI surface, Python version) and silently diverges as code evolves.
"""

import re
import tomllib
from pathlib import Path

from e1f.cli import COMMANDS

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
