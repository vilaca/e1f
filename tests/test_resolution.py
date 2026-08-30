"""ftgo resolution is by ISIN and pinned so the security can't drift.

FT Markets search ordering is not stable, so resolving by the ticker's first
match (or re-searching every fetch) can silently switch to a different
listing/currency. These tests assert we resolve by ISIN, pin the chosen
listing (share-class currency, then EUR then USD), and reuse it.
"""

import pandas as pd
import pytest

from e1f import fetch as etf_fetch


def _matches(*rows):
    return pd.DataFrame(list(rows))


@pytest.fixture
def extractor(tmp_path):
    (tmp_path / "u.yaml").write_text("etfs: {}\n")
    return etf_fetch.DataExtractor(
        config_path=str(tmp_path / "u.yaml"),
        db_path=str(tmp_path / "u.db"),
        currency_meta_path=str(tmp_path / "currency.yaml"),
    )


def test_resolves_by_isin(monkeypatch, extractor):
    seen = {}

    def fake_get_xid(query, display_mode="first"):
        seen['query'] = query
        return _matches(
            {"xid": 26390464, "symbol": "CSPX:LSE:USD",
             "name": "iShares Core S&P 500 UCITS ETF USD (Acc)", "asset_class": "ETFs"},
        )

    monkeypatch.setattr(etf_fetch, "get_xid", fake_get_xid)
    res = extractor._resolve_ftgo("IE00B5BMR087")

    assert seen['query'] == "IE00B5BMR087"           # searched by ISIN, not ticker
    assert res == {"xid": "26390464", "symbol": "CSPX:LSE:USD", "currency": "USD"}


def test_prefers_the_funds_base_currency(monkeypatch, extractor):
    # A USD fund cross-listed in EUR/GBX/USD: pick the USD listing even though
    # it isn't first, because the name says the share class is USD.
    def fake_get_xid(query, display_mode="first"):
        name = "iShares Core S&P 500 UCITS ETF USD (Acc)"
        return _matches(
            {"xid": 1, "symbol": "SXR8:GER:EUR", "name": name, "asset_class": "ETFs"},
            {"xid": 2, "symbol": "CSP1:LSE:GBX", "name": name, "asset_class": "ETFs"},
            {"xid": 3, "symbol": "CSPX:LSE:USD", "name": name, "asset_class": "ETFs"},
        )

    monkeypatch.setattr(etf_fetch, "get_xid", fake_get_xid)
    res = extractor._resolve_ftgo("IE00B5BMR087")
    assert res["symbol"] == "CSPX:LSE:USD" and res["currency"] == "USD"


def test_falls_back_to_first_match_when_base_currency_absent(monkeypatch, extractor):
    # Name says EUR (hedged class) but no EUR listing exists -> first match.
    def fake_get_xid(query, display_mode="first"):
        name = "iShares Core S&P 500 UCITS ETF EUR Hedged (Acc)"
        return _matches(
            {"xid": 1, "symbol": "AAA:LSE:USD", "name": name, "asset_class": "ETFs"},
            {"xid": 2, "symbol": "BBB:GER:CHF", "name": name, "asset_class": "ETFs"},
        )

    monkeypatch.setattr(etf_fetch, "get_xid", fake_get_xid)
    res = extractor._resolve_ftgo("IE00XXHEDGED0")
    assert res["symbol"] == "AAA:LSE:USD"


def test_prefers_eur_over_usd_when_name_has_no_currency_hint(monkeypatch, extractor):
    # No currency token: USD is first, EUR second — still pin EUR (ADR-0043).
    name = "Xtrackers MSCI World Health Care UCITS ETF 1C"
    assert etf_fetch.fund_currency_from_name(name) is None

    def fake_get_xid(query, display_mode="first"):
        return _matches(
            {"xid": 1, "symbol": "XDWH:LSE:USD", "name": name, "asset_class": "ETFs"},
            {"xid": 2, "symbol": "XDWH:LSE:GBX", "name": name, "asset_class": "ETFs"},
            {"xid": 3, "symbol": "XDWH:GER:EUR", "name": name, "asset_class": "ETFs"},
        )

    monkeypatch.setattr(etf_fetch, "get_xid", fake_get_xid)
    res = extractor._resolve_ftgo("IE00BM67HK77")
    assert res["symbol"] == "XDWH:GER:EUR" and res["currency"] == "EUR"


def test_prefers_usd_when_eur_absent_and_name_has_no_currency_hint(monkeypatch, extractor):
    # No EUR line: CHF is first, USD second — still pin USD, not GBX/CHF.
    name = "Some UCITS ETF 1C"

    def fake_get_xid(query, display_mode="first"):
        return _matches(
            {"xid": 1, "symbol": "FOO:SWX:CHF", "name": name, "asset_class": "ETFs"},
            {"xid": 2, "symbol": "FOO:LSE:GBX", "name": name, "asset_class": "ETFs"},
            {"xid": 3, "symbol": "FOO:LSE:USD", "name": name, "asset_class": "ETFs"},
        )

    monkeypatch.setattr(etf_fetch, "get_xid", fake_get_xid)
    res = extractor._resolve_ftgo("IE00NOHINTUSD")
    assert res["symbol"] == "FOO:LSE:USD" and res["currency"] == "USD"


def test_pins_gbx_only_when_it_is_the_sole_listing(monkeypatch, extractor):
    name = "Some UCITS ETF 1C"

    def fake_get_xid(query, display_mode="first"):
        return _matches(
            {"xid": 1, "symbol": "FOO:LSE:GBX", "name": name, "asset_class": "ETFs"},
        )

    monkeypatch.setattr(etf_fetch, "get_xid", fake_get_xid)
    res = extractor._resolve_ftgo("IE00GBXONLY00")
    assert res["symbol"] == "FOO:LSE:GBX" and res["currency"] == "GBX"


def test_resolution_is_pinned_and_reused(monkeypatch, extractor):
    calls = {"n": 0}

    def fake_get_xid(query, display_mode="first"):
        calls["n"] += 1
        # ordering flips on the second call — pinning must ignore it
        name = "iShares Core S&P 500 UCITS ETF USD (Acc)"
        rows = [
            {"xid": 111, "symbol": "SXR8:GER:EUR", "name": name, "asset_class": "ETFs"},
            {"xid": 26390464, "symbol": "CSPX:LSE:USD", "name": name, "asset_class": "ETFs"},
        ]
        if calls["n"] == 1:
            rows.reverse()
        return _matches(*rows)

    monkeypatch.setattr(etf_fetch, "get_xid", fake_get_xid)

    first = extractor._resolve_ftgo("IE00B5BMR087")
    second = extractor._resolve_ftgo("IE00B5BMR087")

    assert calls["n"] == 1                            # second call reused the pin
    assert first == second
    assert first["symbol"] == "CSPX:LSE:USD"

    # a fresh extractor loads the pin from the sidecar (still no new search)
    reloaded = etf_fetch.DataExtractor(
        config_path=extractor.config_path,
        db_path=extractor.db_path,
        currency_meta_path=extractor.currency_meta_path,
    )
    assert reloaded._resolve_ftgo("IE00B5BMR087")["xid"] == "26390464"
    assert calls["n"] == 1
