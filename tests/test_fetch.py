"""DataExtractor: DB init, cache logic, upserts, fetch orchestration.

Network sources (ftgo / yfinance) are mocked at the module boundary.
"""

import sqlite3

import pandas as pd
import pytest
import requests
import yaml

import e1f.fetch as fetch_mod
from e1f.fetch import DataExtractor

ISIN = 'AA0000000001'
UNIVERSE = {ISIN: {'name': 'Test ETF', 'tickers': ['TST'], 'exchange': '', 'figi': ''}}


def make_extractor(tmp_path, universe=None, **kwargs):
    cfg = tmp_path / 'u.yaml'
    cfg.write_text(yaml.dump({'etfs': UNIVERSE if universe is None else universe}))
    return DataExtractor(
        config_path=str(cfg),
        db_path=str(tmp_path / 't.db'),
        currency_meta_path=str(tmp_path / 'meta.yaml'),
        **kwargs,
    )


def close_df(closes, end='2026-08-12'):
    dates = pd.bdate_range(end=end, periods=len(closes))
    return pd.DataFrame({'Close': closes}, index=dates)


# ---------------------------------------------------------------------------
# Currency helpers
# ---------------------------------------------------------------------------

def test_base_currency():
    assert DataExtractor._base_currency('iShares Core S&P 500 UCITS ETF USD (Acc)') == 'USD'
    assert DataExtractor._base_currency('Vanguard FTSE All-World EUR Dist') == 'EUR'
    assert DataExtractor._base_currency('Some Fund') is None


def test_symbol_currency():
    assert DataExtractor._symbol_currency('CSPX:LSE:USD') == 'USD'
    assert DataExtractor._symbol_currency('CSPX') == ''


# ---------------------------------------------------------------------------
# DB init / persistence
# ---------------------------------------------------------------------------

def test_init_creates_prices_table(tmp_path):
    ext = make_extractor(tmp_path)
    with sqlite3.connect(ext.db_path) as conn:
        cols = conn.execute('PRAGMA table_info(prices)').fetchall()
    assert [c[1] for c in cols] == ['isin', 'date', 'close']


def test_save_and_read_back(tmp_path):
    ext = make_extractor(tmp_path)
    ext._save_prices(ISIN, close_df([100.0, 101.5]))
    stored = ext._stored_series(ISIN)
    assert list(stored['close']) == [100.0, 101.5]


def test_upsert_keeps_existing_closes_by_default(tmp_path):
    ext = make_extractor(tmp_path)
    ext._save_prices(ISIN, close_df([100.0]))
    ext._save_prices(ISIN, close_df([999.0]))  # same date, new price
    assert ext._stored_series(ISIN)['close'].iloc[0] == 100.0


def test_force_refresh_overwrites(tmp_path):
    ext = make_extractor(tmp_path, force_refresh=True)
    ext._save_prices(ISIN, close_df([100.0]))
    ext._save_prices(ISIN, close_df([999.0]))
    assert ext._stored_series(ISIN)['close'].iloc[0] == 999.0


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def test_is_cached_empty_db(tmp_path):
    ext = make_extractor(tmp_path)
    cached, df = ext._is_cached(ISIN)
    assert cached is False and df is None


def test_is_cached_when_current(tmp_path):
    ext = make_extractor(tmp_path)
    today = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')
    with sqlite3.connect(ext.db_path) as conn:
        conn.execute('INSERT INTO prices VALUES (?, ?, ?)', (ISIN, today, 100.0))
    cached, df = ext._is_cached(ISIN)
    assert cached is True and len(df) == 1


def test_not_cached_when_stale(tmp_path):
    ext = make_extractor(tmp_path)
    ext._save_prices(ISIN, close_df([100.0], end='2020-01-01'))
    cached, df = ext._is_cached(ISIN)
    assert cached is False and len(df) == 1  # existing data still returned


def test_force_refresh_skips_cache(tmp_path):
    ext = make_extractor(tmp_path, force_refresh=True)
    ext._save_prices(ISIN, close_df([100.0]))
    cached, df = ext._is_cached(ISIN)
    assert cached is False and df is None


# ---------------------------------------------------------------------------
# Source fetchers (mocked network)
# ---------------------------------------------------------------------------

def test_fetch_ftgo_success(tmp_path, monkeypatch):
    ext = make_extractor(tmp_path)
    monkeypatch.setattr(ext, '_resolve_ftgo', lambda isin: {'xid': 'x1'})
    monkeypatch.setattr(
        fetch_mod, 'get_historical_prices',
        lambda xid, start, end: pd.DataFrame(
            {'date': ['2026-08-11', '2026-08-12'], 'close': [100.0, 101.0]}),
    )
    df = ext._fetch_ftgo(ISIN)
    assert list(df['Close']) == [100.0, 101.0]
    assert df.index.name == 'Date'


def test_fetch_ftgo_no_data_returns_none(tmp_path, monkeypatch):
    ext = make_extractor(tmp_path)

    def no_data(isin):
        raise ValueError('No data found for ISIN')

    monkeypatch.setattr(ext, '_resolve_ftgo', no_data)
    assert ext._fetch_ftgo(ISIN) is None


