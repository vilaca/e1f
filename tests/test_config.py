"""e1f config subcommands: list/add/update/remove/trim/validate."""

import sqlite3
from contextlib import closing

import numpy as np
import pandas as pd
import pytest
import yaml

import e1f.config as config_cmd
from e1f.common import OpenFIGIResolver

ISIN_A = 'AA0000000001'
ISIN_B = 'BB0000000002'
ISIN_C = 'CC0000000003'

RESOLVED = {'name': 'Test ETF', 'tickers': ['TST'], 'exchange': 'NA', 'figi': 'F'}


def etf(isin):
    return {'name': f'ETF {isin}', 'tickers': ['T'], 'exchange': 'NA', 'figi': 'F'}


@pytest.fixture
def paths(tmp_path):
    return {
        'config': str(tmp_path / 'universe.yaml'),
        'db': str(tmp_path / 'prices.db'),
        'meta': str(tmp_path / 'currency.yaml'),
    }


def write_config(path, isins):
    with open(path, 'w') as f:
        yaml.dump({'etfs': {i: etf(i) for i in isins}}, f)


def write_db(path, isin_closes):
    """isin_closes: {isin: list-of-close-prices} on recent consecutive business days."""
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('CREATE TABLE prices (isin TEXT, date TEXT, close REAL, '
                     'PRIMARY KEY (isin, date))')
        for isin, closes in isin_closes.items():
            dates = pd.bdate_range(end='2026-08-12', periods=len(closes))
            conn.executemany(
                'INSERT INTO prices VALUES (?, ?, ?)',
                [(isin, d.strftime('%Y-%m-%d %H:%M:%S'), float(c))
                 for d, c in zip(dates, closes, strict=False)],
            )
        conn.commit()


def write_meta(path, isins):
    with open(path, 'w') as f:
        yaml.dump({i: {'xid': 'x', 'symbol': 'T:LSE:USD', 'currency': 'USD'}
                   for i in isins}, f)


def read_config_isins(path):
    return set(yaml.safe_load(open(path))['etfs'])


def read_db_isins(path):
    with closing(sqlite3.connect(path)) as conn:
        return {r[0] for r in conn.execute('SELECT DISTINCT isin FROM prices')}


def mock_resolver(monkeypatch, result=RESOLVED):
    monkeypatch.setattr(OpenFIGIResolver, 'resolve', lambda self, isin: result)
    monkeypatch.setattr('e1f.common.enrich_fund_metadata', lambda isin, info: info)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

def test_list_empty(paths, capsys):
    write_config(paths['config'], [])
    assert config_cmd.main(['--config', paths['config'], 'list']) == 0
    assert 'No ETFs in configuration' in capsys.readouterr().out


def test_list_shows_all_etfs(paths, capsys):
    write_config(paths['config'], [ISIN_A, ISIN_B])
    assert config_cmd.main(['--config', paths['config'], 'list']) == 0
    out = capsys.readouterr().out
    assert ISIN_A in out and ISIN_B in out and 'Total: 2 ETFs' in out


# ---------------------------------------------------------------------------
# add / update
# ---------------------------------------------------------------------------

def test_add_and_update(paths, monkeypatch, capsys):
    write_config(paths['config'], [])
    mock_resolver(monkeypatch)
    assert config_cmd.main(['--config', paths['config'], 'add', ISIN_A, ISIN_B]) == 0
    assert read_config_isins(paths['config']) == {ISIN_A, ISIN_B}

    assert config_cmd.main(['--config', paths['config'], 'update', ISIN_A]) == 0
    assert config_cmd.main(['--config', paths['config'], 'update', ISIN_C]) == 1


def test_update_without_isins_updates_all(paths, monkeypatch, capsys):
    write_config(paths['config'], [ISIN_A, ISIN_B])
    mock_resolver(monkeypatch)

    assert config_cmd.main(['--config', paths['config'], 'update']) == 0
    out = capsys.readouterr().out
    assert "✓ Updated 2/2 ETFs" in out


def test_update_without_isins_on_empty_config(paths, capsys):
    write_config(paths['config'], [])
    assert config_cmd.main(['--config', paths['config'], 'update']) == 0
    assert 'No ETFs in configuration' in capsys.readouterr().out


def test_add_partial_failure_returns_1(paths, monkeypatch):
    write_config(paths['config'], [])
    # Resolve succeeds only for ISIN_A
    monkeypatch.setattr(OpenFIGIResolver, 'resolve',
                        lambda self, isin: RESOLVED if isin == ISIN_A else None)
    assert config_cmd.main(['--config', paths['config'], 'add', ISIN_A, ISIN_B]) == 1
    assert read_config_isins(paths['config']) == {ISIN_A}


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------

