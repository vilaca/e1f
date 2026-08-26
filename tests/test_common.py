"""ConfigManager (YAML universe) and OpenFIGIResolver (mocked HTTP)."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
import requests
import yaml

from e1f.common import (
    BASE_CURRENCY,
    DEFAULT_CONFIG,
    DEFAULT_CURRENCY_META,
    DEFAULT_DB,
    DEFAULT_SCENARIOS,
    DEFAULT_START_DATE,
    ConfigManager,
    OpenFIGIResolver,
    convert_to_eur,
    distribution_from_name,
    enrich_fund_metadata,
    fund_currency_from_name,
    fx_rate_asof,
    load_trades,
    pinned_quote_currency,
    position_timeline,
)
from e1f.common import metrics as _metrics
from e1f.common.metrics import _bisect, _newton, xirr
from e1f.common.universe import (
    _asset_class_from_investment_focus,
    _best_ftgo_name,
    _fund_currency_from_names,
    _fetch_justetf_html,
    _ftgo_fund_name,
    _ftgo_listing_names,
    _justetf_field,
    _parse_percent_value,
    _short_lookup_error,
)

ISIN = "AA0000000001"

RESOLVED = {
    "name": "Test ETF",
    "tickers": ["TST"],
    "exchange": "NA",
    "figi": "BBG000TEST",
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
    monkeypatch.setattr(r.session, "post", lambda *a, **k: FakeResponse(payload))
    return r


def test_default_paths_resolve_against_repo_root():
    """Package layout must not shift _ROOT (defaults.py is one level deeper)."""
    root = Path(__file__).resolve().parents[1]
    assert str(root / "data" / "etf_universe.yaml") == DEFAULT_CONFIG
    assert str(root / "data" / "e1f.db") == DEFAULT_DB
    assert str(root / "data" / "currency_metadata.yaml") == DEFAULT_CURRENCY_META
    assert str(root / "data" / "scenarios.yaml") == DEFAULT_SCENARIOS
    assert DEFAULT_START_DATE == "2000-01-01"
    assert BASE_CURRENCY == "EUR"


# ---------------------------------------------------------------------------
# OpenFIGIResolver
# ---------------------------------------------------------------------------


def test_resolve_success(monkeypatch):
    payload = [
        {"data": [{"name": "Test ETF", "ticker": "TST", "exchCode": "NA", "figi": "BBG000TEST"}]}
    ]
    info = resolver_returning(monkeypatch, payload).resolve(ISIN)
    assert info["name"] == "Test ETF"
    assert info["tickers"] == ["TST"]
    assert info["exchange"] == "NA"
    assert info["figi"] == "BBG000TEST"
    assert info["listings"] == [{"ticker": "TST", "exchange": "NA"}]
    assert info["source"] == "OpenFIGI"


def test_resolve_collects_xtb_listings(monkeypatch):
    payload = [
        {
            "data": [
                {"name": "Global Bond", "ticker": "0GGH", "exchCode": "LN", "figi": "BBG1"},
                {"name": "Global Bond", "ticker": "EUNA", "exchCode": "GR", "figi": "BBG2"},
                {"name": "Global Bond", "ticker": "SKIP", "exchCode": "XX", "figi": "BBG3"},
            ]
        }
    ]
    info = resolver_returning(monkeypatch, payload).resolve(ISIN)
    assert info["exchange"] == "LN"
    assert info["figi"] == "BBG1"
    assert info["tickers"] == ["0GGH", "EUNA"]
    assert info["listings"] == [
        {"ticker": "0GGH", "exchange": "LN"},
        {"ticker": "EUNA", "exchange": "GR"},
    ]


def test_resolve_invalid_isin_short_circuits(monkeypatch):
    r = OpenFIGIResolver()
    monkeypatch.setattr(r.session, "post", lambda *a, **k: pytest.fail("should not POST"))
    assert r.resolve("not-an-isin") is None


def test_resolve_no_data(monkeypatch):
    assert resolver_returning(monkeypatch, [{"data": []}]).resolve(ISIN) is None
    assert resolver_returning(monkeypatch, [{}]).resolve(ISIN) is None


def test_resolve_api_error_gives_up_after_retries(monkeypatch):
    monkeypatch.setattr("e1f.common.retry.time.sleep", lambda s: None)
    r = OpenFIGIResolver()
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise requests.ConnectionError("down")

    monkeypatch.setattr(r.session, "post", boom)
    assert r.resolve(ISIN) is None
    assert calls["n"] == 4  # 1 initial + 3 retries


def test_resolve_malformed_payload(monkeypatch, capsys):
    r = resolver_returning(monkeypatch, ["unexpected"])
    assert r.resolve(ISIN) is None
    assert "Error parsing" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# ConfigManager
# ---------------------------------------------------------------------------


def manager(tmp_path, monkeypatch, resolve_result=RESOLVED):
    monkeypatch.setattr(OpenFIGIResolver, "resolve", lambda self, isin: resolve_result)
    monkeypatch.setattr("e1f.common.universe.enrich_fund_metadata", lambda isin, info: info)
    return ConfigManager(str(tmp_path / "universe.yaml"))


def test_missing_config_starts_empty(tmp_path, monkeypatch):
    cm = manager(tmp_path, monkeypatch)
    assert cm.list() == []


def test_add_writes_yaml(tmp_path, monkeypatch):
    cm = manager(tmp_path, monkeypatch)
    assert cm.add(ISIN) is True

    on_disk = yaml.safe_load((tmp_path / "universe.yaml").read_text())
    assert on_disk["etfs"][ISIN]["name"] == "Test ETF"
    assert cm.get(ISIN)["tickers"] == ["TST"]


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
    cm.add("BB0000000002")
    cm.add(ISIN)
    assert [isin for isin, _ in cm.list()] == [ISIN, "BB0000000002"]


# ---------------------------------------------------------------------------
# Fund metadata enrichment
# ---------------------------------------------------------------------------


def test_fund_currency_from_name():
    assert fund_currency_from_name("VANG FTSE AW USDA") == "USD"
    assert fund_currency_from_name("iShares Core S&P 500 UCITS ETF USD (Acc)") == "USD"
    assert fund_currency_from_name("X MSCI WORLD HEALTH CARE") is None


def test_distribution_from_name():
    assert distribution_from_name("VANG FTSE AW USDA") == "Accumulating"
    assert distribution_from_name("VANG FTSE AW USDD") == "Distributing"
    assert distribution_from_name("iShares Core S&P 500 UCITS ETF USD (Acc)") == "Accumulating"
    assert distribution_from_name("AMUNDI PRME ALL CTRY WLD ACC") == "Accumulating"
    assert distribution_from_name("Some ETF EUR Hedged") is None


def test_asset_class_from_investment_focus():
    assert _asset_class_from_investment_focus("Equity, United States") == "Equity"
    assert _asset_class_from_investment_focus("Bonds, World, Aggregate, All maturities") == "Bonds"
    assert _asset_class_from_investment_focus("") is None


def test_best_ftgo_name_prefers_share_class_matching_openfigi_hint():
    names = [
        "Amundi Prime All Country World UCITS ETF USD Dist",
        "Amundi Prime All Country World UCITS ETF USD Acc",
    ]
    chosen = _best_ftgo_name(names, "AMUNDI PRME ALL CTRY WLD ACC")
    assert chosen.endswith("Acc")


def test_fund_currency_from_names_finds_usd_on_sibling_ftgo_listing():
    names = [
        "Amundi Prime All Country World UCITS ETF USD Dist",
        "Amundi Prime All Country World UCITS ETF Acc",
    ]
    assert _fund_currency_from_names(names) == "USD"
    assert fund_currency_from_name(names[1]) is None


def _mock_ftgo_enrichment(monkeypatch, *, names=None, error=None, ter=None, justetf_html=None):
    if error:
        monkeypatch.setattr("e1f.common.universe._ftgo_load", lambda isin: (None, error))
    else:
        monkeypatch.setattr("e1f.common.universe._ftgo_load", lambda isin: ("matches", None))
        monkeypatch.setattr(
            "e1f.common.universe._names_from_ftgo_matches",
            lambda matches: names or [],
        )
        monkeypatch.setattr("e1f.common.universe._ftgo_ter", lambda matches, hint: ter)
    monkeypatch.setattr("e1f.common.universe._fetch_justetf_html", lambda isin: justetf_html)


def test_parse_percent_value():
    assert _parse_percent_value("0.07%") == 0.07
    assert _parse_percent_value("--") is None
    assert _parse_percent_value("") is None


def test_enrich_prefers_openfigi_distribution_over_conflicting_ftgo_name(
    monkeypatch,
):
    ftgo_names = [
        "Amundi Prime All Country World UCITS ETF USD Dist",
        "Amundi Prime All Country World UCITS ETF Acc",
    ]
    _mock_ftgo_enrichment(monkeypatch, names=ftgo_names)

    info = {"name": "AMUNDI PRME ALL CTRY WLD ACC", "tickers": ["WEBN"], "exchange": "GR"}
    enriched = enrich_fund_metadata(ISIN, info)

    assert enriched["distribution"] == "Accumulating"
    assert enriched["fund_currency"] == "USD"


def test_enrich_fund_metadata_merges_openfigi_and_ftgo(monkeypatch):
    _mock_ftgo_enrichment(
        monkeypatch,
        names=["iShares Core S&P 500 UCITS ETF USD (Acc)"],
        ter=0.07,
    )

    info = {"name": "ISHARES CORE S&P 500", "tickers": ["CSSPX"], "exchange": "SW"}
    enriched = enrich_fund_metadata(ISIN, info)

    assert enriched["fund_currency"] == "USD"
    assert enriched["distribution"] == "Accumulating"
    assert enriched["ter"] == 0.07


def test_enrich_uses_ftgo_when_openfigi_silent(monkeypatch):
    _mock_ftgo_enrichment(
        monkeypatch,
        names=["Some ETF UCITS ETF USD (Dist)"],
    )

    info = {"name": "Some Generic ETF Name", "tickers": ["ABC"], "exchange": "LN"}
    enriched = enrich_fund_metadata(ISIN, info)

    assert enriched["distribution"] == "Distributing"
    assert enriched["fund_currency"] == "USD"


def test_enrich_falls_back_to_justetf_ter(monkeypatch, capsys):
    html = '<div data-testid="tl_etf-basics_value_ter">0.07% p.a.</div>'
    _mock_ftgo_enrichment(
        monkeypatch,
        names=["Amundi Prime All Country World UCITS ETF Acc"],
        ter=None,
        justetf_html=html,
    )

    info = {"name": "AMUNDI PRME ALL CTRY WLD ACC", "tickers": ["WEBN"], "exchange": "GR"}
    enriched = enrich_fund_metadata(ISIN, info)
    out = capsys.readouterr().out

    assert enriched["ter"] == 0.07
    assert "used justETF (0.07%)" in out


def test_enrich_warns_when_name_inference_used_as_fallback(monkeypatch, capsys):
    # justETF returns no structured fields → name parsing kicks in as last resort
    _mock_ftgo_enrichment(
        monkeypatch,
        names=["Amundi Prime All Country World UCITS ETF USD Acc"],
    )

    info = {"name": "AMUNDI PRME ALL CTRY WLD USD ACC", "tickers": ["WEBN"], "exchange": "GR"}
    enrich_fund_metadata(ISIN, info)
    out = capsys.readouterr().out
    assert "justETF missing" in out
    assert "inferred from name" in out


def test_justetf_field_parses_basics_table():
    html = (
        '<div data-testid="tl_etf-basics_value_ter">0.07% p.a.</div>'
        '<td data-testid="tl_etf-basics_value_fund-currency">USD</td>'
        '<div data-testid="tl_etf-basics_value_distribution-policy">Accumulating</div>'
        '<div data-testid="tl_etf-basics_value_investment-focus">Equity, World</div>'
    )
    assert _justetf_field(html, "ter") == "0.07% p.a."
    assert _justetf_field(html, "fund-currency") == "USD"
    assert _justetf_field(html, "distribution-policy") == "Accumulating"
    assert _justetf_field(html, "investment-focus") == "Equity, World"


def test_fetch_justetf_html(monkeypatch):
    class FakeResponse:
        text = '<div data-testid="tl_etf-basics_value_ter">0.07% p.a.</div>'

        @staticmethod
        def raise_for_status() -> None:
            return None

    monkeypatch.setattr("e1f.common.universe.requests.get", lambda *a, **k: FakeResponse())
    html = _fetch_justetf_html(ISIN)
    assert html is not None
    assert "0.07%" in html


def test_enrich_uses_justetf_for_currency_silently(monkeypatch, capsys):
    # justETF is primary: currency from structured field, no warning emitted
    html = '<td data-testid="tl_etf-basics_value_fund-currency">USD</td>'
    _mock_ftgo_enrichment(
        monkeypatch,
        names=["Amundi Prime All Country World UCITS ETF Acc"],
        justetf_html=html,
    )

    info = {"name": "AMUNDI PRME ALL CTRY WLD ACC", "tickers": ["WEBN"], "exchange": "GR"}
    enriched = enrich_fund_metadata(ISIN, info)
    out = capsys.readouterr().out

    assert enriched["fund_currency"] == "USD"
    assert "fund currency" not in out


def test_enrich_uses_justetf_distribution_when_names_silent(monkeypatch):
    html = '<div data-testid="tl_etf-basics_value_distribution-policy">Distributing</div>'
    _mock_ftgo_enrichment(monkeypatch, names=[], justetf_html=html)

    info = {"name": "Some Generic ETF Name", "tickers": ["ABC"], "exchange": "LN"}
    enriched = enrich_fund_metadata(ISIN, info)

    assert enriched["distribution"] == "Distributing"


def test_enrich_uses_justetf_investment_focus_for_asset_class(monkeypatch):
    html = (
        '<div data-testid="tl_etf-basics_value_investment-focus">'
        "Bonds, World, Aggregate, All maturities</div>"
    )
    _mock_ftgo_enrichment(
        monkeypatch,
        names=["Some Bond ETF USD (Acc)"],
        ter=0.1,
        justetf_html=html,
    )

    info = {"name": "Some Bond ETF USD (Acc)", "tickers": ["BND"], "exchange": "LN"}
    enriched = enrich_fund_metadata(ISIN, info)

    assert enriched["asset_class"] == "Bonds"


def test_ftgo_listing_names_and_fund_name(monkeypatch):
    _mock_ftgo_enrichment(
        monkeypatch,
        names=["iShares Core S&P 500 UCITS ETF USD (Acc)"],
    )
    names, error = _ftgo_listing_names(ISIN)
    assert error is None
    assert len(names) == 1

    name, err = _ftgo_fund_name(ISIN, "ISHARES CORE S&P 500")
    assert err is None
    assert "Acc" in (name or "")


def test_ftgo_ter_parses_ongoing_charge(monkeypatch):
    class FakeMatches:
        def iterrows(self):
            yield 0, {"name": "Test ETF USD (Acc)", "xid": "123"}

        def iloc(self):
            return self

        def __getitem__(self, idx):
            return {"xid": "123"}

    monkeypatch.setattr(
        "e1f.common.universe.get_fund_stats",
        lambda xid: {"Ongoing charge": "0.22%"},
    )

    from e1f.common.universe import _ftgo_ter

    ter = _ftgo_ter(FakeMatches(), "Test ETF USD (Acc)")
    assert ter == 0.22


def test_short_lookup_error_normalizes_messages():
    assert _short_lookup_error(RuntimeError("HTTP Error 404: not found")) == "quote not found"
    assert _short_lookup_error(RuntimeError("429 Too Many Requests")) == "rate limited"


def test_enrich_fund_metadata_warns_when_ftgo_fails_but_openfigi_parses(monkeypatch, capsys):
    _mock_ftgo_enrichment(monkeypatch, error="no FT Markets listing")

    info = {"name": "VANG FTSE AW USDA", "tickers": ["VWRA"], "exchange": "LN"}
    enriched = enrich_fund_metadata(ISIN, info)
    out = capsys.readouterr().out

    assert enriched["fund_currency"] == "USD"
    assert enriched["distribution"] == "Accumulating"
    assert f"⚠ ftgo {ISIN}: no FT Markets listing" in out
    assert "justETF missing" in out
    assert "inferred from name" in out


# ---------------------------------------------------------------------------
# FX rate lookup / conversion (ADR-0010)
# ---------------------------------------------------------------------------


def _fx_db(tmp_path, rows):
    """Build a DB with an fx_rates table populated with (base, quote, date, rate)."""
    db = tmp_path / "fx.db"
    with closing(sqlite3.connect(str(db))) as conn:
        conn.execute(
            "CREATE TABLE fx_rates (base TEXT, quote TEXT, date TEXT, rate REAL, "
            "PRIMARY KEY (base, quote, date))"
        )
        conn.executemany("INSERT INTO fx_rates VALUES (?, ?, ?, ?)", rows)
        conn.commit()
    return str(db)


def test_fx_rate_asof_identity_needs_no_db():
    # base == quote is 1.0 without touching the DB (path is never opened).
    assert fx_rate_asof("/no/such.db", "EUR", "2026-08-20", base="EUR") == 1.0


def test_fx_rate_asof_exact_and_nearest_prior(tmp_path):
    db = _fx_db(
        tmp_path,
        [
            ("EUR", "USD", "2026-08-14", 1.16),
            ("EUR", "USD", "2026-08-17", 1.17),
        ],
    )
    assert fx_rate_asof(db, "USD", "2026-08-14") == 1.16  # exact
    assert fx_rate_asof(db, "USD", "2026-08-16") == 1.16  # weekend: prior
    assert fx_rate_asof(db, "USD", "2026-08-20") == 1.17  # after last: last


def test_fx_rate_asof_before_series_raises(tmp_path):
    db = _fx_db(tmp_path, [("EUR", "USD", "2026-08-14", 1.16)])
    with pytest.raises(ValueError, match="on or before 2026-08-10"):
        fx_rate_asof(db, "USD", "2026-08-10")  # leading-edge gap: no prior rate


def test_fx_rate_asof_unfetched_pair_raises(tmp_path):
    db = _fx_db(tmp_path, [("EUR", "USD", "2026-08-14", 1.16)])
    with pytest.raises(ValueError, match="no EUR/GBP FX rate"):
        fx_rate_asof(db, "GBP", "2026-08-14")


def test_convert_to_eur_passthrough_for_base(tmp_path):
    # EUR amounts convert to themselves without needing a rate.
    assert convert_to_eur(100.0, "EUR", "2026-08-20", "/no/such.db") == 100.0


def test_convert_to_eur_divides_by_quote_per_base(tmp_path):
    db = _fx_db(tmp_path, [("EUR", "USD", "2026-08-14", 1.25)])
    # 125 USD / (1.25 USD per EUR) = 100 EUR
    assert convert_to_eur(125.0, "USD", "2026-08-14", db) == 100.0


def test_convert_to_eur_refuses_pence(tmp_path):
    with pytest.raises(ValueError, match=r"GBX .*no EUR FX rule"):
        convert_to_eur(100.0, "GBX", "2026-08-14", "/no/such.db")


# ---------------------------------------------------------------------------
# Trade loading / position timeline / pinned currency (ADR-0011)
# ---------------------------------------------------------------------------


def _tx_db(tmp_path, rows):
    """Build a DB with a transactions table; rows are the canonical 9-tuples."""
    db = tmp_path / "tx.db"
    with closing(sqlite3.connect(str(db))) as conn:
        conn.execute(
            "CREATE TABLE transactions (broker TEXT, transaction_id TEXT, "
            "datetime TEXT, symbol TEXT, side TEXT, shares REAL, price REAL, "
            "fee REAL, tax REAL, PRIMARY KEY (broker, transaction_id))"
        )
        conn.executemany("INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        conn.commit()
    return str(db)


def test_load_trades_missing_table_is_empty(tmp_path):
    db = tmp_path / "empty.db"
    with closing(sqlite3.connect(str(db))) as conn:
        conn.execute("CREATE TABLE other (x INTEGER)")
        conn.commit()
    assert load_trades(str(db)) == []


def test_load_trades_orders_by_datetime(tmp_path):
    db = _tx_db(
        tmp_path,
        [
            ("tr", "t2", "2026-02-01", "ISIN1", "BUY", 1.0, 100.0, 1.0, 0.0),
            ("tr", "t1", "2026-01-01", "ISIN1", "BUY", 2.0, 90.0, 1.0, 0.0),
        ],
    )
    dates = [row[1] for row in load_trades(db)]
    assert dates == ["2026-01-01", "2026-02-01"]


def test_position_timeline_nets_across_brokers():
    rows = [
        ("tr", "2026-01-01", "ISIN1", "BUY", 1.0, 100.0, 1.0),
        ("xtb", "2026-02-01", "ISIN1", "BUY", 2.0, 110.0, 0.0),
    ]
    timeline = position_timeline(rows)
    assert list(timeline) == ["ISIN1"]
    events = timeline["ISIN1"]
    # Netted: shares accumulate across brokers, average-cost running total.
    assert events[0].shares_held == 1.0
    assert events[0].cash_flow == 101.0
    assert events[1].shares_held == 3.0
    assert events[1].cost_basis == pytest.approx(321.0)


def test_position_timeline_sell_reduces_no_cash_flow():
    rows = [
        ("tr", "2026-01-01", "ISIN1", "BUY", 2.0, 100.0, 0.0),
        ("tr", "2026-03-01", "ISIN1", "SELL", 1.0, 130.0, 0.0),
    ]
    events = position_timeline(rows)["ISIN1"]
    assert events[1].shares_held == 1.0
    assert events[1].cost_basis == pytest.approx(100.0)  # average-cost reduction
    assert events[1].cash_flow == 0.0  # buy-and-hold: sells are not modelled as inflows


def test_position_timeline_skips_nonpositive_and_orphan_sell():
    rows = [
        ("tr", "2026-01-01", "ISIN1", "BUY", 0.0, 100.0, 0.0),  # zero-share buy skipped
        ("tr", "2026-02-01", "ISIN2", "SELL", 1.0, 100.0, 0.0),  # sell with no position skipped
        ("tr", "2026-02-02", "ISIN3", "DIVIDEND", 1.0, 5.0, 0.0),  # unknown side skipped
    ]
    assert position_timeline(rows) == {}


def test_position_timeline_date_prefix_from_datetime():
    rows = [("tr", "2026-01-01 09:30:00", "ISIN1", "BUY", 1.0, 100.0, 0.0)]
    assert position_timeline(rows)["ISIN1"][0].date == "2026-01-01"


def test_pinned_quote_currency_reads_sidecar(tmp_path):
    meta = tmp_path / "meta.yaml"
    meta.write_text(
        yaml.dump(
            {
                "IE00B4L5Y983": {"currency": "USD", "symbol": "X:LSE:USD", "xid": "1"},
            }
        )
    )
    assert pinned_quote_currency("IE00B4L5Y983", str(meta)) == "USD"


def test_pinned_quote_currency_absent_is_none(tmp_path):
    meta = tmp_path / "meta.yaml"
    meta.write_text(yaml.dump({"fx_pairs": {"EURUSD": {"xid": "9"}}}))
    assert pinned_quote_currency("IE00UNKNOWN000", str(meta)) is None
    assert pinned_quote_currency("IE00B4L5Y983", str(tmp_path / "missing.yaml")) is None


# ---------------------------------------------------------------------------
# Pure XIRR solver (Newton + bisection) — graduated from performance (ADR-0019).
# ---------------------------------------------------------------------------


def test_xirr_lump_sum_known_10_percent():
    # -1000 today, +1100 one year later => exactly 10% annualized.
    assert xirr([("2024-01-01", -1000.0), ("2024-12-31", 1100.0)]) == pytest.approx(0.10)


def test_xirr_doubling_over_two_years_is_root_two_minus_one():
    rate = xirr([("2020-01-01", -1000.0), ("2021-12-31", 2000.0)])
    assert rate == pytest.approx(2**0.5 - 1, rel=1e-4)  # ~41.42%


def test_xirr_multiple_contributions():
    rate = xirr(
        [
            ("2024-01-01", -1000.0),
            ("2024-07-01", -1000.0),
            ("2024-12-31", 2100.0),
        ]
    )
    assert rate is not None and rate > 0.0


def test_xirr_requires_two_flows():
    assert xirr([("2024-01-01", -1000.0)]) is None


def test_xirr_requires_sign_change():
    assert xirr([("2024-01-01", -1.0), ("2024-06-01", -2.0)]) is None
    assert xirr([("2024-01-01", 1.0), ("2024-06-01", 2.0)]) is None


def test_xirr_falls_back_to_bisection(monkeypatch):
    # When Newton yields nothing, xirr still returns the bisection root.
    monkeypatch.setattr(_metrics, "_newton", lambda flows: None)
    assert xirr([("2024-01-01", -1000.0), ("2024-12-31", 1100.0)]) == pytest.approx(0.10)


def test_newton_returns_none_on_divergence():
    flows = [(0.0, -1000.0), (1.0, 1100.0)]
    assert _newton(flows, guess=50.0) is None


def test_newton_returns_none_on_zero_derivative():
    # Same-date opposite flows: NPV is constant in rate, derivative is 0.
    assert _newton([(0.0, -100.0), (0.0, 100.0)]) is None


def test_bisect_finds_root_and_reports_no_sign_change():
    flows = [(0.0, -1000.0), (1.0, 1100.0)]
    assert _bisect(flows) == pytest.approx(0.10, rel=1e-4)
    assert _bisect([(0.0, 100.0), (1.0, 100.0)]) is None  # both endpoints positive
