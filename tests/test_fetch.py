"""DataExtractor: DB init, cache logic, upserts, fetch orchestration.

Network sources (ftgo / yfinance) are mocked at the module boundary.
"""

import sqlite3
from contextlib import closing

import pandas as pd
import pytest
import requests
import yaml

import e1f.fetch as fetch_mod
from e1f.common import fund_currency_from_name
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
    assert fund_currency_from_name('iShares Core S&P 500 UCITS ETF USD (Acc)') == 'USD'
    assert fund_currency_from_name('Vanguard FTSE All-World EUR Dist') == 'EUR'
    assert fund_currency_from_name('Some Fund') is None


def test_symbol_currency():
    assert DataExtractor._symbol_currency('CSPX:LSE:USD') == 'USD'
    assert DataExtractor._symbol_currency('WEBN:MUN') == ''
    assert DataExtractor._symbol_currency('CSPX') == ''


def test_resolve_ftgo_skips_symbols_without_currency(tmp_path, monkeypatch):
    ext = make_extractor(tmp_path)
    matches = pd.DataFrame([
        {'xid': 'bad', 'symbol': 'WEBN:MUN', 'name': 'Test ETF USD'},
        {'xid': 'good', 'symbol': 'WEBN:LSE:USD', 'name': 'Test ETF USD'},
    ])
    monkeypatch.setattr(fetch_mod, 'get_xid', lambda isin, display_mode: matches)

    assert ext._resolve_ftgo(ISIN) == {
        'xid': 'good',
        'symbol': 'WEBN:LSE:USD',
        'currency': 'USD',
    }


def test_resolve_ftgo_rejects_symbols_without_currency(tmp_path, monkeypatch):
    ext = make_extractor(tmp_path)
    matches = pd.DataFrame([
        {'xid': 'bad', 'symbol': 'WEBN:MUN', 'name': 'Test ETF USD'},
    ])
    monkeypatch.setattr(fetch_mod, 'get_xid', lambda isin, display_mode: matches)

    with pytest.raises(ValueError, match='No currency-qualified ftgo match'):
        ext._resolve_ftgo(ISIN)


# ---------------------------------------------------------------------------
# DB init / persistence
# ---------------------------------------------------------------------------

def test_init_creates_prices_table(tmp_path):
    ext = make_extractor(tmp_path)
    with closing(sqlite3.connect(ext.db_path)) as conn:
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


def test_read_series_parses_mixed_date_formats(tmp_path):
    # A date-only row next to a 'YYYY-MM-DD HH:MM:SS' row must both parse (not
    # collapse to NaT), so cache-freshness math and strftime stay correct.
    ext = make_extractor(tmp_path)
    with closing(sqlite3.connect(ext.db_path)) as conn:
        conn.executemany('INSERT INTO prices VALUES (?, ?, ?)', [
            (ISIN, '2026-08-10 00:00:00', 100.0),
            (ISIN, '2026-08-11', 101.0),  # date-only
        ])
        conn.commit()

    stored = ext._stored_series(ISIN)

    assert list(stored['close']) == [100.0, 101.0]
    assert not stored.index.isna().any()


def test_read_series_drops_unparseable_dates(tmp_path):
    ext = make_extractor(tmp_path)
    with closing(sqlite3.connect(ext.db_path)) as conn:
        conn.executemany('INSERT INTO prices VALUES (?, ?, ?)', [
            (ISIN, '2026-08-10', 100.0),
            (ISIN, 'not-a-date', 999.0),
        ])
        conn.commit()

    stored = ext._stored_series(ISIN)

    assert list(stored['close']) == [100.0]  # corrupt-date row dropped


def test_replace_prices_removes_rows_absent_from_fetched_series(tmp_path):
    ext = make_extractor(tmp_path, replace=True)
    ext._save_prices(ISIN, close_df([100.0, 101.0]))

    # Same row count: not a shrink, so no override needed.
    ext._replace_prices(ISIN, close_df([998.0, 999.0]))

    stored = ext._stored_series(ISIN)
    assert list(stored['close']) == [998.0, 999.0]