def test_remove_deletes_everywhere(paths, capsys):
    write_config(paths['config'], [ISIN_A, ISIN_B])
    write_db(paths['db'], {ISIN_A: [100, 101], ISIN_B: [100, 101]})
    write_meta(paths['meta'], [ISIN_A, ISIN_B])

    rc = config_cmd.main(['--config', paths['config'], 'remove', ISIN_A, ISIN_C,
                          '--db', paths['db'], '--currency-meta', paths['meta']])
    assert rc == 0
    out = capsys.readouterr().out
    assert f'{ISIN_C}: not found in any file' in out
    assert read_config_isins(paths['config']) == {ISIN_B}
    assert read_db_isins(paths['db']) == {ISIN_B}
    with open(paths['meta']) as f:
        assert set(yaml.safe_load(f)) == {ISIN_B}


def test_remove_without_db(paths):
    write_config(paths['config'], [ISIN_A])
    write_meta(paths['meta'], [])
    rc = config_cmd.main(['--config', paths['config'], 'remove', ISIN_A,
                          '--db', paths['db'], '--currency-meta', paths['meta']])
    assert rc == 0
    assert read_config_isins(paths['config']) == set()
    assert not pd.io.common.os.path.exists(paths['db'])  # no empty DB created


# ---------------------------------------------------------------------------
# trim
# ---------------------------------------------------------------------------

def test_trim_keeps_intersection(paths, capsys):
    write_config(paths['config'], [ISIN_A, ISIN_B])
    write_db(paths['db'], {ISIN_B: [100], ISIN_C: [100]})
    write_meta(paths['meta'], [ISIN_B, ISIN_C])

    rc = config_cmd.main(['--config', paths['config'], 'trim',
                          '--db', paths['db'], '--currency-meta', paths['meta']])
    assert rc == 0
    assert read_config_isins(paths['config']) == {ISIN_B}
    assert read_db_isins(paths['db']) == {ISIN_B}
    with open(paths['meta']) as f:
        assert set(yaml.safe_load(f)) == {ISIN_B}


def test_trim_in_sync_is_noop(paths, capsys):
    write_config(paths['config'], [ISIN_A])
    write_db(paths['db'], {ISIN_A: [100]})
    write_meta(paths['meta'], [ISIN_A])
    rc = config_cmd.main(['--config', paths['config'], 'trim',
                          '--db', paths['db'], '--currency-meta', paths['meta']])
    assert rc == 0
    assert 'Nothing to trim' in capsys.readouterr().out


def test_trim_refuses_without_db(paths, capsys):
    write_config(paths['config'], [ISIN_A])
    write_meta(paths['meta'], [ISIN_A])
    rc = config_cmd.main(['--config', paths['config'], 'trim',
                          '--db', paths['db'], '--currency-meta', paths['meta']])
    assert rc == 1
    assert 'refusing to trim' in capsys.readouterr().out
    assert read_config_isins(paths['config']) == {ISIN_A}  # untouched


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------

def good_prices(isin, n=1100, seed=0):
    rng = np.random.default_rng(seed)
    return list(100 * np.exp(np.cumsum(rng.normal(0, 0.01, n))))


def test_validate_healthy(paths, capsys):
    write_config(paths['config'], [ISIN_A, ISIN_B])
    write_db(paths['db'], {ISIN_A: good_prices(ISIN_A, seed=1),
                           ISIN_B: good_prices(ISIN_B, seed=2)})
    rc = config_cmd.main(['--config', paths['config'], 'validate',
                          '--db', paths['db']])
    assert rc == 0
    out = capsys.readouterr().out
    assert '=== Data Integrity ===' in out
    assert 'Duplicate keys:       0' in out
    assert 'Null closes:          0' in out
    assert 'Non-positive closes:  0' in out
    assert 'Weekend rows:         0' in out
    assert 'config and DB in sync' in out
    assert 'None — all ETFs look good' in out


def test_quality_report_counts_missing_business_days():
    # ISIN_A: Tue 23rd -> Mon 29th skips Wed/Thu/Fri (24-26) = 3 missing.
    # ISIN_B: consecutive business days = 0 missing.
    prices = pd.DataFrame({
        'isin': [ISIN_A, ISIN_A, ISIN_B, ISIN_B],
        'date': ['2025-12-23', '2025-12-29', '2026-08-11', '2026-08-12'],
        'close': [100.0, 101.0, 50.0, 51.0],
    })

    report = config_cmd.quality_report(prices)

    assert report['max_missing_business_days'] == 3
    assert report['missing_business_days_by_isin'] == {ISIN_A: 3, ISIN_B: 0}


