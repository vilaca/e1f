"""Shell completion generation and candidate discovery."""

import pytest

from e1f import cli


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_prints_completion_script(shell, capsys):
    assert cli.main(["autocomplete", shell]) == 0

    output = capsys.readouterr().out
    assert "e1f autocomplete --complete" in output
    assert f"_{'e1f'}" in output


def test_infers_shell_from_environment(monkeypatch, capsys):
    monkeypatch.setenv("SHELL", "/bin/zsh")

    assert cli.main(["autocomplete"]) == 0

    assert "#compdef e1f" in capsys.readouterr().out


def test_completes_top_level_commands(capsys):
    assert cli.main(["autocomplete", "--complete", ""]) == 0

    assert set(capsys.readouterr().out.splitlines()) == set(cli.COMMANDS)


def test_completes_nested_commands_and_options(capsys):
    assert cli.main(["autocomplete", "--complete", "config", ""]) == 0
    config_candidates = capsys.readouterr().out.splitlines()
    assert {"add", "list", "update", "remove", "trim", "--config"} <= set(
        config_candidates
    )

    assert cli.main(["autocomplete", "--complete", "config", "remove", ""]) == 0
    assert {"--db", "--currency-meta", "--force", "-f"} <= set(
        capsys.readouterr().out.splitlines()
    )

    assert cli.main(["autocomplete", "--complete", "config", "trim", ""]) == 0
    assert {"--db", "--currency-meta", "--force", "-f"} <= set(
        capsys.readouterr().out.splitlines()
    )

    assert cli.main(["autocomplete", "--complete", "portfolio", "--sort", ""]) == 0
    assert set(capsys.readouterr().out.splitlines()) == {
        "broker",
        "isin",
        "name",
        "weight",
        "total",
        "units",
        "avg",
        "ter",
        "fee_yr",
    }


def test_requests_file_completion_for_import_path(capsys):
    assert (
        cli.main(
            ["autocomplete", "--complete", "transactions", "trade-republic", ""]
        )
        == 0
    )

    assert "__E1F_FILES__" in capsys.readouterr().out.splitlines()


def test_rejects_unknown_inferred_shell(monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/fish")

    with pytest.raises(SystemExit):
        cli.main(["autocomplete"])