def test_replace_refuses_to_shrink_without_allow_shrink(tmp_path):
    ext = make_extractor(tmp_path, replace=True)
    ext._save_prices(ISIN, close_df([100.0, 101.0]))

    with pytest.raises(RuntimeError, match='does not cover'):
        ext._replace_prices(ISIN, close_df([999.0]))

    # Guard runs before the DELETE, so the stored series is untouched.
    assert list(ext._stored_series(ISIN)['close']) == [100.0, 101.0]


def test_replace_allow_shrink_removes_rows_absent_from_fetched_series(tmp_path):
    ext = make_extractor(tmp_path, replace=True, allow_shrink=True)
    ext._save_prices(ISIN, close_df([100.0, 101.0]))

    ext._replace_prices(ISIN, close_df([999.0]))

    assert list(ext._stored_series(ISIN)['close']) == [999.0]


def test_replace_refuses_to_narrow_date_coverage(tmp_path):
    # Same row count, but the fetched window ends earlier than what is stored —
    # a truncated response that the count-only check would have let through.
    ext = make_extractor(tmp_path, replace=True)
    ext._save_prices(ISIN, close_df([100.0, 101.0], end='2026-08-12'))

    with pytest.raises(RuntimeError, match='does not cover'):
        ext._replace_prices(ISIN, close_df([998.0, 999.0], end='2026-08-06'))

    assert list(ext._stored_series(ISIN)['close']) == [100.0, 101.0]


def test_replace_refuses_interior_hole(tmp_path):
    # Same row count and same endpoints, but an interior stored date is missing
    # from the fetched series — only a per-date check catches this.
    ext = make_extractor(tmp_path, replace=True)
    ext._save_prices(ISIN, close_df([100.0, 101.0, 102.0], end='2026-08-12'))  # 10,11,12

    holed = pd.DataFrame(
        {'Close': [900.0, 901.0, 902.0]},
        index=pd.to_datetime(['2026-08-07', '2026-08-10', '2026-08-12']),  # 11 missing
    )
    with pytest.raises(RuntimeError, match=r'does not cover 1 valid stored date'):
        ext._replace_prices(ISIN, holed)

    assert list(ext._stored_series(ISIN)['close']) == [100.0, 101.0, 102.0]


def test_replace_ignores_corrupt_stored_dates_in_guard(tmp_path):
    # A NULL/unparseable stored date is corruption, not real coverage, so it must
    # not block the repair (nor leak a literal 'None' into the guard message).
    ext = make_extractor(tmp_path, replace=True)
    with closing(sqlite3.connect(ext.db_path)) as conn:
        conn.executemany('INSERT INTO prices VALUES (?, ?, ?)', [
            (ISIN, '2026-08-12', 100.0),
            (ISIN, None, 999.0),          # NULL date
            (ISIN, 'not-a-date', 998.0),  # unparseable date
        ])
        conn.commit()

    # Fetched series covers the one valid stored date; corrupt rows get cleaned.
    ext._replace_prices(ISIN, close_df([200.0], end='2026-08-12'))

    assert list(ext._stored_series(ISIN)['close']) == [200.0]


def test_fetch_replace_repairs_corrupt_date_row(tmp_path, monkeypatch):
    # End-to-end: --replace (no --allow-shrink) repairs a corrupt-date row, as
    # _read_series' docstring advertises.
    ext = make_extractor(tmp_path, replace=True)
    with closing(sqlite3.connect(ext.db_path)) as conn:
        conn.executemany('INSERT INTO prices VALUES (?, ?, ?)', [
            (ISIN, '2026-08-12', 100.0),
            (ISIN, 'not-a-date', 999.0),
        ])
        conn.commit()
    monkeypatch.setattr(ext, '_fetch_ftgo',
                        lambda *a, **k: close_df([200.0], end='2026-08-12'))

    ext.fetch(ISIN)

    assert list(ext._stored_series(ISIN)['close']) == [200.0]