def test_quality_report_missing_business_days_with_weekend_endpoint():
    # A stored weekend row (Sat) followed by Tue leaves only Mon missing; the
    # count must not over-subtract just because an endpoint isn't a business day.
    prices = pd.DataFrame({
        'isin': [ISIN_A, ISIN_A],
        'date': ['2026-08-01', '2026-08-04'],  # Sat -> Tue, Mon missing
        'close': [100.0, 101.0],
    })

    report = config_cmd.quality_report(prices)

    assert report['max_missing_business_days'] == 1
    assert report['missing_business_days_by_isin'] == {ISIN_A: 1}


def test_quality_report_handles_invalid_and_nat_dates():
    # A NULL date and an unparseable string must not crash the business-day math.
    prices = pd.DataFrame({
        'isin': [ISIN_A, ISIN_A, ISIN_A],
        'date': ['2026-08-10', None, 'not-a-date'],
        'close': [100.0, 101.0, 102.0],
    })

    report = config_cmd.quality_report(prices)

    assert report['rows'] == 3
    assert report['invalid_dates'] == 2
    # Two unparseable dates must not masquerade as a (NaT == NaT) key collision.
    assert report['duplicates'] == 0
    assert report['duplicate_isins'] == []
    # Only one valid date survives, so there is no gap to measure.
    assert report['max_missing_business_days'] == 0
    assert report['missing_business_days_by_isin'] == {ISIN_A: 0}


def test_quality_report_all_invalid_dates_no_phantom_duplicates():
    prices = pd.DataFrame({
        'isin': [ISIN_A, ISIN_A, ISIN_A],
        'date': ['junk1', 'junk2', 'junk3'],
        'close': [1.0, 2.0, 3.0],
    })

    report = config_cmd.quality_report(prices)

    assert report['invalid_dates'] == 3
    assert report['duplicates'] == 0


def test_validate_reports_price_integrity_issues(paths, capsys):
    write_config(paths['config'], [ISIN_A])
    write_db(paths['db'], {ISIN_A: [100, -5, 200]})
    with closing(sqlite3.connect(paths['db'])) as conn:
        conn.execute(
            'INSERT INTO prices VALUES (?, ?, ?)',
            (ISIN_A, '2026-08-29 00:00:00', None),
        )
        conn.commit()

    rc = config_cmd.main(['--config', paths['config'], 'validate',
                          '--db', paths['db']])

    assert rc == 1
    out = capsys.readouterr().out
    assert 'Null closes:          1' in out
    assert 'Non-positive closes:  1' in out
    weekend_line = next(line for line in out.splitlines() if 'Weekend rows:' in line)
    assert f'1 [{ISIN_A}]' in weekend_line
    # Gap breakdown line: 08-12 -> 08-29 (a Sat) skips 12 business days.
    assert f'      12 days  {ISIN_A}  ETF {ISIN_A}' in out
    price_change_line = next(
        line for line in out.splitlines() if 'Largest price change:' in line
    )
    assert f'[{ISIN_A}]' in price_change_line
    assert 'Validation failed' in out
    assert 'None — all ETFs look good' not in out


def test_validate_exits_1_on_duplicate_keys_without_crashing(paths, capsys):
    # Logical duplicate (isin, date) keys must not crash the per-ETF pivot;
    # validate reports them, still renders later sections, and exits 1. Distinct
    # PK strings that parse to the same day are the realistic source (a
    # date-only row alongside 'YYYY-MM-DD HH:MM:SS' ones).
    write_config(paths['config'], [ISIN_A])
    write_db(paths['db'], {ISIN_A: good_prices(ISIN_A, seed=1)})
    with closing(sqlite3.connect(paths['db'])) as conn:
        dupe_date = conn.execute(
            'SELECT date FROM prices WHERE isin = ? LIMIT 1', (ISIN_A,)
        ).fetchone()[0]
        conn.execute('INSERT INTO prices VALUES (?, ?, ?)',
                     (ISIN_A, dupe_date[:10], 123.0))
        conn.commit()

    rc = config_cmd.main(['--config', paths['config'], 'validate',
                          '--db', paths['db']])

    assert rc == 1
    out = capsys.readouterr().out
    # format='mixed' resolves the date-only row to the same day as its datetime
    # twin, so it is exactly one duplicate — not a column-wide NaT collision.
    assert f'Duplicate keys:       1 [{ISIN_A}]' in out
    assert 'Invalid dates:        0' in out
    assert '=== History Breakdown ===' in out  # execution reached past the pivot
    assert 'Validation failed' in out


