"""CLI dispatch: routes the first token, forwards the rest to the subcommand."""

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


def test_real_commands_are_registered():
    import e1f.config as config_mod
    import e1f.fetch as fetch_mod
    import e1f.portfolio as portfolio_mod
    import e1f.transactions as transactions_mod
    import e1f.validate as validate_mod

    assert cli.COMMANDS['config'] is config_mod.main
    assert cli.COMMANDS['fetch'] is fetch_mod.main
    assert cli.COMMANDS['validate'] is validate_mod.main
    assert cli.COMMANDS['transactions'] is transactions_mod.main
    assert cli.COMMANDS['portfolio'] is portfolio_mod.main