def test_fetch_ftgo_request_error_gives_up_after_retries(tmp_path, monkeypatch):
    monkeypatch.setattr('e1f.common.time.sleep', lambda s: None)
    ext = make_extractor(tmp_path)
    calls = {'n': 0}

    def boom(isin):
        calls['n'] += 1
        raise requests.ConnectionError('down')

    monkeypatch.setattr(ext, '_resolve_ftgo', boom)
    assert ext._fetch_ftgo(ISIN) is None
    assert calls['n'] == 4  # 1 initial + 3 retries


def test_fetch_yfinance_suffix_fallback(tmp_path, monkeypatch):
    ext = make_extractor(tmp_path)

    def fake_download(ticker, **kwargs):
        if ticker == 'TST.L':
            return close_df([100.0])
        return pd.DataFrame()  # bare ticker and .DE: empty

    monkeypatch.setattr(fetch_mod.yf, 'download', fake_download)
    result = ext._fetch_yfinance('TST')
    assert result is not None
    df, actual = result
    assert actual == 'TST.L' and list(df['Close']) == [100.0]


def test_fetch_yfinance_all_empty(tmp_path, monkeypatch):
    ext = make_extractor(tmp_path)
    monkeypatch.setattr(fetch_mod.yf, 'download', lambda t, **k: pd.DataFrame())
    assert ext._fetch_yfinance('TST') is None


# ---------------------------------------------------------------------------
# fetch() orchestration
# ---------------------------------------------------------------------------

def test_fetch_uses_cache_without_network(tmp_path, monkeypatch):
    ext = make_extractor(tmp_path)
    today = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')
    with sqlite3.connect(ext.db_path) as conn:
        conn.execute('INSERT INTO prices VALUES (?, ?, ?)', (ISIN, today, 100.0))
    monkeypatch.setattr(ext, '_fetch_ftgo',
                        lambda *a, **k: pytest.fail('should not hit network'))
    combined = ext.fetch()
    assert list(combined.columns) == [ISIN]


def test_fetch_ftgo_then_yfinance_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr('e1f.fetch.time.sleep', lambda s: None)
    ext = make_extractor(tmp_path, fallback=True)
    monkeypatch.setattr(ext, '_fetch_ftgo', lambda *a, **k: None)
    monkeypatch.setattr(ext, '_fetch_yfinance',
                        lambda ticker, start=None: (close_df([100.0, 101.0]), ticker))

    combined = ext.fetch()
    assert list(combined[ISIN].dropna()) == [100.0, 101.0]
    assert len(ext._stored_series(ISIN)) == 2  # persisted


def test_fetch_no_fallback_skips_yfinance(tmp_path, monkeypatch):
    ext = make_extractor(tmp_path)  # fallback=False by default
    monkeypatch.setattr(ext, '_fetch_ftgo', lambda *a, **k: None)
    monkeypatch.setattr(ext, '_fetch_yfinance',
                        lambda *a, **k: pytest.fail('yfinance should not be called'))
    with pytest.raises(RuntimeError, match='No data fetched'):
        ext.fetch()


def test_fetch_raises_when_all_sources_fail(tmp_path, monkeypatch):
    monkeypatch.setattr('e1f.fetch.time.sleep', lambda s: None)
    ext = make_extractor(tmp_path)
    monkeypatch.setattr(ext, '_fetch_ftgo', lambda *a, **k: None)
    monkeypatch.setattr(ext, '_fetch_yfinance', lambda *a, **k: None)
    with pytest.raises(RuntimeError, match='No data fetched'):
        ext.fetch()


def test_fetch_unknown_isin_raises(tmp_path):
    ext = make_extractor(tmp_path)
    with pytest.raises(ValueError, match='not in config'):
        ext.fetch('ZZ9999999999')


def test_universe_skips_entries_without_tickers(tmp_path):
    universe = {ISIN: {'name': 'No Tickers ETF', 'tickers': []}}
    ext = make_extractor(tmp_path, universe=universe)
    assert ext.etf_universe == {}


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

class FakeExtractor:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def fetch(self, isin=None):
        index = pd.bdate_range('2026-08-11', periods=2)
        return pd.DataFrame({ISIN: [100.0, 101.0]}, index=index)


def test_main_success(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(fetch_mod, 'DataExtractor', FakeExtractor)
    rc = fetch_mod.main(['--config', str(tmp_path / 'u.yaml'),
                         '--db', str(tmp_path / 't.db'),
                         '--currency-meta', str(tmp_path / 'm.yaml')])
    assert rc == 0


def test_main_failure_returns_1(tmp_path, monkeypatch, capsys):
    class Boom(FakeExtractor):
        def fetch(self, isin=None):
            raise RuntimeError('No data fetched')

    monkeypatch.setattr(fetch_mod, 'DataExtractor', Boom)
    rc = fetch_mod.main(['--config', str(tmp_path / 'u.yaml')])
    assert rc == 1
    assert 'No data fetched' in capsys.readouterr().out
