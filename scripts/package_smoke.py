#!/usr/bin/env python
"""Build and exercise the installed wheel outside the source checkout."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    )


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="e1f-package-smoke-") as tmp:
        work = Path(tmp)
        dist = work / "dist"
        venv = work / "venv"
        _run("uv", "build", "--wheel", "--out-dir", str(dist))
        wheels = list(dist.glob("*.whl"))
        if len(wheels) != 1:
            raise RuntimeError(f"expected one wheel, found {len(wheels)}")

        _run("uv", "venv", str(venv))
        python = venv / "bin" / "python"
        cli = venv / "bin" / "e1f"
        _run("uv", "pip", "install", "--python", str(python), str(wheels[0]))

        glossary = _run(str(cli), "glossary", "TWR", cwd=work).stdout
        if "── TWR ──" not in glossary:
            raise RuntimeError("installed glossary lookup did not render TWR")
        _run(str(cli), "config", "list", cwd=work)
        _run(
            str(python),
            "-c",
            (
                "from pathlib import Path; "
                "from e1f.common import DEFAULT_CONFIG, DEFAULT_CURRENCY_META, DEFAULT_GLOSSARY; "
                "paths = [DEFAULT_CONFIG, DEFAULT_CURRENCY_META, DEFAULT_GLOSSARY]; "
                "assert all(Path(path).is_file() for path in paths), paths"
            ),
            cwd=work,
        )

    print("wheel install and default-data smoke test passed")


if __name__ == "__main__":
    main()
