"""CLI dispatch: routes the first token, forwards the rest to the subcommand."""

import importlib
import subprocess
import sys

import pytest

from e1f import cli


def test_no_args_prints_help_and_returns_1(capsys):
    assert cli.main([]) == 1
    assert 'config' in capsys.readouterr().out


def test_help_returns_0(capsys):
    assert cli.main(['--help']) == 0
    assert 'fetch' in capsys.readouterr().out


def test_unknown_command_exits():
    with pytest.raises(SystemExit):
        cli.main(['bogus'])


def test_dispatch_forwards_remaining_argv(monkeypatch):
    seen = {}

    def fake_main(argv):
        seen['argv'] = argv
        return 0

    monkeypatch.setitem(cli.COMMANDS, 'fetch', fake_main)
    assert cli.main(['fetch', '--force', 'IE00BM67HK77']) == 0
    assert seen['argv'] == ['--force', 'IE00BM67HK77']


def test_dispatch_propagates_return_code(monkeypatch):
    monkeypatch.setitem(cli.COMMANDS, 'config', lambda argv: 1)
    assert cli.main(['config', 'list']) == 1


def test_command_registries_are_complete_disjoint_partitions():
    assert set(cli.STABLE_COMMANDS).isdisjoint(cli.EXPERIMENTAL_COMMANDS)
    assert cli.COMMANDS == {**cli.STABLE_COMMANDS, **cli.EXPERIMENTAL_COMMANDS}
    assert cli.PARSER_FACTORIES == {
        **cli.STABLE_PARSER_FACTORIES,
        **cli.EXPERIMENTAL_PARSER_FACTORIES,
    }
    assert set(cli.COMMANDS) == set(cli.PARSER_FACTORIES)


@pytest.mark.parametrize("name", cli.COMMANDS)
def test_real_commands_and_parsers_are_registered(name, monkeypatch):
    tier = "" if name in cli.STABLE_COMMANDS else "experimental."
    module = importlib.import_module(f"e1f.{tier}{name}")
    assert cli.MODULES[name] == module.__name__

    # The parser factory resolves to the module's own parser builder.
    seen = []
    monkeypatch.setattr(module, "_build_parser", lambda: seen.append(name) or "parser")
    assert cli.PARSER_FACTORIES[name]() == "parser"
    assert seen == [name]

    # Dispatch forwards argv to the module's main (autocomplete also gets the factories).
    calls = []
    if name == "autocomplete":
        monkeypatch.setattr(
            module, "main", lambda argv, factories: calls.append((argv, factories)) or 7
        )
        assert cli.COMMANDS[name](["x"]) == 7
        assert calls == [(["x"], cli.PARSER_FACTORIES)]
    else:
        monkeypatch.setattr(module, "main", lambda argv: calls.append(argv) or 7)
        assert cli.COMMANDS[name](["x"]) == 7
        assert calls == [["x"]]


def test_importing_cli_loads_no_command_module():
    code = (
        "import sys, e1f.cli;"
        "heavy = ('e1f.performance', 'e1f.fetch', 'pandas', 'matplotlib');"
        "bad = [m for m in heavy if m in sys.modules];"
        "sys.exit(1 if bad else 0)"
    )
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0