def test_validate_reports_invalid_dates_as_error(paths, capsys):
    write_config(paths['config'], [ISIN_A])
    write_db(paths['db'], {ISIN_A: good_prices(ISIN_A, seed=1)})
    with closing(sqlite3.connect(paths['db'])) as conn:
        conn.execute('INSERT INTO prices VALUES (?, ?, ?)', (ISIN_A, None, 100.0))
        conn.commit()

    rc = config_cmd.main(['--config', paths['config'], 'validate',
                          '--db', paths['db']])

    assert rc == 1
    out = capsys.readouterr().out
    assert f'Invalid dates:        1 [{ISIN_A}]' in out
    assert 'Validation failed' in out


def test_validate_reports_config_isins_missing_from_db(paths, capsys):
    # The 'In config, missing from DB' desync direction (complements the orphan case).
    write_config(paths['config'], [ISIN_A, ISIN_B])
    write_db(paths['db'], {ISIN_A: good_prices(ISIN_A, seed=1)})

    rc = config_cmd.main(['--config', paths['config'], 'validate',
                          '--db', paths['db']])

    assert rc == 1
    out = capsys.readouterr().out
    assert 'In config, missing from DB' in out and ISIN_B in out


def test_validate_empty_history_prints_none_not_bare_header(paths, capsys):
    # A prices table with no usable dated rows must not leave a bare
    # "=== History Breakdown ===" header with no body.
    write_config(paths['config'], [ISIN_A])
    write_db(paths['db'], {ISIN_A: []})  # table exists, zero rows

    rc = config_cmd.main(['--config', paths['config'], 'validate',
                          '--db', paths['db']])

    out = capsys.readouterr().out
    assert '=== History Breakdown ===' in out
    assert 'no dated price history' in out
    assert rc == 1  # config lists ISIN_A but the DB has none — a desync error


def test_validate_flags_orphans_and_short_history(paths, capsys):
    write_config(paths['config'], [ISIN_A])
    write_db(paths['db'], {ISIN_A: good_prices(ISIN_A, n=50),  # < 3yr -> short
                           ISIN_B: good_prices(ISIN_B, seed=3)})  # orphan
    rc = config_cmd.main(['--config', paths['config'], 'validate',
                          '--db', paths['db']])
    assert rc == 1
    out = capsys.readouterr().out
    assert 'orphans' in out and ISIN_B in out
    assert 'Short history' in out and ISIN_A in out


def test_validate_warning_only_returns_success(paths, capsys):
    write_config(paths['config'], [ISIN_A])
    write_db(paths['db'], {ISIN_A: good_prices(ISIN_A, n=50)})

    rc = config_cmd.main(['--config', paths['config'], 'validate',
                          '--db', paths['db']])

    assert rc == 0
    assert 'Validation passed with warnings' in capsys.readouterr().out


def test_validate_integrity_warning_without_flags_is_not_contradictory(paths, capsys):
    # A healthy-but-for-one-jump series: triggers an integrity warning (large
    # price change) yet is not short/sparse/cash, so nothing is "flagged". The
    # Warnings section must not claim a bare "None" while the summary reports
    # "passed with warnings".
    closes = good_prices(ISIN_A, seed=1)
    closes[500] *= 2  # ~100% up then ~50% down — well over the 50% limit
    write_config(paths['config'], [ISIN_A])
    write_db(paths['db'], {ISIN_A: closes})

    rc = config_cmd.main(['--config', paths['config'], 'validate',
                          '--db', paths['db']])

    assert rc == 0
    out = capsys.readouterr().out
    assert 'Validation passed with warnings' in out
    assert 'None here — see Data Integrity warnings above.' in out
    # The old contradictory bare "None." line must be gone.
    assert '\n  None.\n' not in out


def test_validate_without_db(paths, capsys):
    write_config(paths['config'], [ISIN_A])
    rc = config_cmd.main(['--config', paths['config'], 'validate',
                          '--db', paths['db']])
    assert rc == 1
    assert "run 'e1f fetch' first" in capsys.readouterr().out


def test_no_subcommand_prints_help(paths, capsys):
    write_config(paths['config'], [])
    assert config_cmd.main(['--config', paths['config']]) == 1
    assert 'usage' in capsys.readouterr().out.lower()