def test_replace_collapses_duplicate_fetched_dates(tmp_path):
    # Two intraday timestamps strftime to the same day; the last one wins via
    # ON CONFLICT instead of tripping the UNIQUE(isin, date) constraint.
    ext = make_extractor(tmp_path, replace=True)
    intraday = pd.DataFrame(
        {'Close': [10.0, 20.0]},
        index=pd.to_datetime(['2026-08-12 09:00', '2026-08-12 16:00']),
    )

    ext._replace_prices(ISIN, intraday)

    assert list(ext._stored_series(ISIN)['close']) == [20.0]


def test_replace_rolls_back_on_insert_failure(tmp_path, monkeypatch):
    # ADR-0008 atomicity: if the INSERT fails, the DELETE must roll back too, so
    # the stored series survives a mid-transaction error. Force the INSERT to
    # fail (malformed rows) after the DELETE; allow_shrink skips the guard.
    ext = make_extractor(tmp_path, replace=True, allow_shrink=True)
    ext._save_prices(ISIN, close_df([100.0, 101.0]))
    monkeypatch.setattr(ext, '_price_rows',
                        lambda isin, df: [(isin, '2026-08-12', 1.0, 'too many cols')])

    with pytest.raises(sqlite3.ProgrammingError):
        ext._replace_prices(ISIN, close_df([200.0]))

    assert list(ext._stored_series(ISIN)['close']) == [100.0, 101.0]


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
    with closing(sqlite3.connect(ext.db_path)) as conn:
        conn.execute('INSERT INTO prices VALUES (?, ?, ?)', (ISIN, today, 100.0))
        conn.commit()
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


def test_replace_skips_cache(tmp_path):
    ext = make_extractor(tmp_path, replace=True)
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
    monkeypatch.setattr('e1f.common.retry.time.sleep', lambda s: None)
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
# Summary / day-count reporting
# ---------------------------------------------------------------------------

def _series(mapping, col='close'):
    idx = pd.to_datetime(list(mapping))
    return pd.DataFrame({col: list(mapping.values())}, index=idx)


def test_delta_none_before_counts_all_new():
    after = _series({'2026-08-10': 1.0, '2026-08-11': 2.0, '2026-08-12': 3.0})
    assert DataExtractor._delta(None, after, 'close') == (3, 0)


def test_delta_incremental_only_new_days():
    before = _series({'2026-08-10': 1.0, '2026-08-11': 2.0})
    after = _series({'2026-08-10': 1.0, '2026-08-11': 2.0, '2026-08-12': 3.0})
    assert DataExtractor._delta(before, after, 'close') == (1, 0)


def test_delta_counts_replaced_when_value_changed():
    before = _series({'2026-08-10': 1.0, '2026-08-11': 2.0, '2026-08-12': 3.0})
    after = _series({'2026-08-10': 1.0, '2026-08-11': 9.0, '2026-08-12': 3.0})
    assert DataExtractor._delta(before, after, 'close') == (0, 1)


def test_changes_phrase_new_and_replaced():
    assert DataExtractor._changes(None, 0, 12) == '12 days'
    assert DataExtractor._changes(3, 0, 12) == '+3 new, 12 total'
    assert DataExtractor._changes(3, 2, 12) == '+3 new, 2 replaced, 12 total'


def test_summary_includes_day_changes():
    df = _series({'2026-08-11': 1.0, '2026-08-12': 2.0})
    line = DataExtractor._summary(ISIN, 'Test ETF', 'ftgo', df, new=2, replaced=0)
    assert '— ftgo - +2 new, 2 total -' in line


# ---------------------------------------------------------------------------
# fetch() orchestration
# ---------------------------------------------------------------------------

def test_fetch_uses_cache_without_network(tmp_path, monkeypatch):
    ext = make_extractor(tmp_path)
    today = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')
    with closing(sqlite3.connect(ext.db_path)) as conn:
        conn.execute('INSERT INTO prices VALUES (?, ?, ?)', (ISIN, today, 100.0))
        conn.commit()
    monkeypatch.setattr(ext, '_fetch_ftgo',
                        lambda *a, **k: pytest.fail('should not hit network'))
    combined = ext.fetch()
    assert list(combined.columns) == [ISIN]


