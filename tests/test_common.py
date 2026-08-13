"""ConfigManager (YAML universe) and OpenFIGIResolver (mocked HTTP)."""

import pytest
import requests
import yaml

from e1f.common import ConfigManager, OpenFIGIResolver

ISIN = 'AA0000000001'

RESOLVED = {
    'name': 'Test ETF',
    'tickers': ['TST'],
    'exchange': 'NA',
    'figi': 'BBG000TEST',
}


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.headers = {}

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def resolver_returning(monkeypatch, payload):
    r = OpenFIGIResolver()
    monkeypatch.setattr(r.session, 'post', lambda *a, **k: FakeResponse(payload))
    return r


# ---------------------------------------------------------------------------
# OpenFIGIResolver
# ---------------------------------------------------------------------------

def test_resolve_success(monkeypatch):
    payload = [{'data': [{'name': 'Test ETF', 'ticker': 'TST',
                          'exchCode': 'NA', 'figi': 'BBG000TEST'}]}]
    info = resolver_returning(monkeypatch, payload).resolve(ISIN)
    assert info['name'] == 'Test ETF'
    assert info['tickers'] == ['TST']
    assert info['exchange'] == 'NA'
    assert info['figi'] == 'BBG000TEST'
    assert info['source'] == 'OpenFIGI'


def test_resolve_invalid_isin_short_circuits(monkeypatch):
    r = OpenFIGIResolver()
    monkeypatch.setattr(r.session, 'post',
                        lambda *a, **k: pytest.fail('should not POST'))
    assert r.resolve('not-an-isin') is None


def test_resolve_no_data(monkeypatch):
    assert resolver_returning(monkeypatch, [{'data': []}]).resolve(ISIN) is None
    assert resolver_returning(monkeypatch, [{}]).resolve(ISIN) is None


def test_resolve_api_error_gives_up_after_retries(monkeypatch):
    monkeypatch.setattr('e1f.common.time.sleep', lambda s: None)
    r = OpenFIGIResolver()
    calls = {'n': 0}

    def boom(*a, **k):
        calls['n'] += 1
        raise requests.ConnectionError('down')

    monkeypatch.setattr(r.session, 'post', boom)
    assert r.resolve(ISIN) is None
    assert calls['n'] == 4  # 1 initial + 3 retries


def test_resolve_malformed_payload(monkeypatch, capsys):
    r = resolver_returning(monkeypatch, ['unexpected'])
    assert r.resolve(ISIN) is None
    assert 'Error parsing' in capsys.readouterr().out


# ---------------------------------------------------------------------------
# ConfigManager
# ---------------------------------------------------------------------------

def manager(tmp_path, monkeypatch, resolve_result=RESOLVED):
    monkeypatch.setattr(OpenFIGIResolver, 'resolve',
                        lambda self, isin: resolve_result)
    return ConfigManager(str(tmp_path / 'universe.yaml'))


def test_missing_config_starts_empty(tmp_path, monkeypatch):
    cm = manager(tmp_path, monkeypatch)
    assert cm.list() == []


def test_add_writes_yaml(tmp_path, monkeypatch):
    cm = manager(tmp_path, monkeypatch)
    assert cm.add(ISIN) is True

    on_disk = yaml.safe_load((tmp_path / 'universe.yaml').read_text())
    assert on_disk['etfs'][ISIN]['name'] == 'Test ETF'
    assert cm.get(ISIN)['tickers'] == ['TST']


def test_add_duplicate_refused(tmp_path, monkeypatch):
    cm = manager(tmp_path, monkeypatch)
    assert cm.add(ISIN) is True
    assert cm.add(ISIN) is False
    assert len(cm.list()) == 1


def test_add_failed_resolution(tmp_path, monkeypatch):
    cm = manager(tmp_path, monkeypatch, resolve_result=None)
    assert cm.add(ISIN) is False
    assert cm.list() == []


def test_update_existing_and_missing(tmp_path, monkeypatch):
    cm = manager(tmp_path, monkeypatch)
    assert cm.update(ISIN) is False  # not in config yet
    cm.add(ISIN)
    assert cm.update(ISIN) is True


def test_list_sorted(tmp_path, monkeypatch):
    cm = manager(tmp_path, monkeypatch)
    cm.add('BB0000000002')
    cm.add(ISIN)
    assert [isin for isin, _ in cm.list()] == [ISIN, 'BB0000000002']