def test_incremental_fetch_overlaps_last_stored_date(tmp_path, monkeypatch):
    # ftgo returns empty when start == end (same-day range). The incremental
    # fetch must pass start = last_stored_date (not +1 day) so the range is
    # always start < end, while the DO NOTHING upsert keeps it idempotent.
    yesterday = pd.Timestamp.now().normalize() - pd.offsets.BDay(1)
    ext = make_extractor(tmp_path)
    ext._save_prices(ISIN, close_df([100.0], end=yesterday.strftime('%Y-%m-%d')))

    captured = {}

    def capture_start(isin, start=None):
        captured['start'] = start
        return close_df([100.0, 101.0], end=pd.Timestamp.now().strftime('%Y-%m-%d'))

    monkeypatch.setattr(ext, '_fetch_ftgo', capture_start)
    ext.fetch()

    assert captured['start'] == yesterday


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


def test_replace_preserves_existing_rows_when_fetch_fails(tmp_path, monkeypatch):
    ext = make_extractor(tmp_path, replace=True)
    ext._save_prices(ISIN, close_df([100.0]))
    monkeypatch.setattr(ext, '_fetch_ftgo', lambda *a, **k: None)

    with pytest.raises(RuntimeError, match='No data fetched'):
        ext.fetch(ISIN)

    assert list(ext._stored_series(ISIN)['close']) == [100.0]


def test_fetch_replace_removes_stale_rows(tmp_path, monkeypatch):
    ext = make_extractor(tmp_path, replace=True, allow_shrink=True)
    ext._save_prices(ISIN, close_df([100.0, 101.0]))
    monkeypatch.setattr(ext, '_fetch_ftgo', lambda *a, **k: close_df([999.0]))

    ext.fetch(ISIN)

    assert list(ext._stored_series(ISIN)['close']) == [999.0]


def test_fetch_replace_truncated_response_preserves_stored_rows(tmp_path, monkeypatch):
    ext = make_extractor(tmp_path, replace=True)
    ext._save_prices(ISIN, close_df([100.0, 101.0, 102.0]))
    monkeypatch.setattr(ext, '_fetch_ftgo', lambda *a, **k: close_df([999.0]))

    with pytest.raises(RuntimeError, match='does not cover'):
        ext.fetch(ISIN)

    assert list(ext._stored_series(ISIN)['close']) == [100.0, 101.0, 102.0]


def test_fetch_replace_without_isin_replaces_all(tmp_path, monkeypatch):
    monkeypatch.setattr('e1f.fetch.time.sleep', lambda s: None)
    ext = make_extractor(tmp_path, replace=True)
    ext._save_prices(ISIN, close_df([100.0, 101.0]))
    monkeypatch.setattr(
        ext, '_fetch_ftgo',
        lambda isin, ticker, start=None: close_df([200.0, 201.0, 202.0]),
    )

    ext.fetch()

    assert list(ext._stored_series(ISIN)['close']) == [200.0, 201.0, 202.0]


def test_fetch_replace_via_yfinance_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr('e1f.fetch.time.sleep', lambda s: None)
    ext = make_extractor(tmp_path, replace=True, fallback=True)
    ext._save_prices(ISIN, close_df([100.0, 101.0]))
    monkeypatch.setattr(ext, '_fetch_ftgo', lambda *a, **k: None)
    monkeypatch.setattr(
        ext, '_fetch_yfinance',
        lambda ticker, start=None: (close_df([200.0, 201.0, 202.0]), ticker),
    )

    ext.fetch(ISIN)

    assert list(ext._stored_series(ISIN)['close']) == [200.0, 201.0, 202.0]


def test_fetch_unknown_isin_raises(tmp_path):
    ext = make_extractor(tmp_path)
    with pytest.raises(ValueError, match='not in config'):
        ext.fetch('ZZ9999999999')


def test_universe_skips_entries_without_tickers(tmp_path):
    universe = {ISIN: {'name': 'No Tickers ETF', 'tickers': []}}
    ext = make_extractor(tmp_path, universe=universe)
    assert ext.etf_universe == {}


# ---------------------------------------------------------------------------
# FX rates (ADR-0010)
# ---------------------------------------------------------------------------

def seed_held(ext, isin, shares=1.0, broker='xtb'):
    """Give an ISIN a net-positive position so portfolio_isins reports it held."""
    with closing(sqlite3.connect(ext.db_path)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS transactions ("
            "broker TEXT, transaction_id TEXT, datetime TEXT, symbol TEXT, "
            "side TEXT, shares REAL, PRIMARY KEY (broker, transaction_id))"
        )
        conn.execute(
            "INSERT INTO transactions "
            "(broker, transaction_id, datetime, symbol, side, shares) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (broker, f"{isin}-1", '2026-01-01', isin, 'BUY', shares),
        )
        conn.commit()


def test_init_creates_fx_rates_table(tmp_path):
    ext = make_extractor(tmp_path)
    with closing(sqlite3.connect(ext.db_path)) as conn:
        cols = conn.execute('PRAGMA table_info(fx_rates)').fetchall()
    assert [c[1] for c in cols] == ['base', 'quote', 'date', 'rate']


def test_resolve_fx_pins_currencies_row(tmp_path, monkeypatch):
    ext = make_extractor(tmp_path)
    matches = pd.DataFrame([
        {'xid': '617254', 'symbol': 'EURUSD', 'asset_class': 'Currencies'},
        {'xid': '9', 'symbol': 'LU0937166394:USD', 'asset_class': 'Funds'},
    ])
    monkeypatch.setattr(fetch_mod, 'get_xid', lambda q, display_mode: matches)

    assert ext._resolve_fx('EUR', 'USD') == {'xid': '617254', 'symbol': 'EURUSD'}
    assert ext._ftgo_meta.fx_pairs['EURUSD']['xid'] == '617254'

    # Second call is served from the pin without touching ftgo.
    monkeypatch.setattr(fetch_mod, 'get_xid',
                        lambda *a, **k: pytest.fail('resolution should be pinned'))
    assert ext._resolve_fx('EUR', 'USD')['xid'] == '617254'


def test_resolve_fx_rejects_non_currency_matches(tmp_path, monkeypatch):
    ext = make_extractor(tmp_path)
    matches = pd.DataFrame([
        {'xid': '9', 'symbol': 'LEUR:LSE:USD', 'asset_class': 'ETFs'},
    ])
    monkeypatch.setattr(fetch_mod, 'get_xid', lambda q, display_mode: matches)
    with pytest.raises(ValueError, match='No ftgo FX spot rate'):
        ext._resolve_fx('EUR', 'USD')


def test_fetch_fx_ftgo_success(tmp_path, monkeypatch):
    ext = make_extractor(tmp_path)
    monkeypatch.setattr(ext, '_resolve_fx', lambda base, quote: {'xid': 'x1'})
    monkeypatch.setattr(
        fetch_mod, 'get_historical_prices',
        lambda xid, start, end: pd.DataFrame(
            {'date': ['2026-08-13', '2026-08-14'], 'close': [1.15, 1.16]}),
    )
    df = ext._fetch_fx_ftgo('EUR', 'USD')
    assert list(df['Close']) == [1.15, 1.16]
    assert df.index.name == 'Date'


def test_fetch_fx_yfinance_success(tmp_path, monkeypatch):
    ext = make_extractor(tmp_path)
    monkeypatch.setattr(fetch_mod.yf, 'download', lambda t, **k: close_df([1.16, 1.17]))
    df = ext._fetch_fx_yfinance('EUR', 'USD')
    assert list(df['Close']) == [1.16, 1.17]


def test_fetch_fx_yfinance_empty_returns_none(tmp_path, monkeypatch):
    ext = make_extractor(tmp_path)
    monkeypatch.setattr(fetch_mod.yf, 'download', lambda t, **k: pd.DataFrame())
    assert ext._fetch_fx_yfinance('EUR', 'USD') is None


def test_refresh_fx_pair_all_sources_fail_leaves_empty(tmp_path, monkeypatch):
    ext = make_extractor(tmp_path, fallback=True)
    monkeypatch.setattr(ext, '_fetch_fx_ftgo', lambda *a, **k: None)
    monkeypatch.setattr(ext, '_fetch_fx_yfinance', lambda *a, **k: None)
    ext._refresh_fx_pair('EUR', 'USD')  # warns; no rows written, no raise
    assert ext._fx_stored('EUR', 'USD').empty


def test_save_fx_and_read_back(tmp_path):
    ext = make_extractor(tmp_path)
    ext._save_fx('EUR', 'USD', close_df([1.15, 1.16]))
    assert list(ext._fx_stored('EUR', 'USD')['rate']) == [1.15, 1.16]


def test_save_fx_upsert_keeps_existing_by_default(tmp_path):
    ext = make_extractor(tmp_path)
    ext._save_fx('EUR', 'USD', close_df([1.15]))
    ext._save_fx('EUR', 'USD', close_df([9.99]))  # same date, new rate
    assert ext._fx_stored('EUR', 'USD')['rate'].iloc[0] == 1.15


def test_save_fx_force_overwrites(tmp_path):
    ext = make_extractor(tmp_path, force_refresh=True)
    ext._save_fx('EUR', 'USD', close_df([1.15]))
    ext._save_fx('EUR', 'USD', close_df([9.99]))
    assert ext._fx_stored('EUR', 'USD')['rate'].iloc[0] == 9.99


def test_is_fx_cached_empty(tmp_path):
    ext = make_extractor(tmp_path)
    cached, df = ext._is_fx_cached('EUR', 'USD')
    assert cached is False and df is None


def test_is_fx_cached_when_current(tmp_path):
    ext = make_extractor(tmp_path)
    today = pd.Timestamp.now().strftime('%Y-%m-%d')
    with closing(sqlite3.connect(ext.db_path)) as conn:
        conn.execute('INSERT INTO fx_rates VALUES (?, ?, ?, ?)', ('EUR', 'USD', today, 1.16))
        conn.commit()
    cached, df = ext._is_fx_cached('EUR', 'USD')
    assert cached is True and len(df) == 1


def test_is_fx_cached_when_stale(tmp_path):
    ext = make_extractor(tmp_path)
    ext._save_fx('EUR', 'USD', close_df([1.16], end='2020-01-01'))
    cached, df = ext._is_fx_cached('EUR', 'USD')
    assert cached is False and len(df) == 1  # existing rates still returned


def test_needed_fx_quotes_from_pinned_currency_not_fund_currency(tmp_path):
    ext = make_extractor(tmp_path)
    # Pinned price currency is what matters; a divergent fund_currency is ignored.
    ext._ftgo_meta.funds['HELDUSD00001'] = {'currency': 'USD'}
    ext._ftgo_meta.funds['HELDEUR00001'] = {'currency': 'EUR'}
    seed_held(ext, 'HELDUSD00001')
    seed_held(ext, 'HELDEUR00001')
    assert ext._needed_fx_quotes() == {'USD'}  # base EUR excluded


def test_refresh_fx_fails_loud_on_pence(tmp_path):
    ext = make_extractor(tmp_path)
    ext._ftgo_meta.funds['HELDGBX00001'] = {'currency': 'GBX'}
    seed_held(ext, 'HELDGBX00001')
    with pytest.raises(ValueError, match=r'GBX .*not supported'):
        ext._refresh_fx()


def test_refresh_fx_pair_falls_back_to_yfinance(tmp_path, monkeypatch):
    ext = make_extractor(tmp_path, fallback=True)
    monkeypatch.setattr(ext, '_fetch_fx_ftgo', lambda *a, **k: None)
    monkeypatch.setattr(ext, '_fetch_fx_yfinance', lambda *a, **k: close_df([1.16, 1.17]))
    ext._refresh_fx_pair('EUR', 'USD')
    assert list(ext._fx_stored('EUR', 'USD')['rate']) == [1.16, 1.17]


def test_refresh_fx_pair_no_fallback_skips_yfinance(tmp_path, monkeypatch):
    ext = make_extractor(tmp_path)  # fallback=False
    monkeypatch.setattr(ext, '_fetch_fx_ftgo', lambda *a, **k: None)
    monkeypatch.setattr(ext, '_fetch_fx_yfinance',
                        lambda *a, **k: pytest.fail('yfinance is gated behind --fallback'))
    ext._refresh_fx_pair('EUR', 'USD')  # no data; warns, does not raise
    assert ext._fx_stored('EUR', 'USD').empty


def test_refresh_fx_pair_uses_cache_without_network(tmp_path, monkeypatch):
    ext = make_extractor(tmp_path)
    today = pd.Timestamp.now().strftime('%Y-%m-%d')
    with closing(sqlite3.connect(ext.db_path)) as conn:
        conn.execute('INSERT INTO fx_rates VALUES (?, ?, ?, ?)', ('EUR', 'USD', today, 1.16))
        conn.commit()
    monkeypatch.setattr(ext, '_fetch_fx_ftgo',
                        lambda *a, **k: pytest.fail('should not hit network'))
    ext._refresh_fx_pair('EUR', 'USD')


def test_fetch_auto_refreshes_fx_for_held_currency(tmp_path, monkeypatch):
    ext = make_extractor(tmp_path)
    ext._ftgo_meta.funds[ISIN] = {'currency': 'USD'}
    seed_held(ext, ISIN)
    monkeypatch.setattr(ext, '_fetch_ftgo', lambda *a, **k: close_df([100.0, 101.0]))
    monkeypatch.setattr(ext, '_fetch_fx_ftgo', lambda *a, **k: close_df([1.16, 1.17]))

    ext.fetch()

    assert list(ext._fx_stored('EUR', 'USD')['rate']) == [1.16, 1.17]


def test_fetch_single_isin_skips_fx(tmp_path, monkeypatch):
    ext = make_extractor(tmp_path)
    ext._ftgo_meta.funds[ISIN] = {'currency': 'USD'}
    seed_held(ext, ISIN)
    monkeypatch.setattr(ext, '_fetch_ftgo', lambda *a, **k: close_df([100.0]))
    monkeypatch.setattr(ext, '_refresh_fx',
                        lambda: pytest.fail('single-ISIN fetch must skip FX'))

    ext.fetch(ISIN)

    assert ext._fx_stored('EUR', 'USD').empty


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


def test_main_replace_without_isin_accepted(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr('e1f.fetch.time.sleep', lambda s: None)
    monkeypatch.setattr('e1f.fetch.DEFAULT_DB', str(tmp_path / 'e1f.db'))
    rc = fetch_mod.main(['--replace'])
    assert rc == 0


def test_replace_portfolio_replaces_only_held_isin(tmp_path, monkeypatch):
    monkeypatch.setattr('e1f.fetch.time.sleep', lambda s: None)
    other_isin = 'IE00B4K48X80'
    ext = make_extractor(tmp_path, replace=True)
    ext._save_prices(ISIN, close_df([100.0, 101.0]))
    ext._save_prices(other_isin, close_df([50.0, 51.0]))
    seed_held(ext, ISIN)
    fetched = []
    monkeypatch.setattr(
        ext, '_fetch_ftgo',
        lambda isin, ticker, start=None: fetched.append(isin) or close_df([200.0, 201.0, 202.0]),
    )
    held = fetch_mod.portfolio_isins(ext.db_path)
    ext.etf_universe = {k: v for k, v in ext.etf_universe.items() if k in held}

    ext.fetch()

    assert fetched == [ISIN]
    assert list(ext._stored_series(ISIN)['close']) == [200.0, 201.0, 202.0]
    assert list(ext._stored_series(other_isin)['close']) == [50.0, 51.0]


def test_main_allow_shrink_requires_replace(capsys):
    rc = fetch_mod.main(['--allow-shrink', ISIN])
    assert rc == 1
    assert '--allow-shrink only applies with --replace' in capsys.readouterr().out


def test_main_force_and_replace_are_mutually_exclusive(capsys):
    with pytest.raises(SystemExit):
        fetch_mod.main([ISIN, '--force', '--replace'])
    assert 'not allowed with argument' in capsys.readouterr().err
