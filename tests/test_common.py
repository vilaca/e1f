"""ConfigManager (YAML universe) and OpenFIGIResolver (mocked HTTP)."""

import pytest
import requests
import yaml

from e1f.common import (
    ConfigManager,
    OpenFIGIResolver,
    _asset_class_from_investment_focus,
    _best_ftgo_name,
    _fund_currency_from_names,
    _parse_percent_value,
    _justetf_field,
    _fetch_justetf_html,
    _ftgo_fund_name,
    _ftgo_listing_names,
    _short_lookup_error,
    distribution_from_name,
    enrich_fund_metadata,
    fund_currency_from_name,
)

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
    assert info['listings'] == [{'ticker': 'TST', 'exchange': 'NA'}]
    assert info['source'] == 'OpenFIGI'


def test_resolve_collects_xtb_listings(monkeypatch):
    payload = [{'data': [
        {'name': 'Global Bond', 'ticker': '0GGH', 'exchCode': 'LN', 'figi': 'BBG1'},
        {'name': 'Global Bond', 'ticker': 'EUNA', 'exchCode': 'GR', 'figi': 'BBG2'},
        {'name': 'Global Bond', 'ticker': 'SKIP', 'exchCode': 'XX', 'figi': 'BBG3'},
    ]}]
    info = resolver_returning(monkeypatch, payload).resolve(ISIN)
    assert info['exchange'] == 'LN'
    assert info['figi'] == 'BBG1'
    assert info['tickers'] == ['0GGH', 'EUNA']
    assert info['listings'] == [
        {'ticker': '0GGH', 'exchange': 'LN'},
        {'ticker': 'EUNA', 'exchange': 'GR'},
    ]


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
    monkeypatch.setattr('e1f.common.enrich_fund_metadata',
                        lambda isin, info: info)
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


# ---------------------------------------------------------------------------
# Fund metadata enrichment
# ---------------------------------------------------------------------------

def test_fund_currency_from_name():
    assert fund_currency_from_name('VANG FTSE AW USDA') == 'USD'
    assert fund_currency_from_name('iShares Core S&P 500 UCITS ETF USD (Acc)') == 'USD'
    assert fund_currency_from_name('X MSCI WORLD HEALTH CARE') is None


def test_distribution_from_name():
    assert distribution_from_name('VANG FTSE AW USDA') == 'Accumulating'
    assert distribution_from_name('VANG FTSE AW USDD') == 'Distributing'
    assert distribution_from_name('iShares Core S&P 500 UCITS ETF USD (Acc)') == 'Accumulating'
    assert distribution_from_name('AMUNDI PRME ALL CTRY WLD ACC') == 'Accumulating'
    assert distribution_from_name('Some ETF EUR Hedged') is None


def test_asset_class_from_investment_focus():
    assert _asset_class_from_investment_focus('Equity, United States') == 'Equity'
    assert _asset_class_from_investment_focus(
        'Bonds, World, Aggregate, All maturities'
    ) == 'Bonds'
    assert _asset_class_from_investment_focus('') is None


def test_best_ftgo_name_prefers_share_class_matching_openfigi_hint():
    names = [
        'Amundi Prime All Country World UCITS ETF USD Dist',
        'Amundi Prime All Country World UCITS ETF USD Acc',
    ]
    chosen = _best_ftgo_name(names, 'AMUNDI PRME ALL CTRY WLD ACC')
    assert chosen.endswith('Acc')


def test_fund_currency_from_names_finds_usd_on_sibling_ftgo_listing():
    names = [
        'Amundi Prime All Country World UCITS ETF USD Dist',
        'Amundi Prime All Country World UCITS ETF Acc',
    ]
    assert _fund_currency_from_names(names) == 'USD'
    assert fund_currency_from_name(names[1]) is None


def _mock_ftgo_enrichment(monkeypatch, *, names=None, error=None, ter=None, justetf_html=None):
    if error:
        monkeypatch.setattr('e1f.common._ftgo_load', lambda isin: (None, error))
    else:
        monkeypatch.setattr('e1f.common._ftgo_load', lambda isin: ('matches', None))
        monkeypatch.setattr(
            'e1f.common._names_from_ftgo_matches',
            lambda matches: names or [],
        )
        monkeypatch.setattr('e1f.common._ftgo_ter', lambda matches, hint: ter)
    monkeypatch.setattr('e1f.common._fetch_justetf_html', lambda isin: justetf_html)


def test_parse_percent_value():
    assert _parse_percent_value('0.07%') == 0.07
    assert _parse_percent_value('--') is None
    assert _parse_percent_value('') is None


def test_enrich_prefers_openfigi_distribution_over_conflicting_ftgo_name(
    monkeypatch,
):
    ftgo_names = [
        'Amundi Prime All Country World UCITS ETF USD Dist',
        'Amundi Prime All Country World UCITS ETF Acc',
    ]
    _mock_ftgo_enrichment(monkeypatch, names=ftgo_names)

    info = {'name': 'AMUNDI PRME ALL CTRY WLD ACC', 'tickers': ['WEBN'], 'exchange': 'GR'}
    enriched = enrich_fund_metadata(ISIN, info)

    assert enriched['distribution'] == 'Accumulating'
    assert enriched['fund_currency'] == 'USD'


def test_enrich_fund_metadata_merges_openfigi_and_ftgo(monkeypatch):
    _mock_ftgo_enrichment(
        monkeypatch,
        names=['iShares Core S&P 500 UCITS ETF USD (Acc)'],
        ter=0.07,
    )

    info = {'name': 'ISHARES CORE S&P 500', 'tickers': ['CSSPX'], 'exchange': 'SW'}
    enriched = enrich_fund_metadata(ISIN, info)

    assert enriched['fund_currency'] == 'USD'
    assert enriched['distribution'] == 'Accumulating'
    assert enriched['ter'] == 0.07


def test_enrich_uses_ftgo_when_openfigi_silent(monkeypatch):
    _mock_ftgo_enrichment(
        monkeypatch,
        names=['Some ETF UCITS ETF USD (Dist)'],
    )

    info = {'name': 'Some Generic ETF Name', 'tickers': ['ABC'], 'exchange': 'LN'}
    enriched = enrich_fund_metadata(ISIN, info)

    assert enriched['distribution'] == 'Distributing'
    assert enriched['fund_currency'] == 'USD'


def test_enrich_falls_back_to_justetf_ter(monkeypatch, capsys):
    html = '<div data-testid="tl_etf-basics_value_ter">0.07% p.a.</div>'
    _mock_ftgo_enrichment(monkeypatch, names=['Amundi Prime All Country World UCITS ETF Acc'],
                          ter=None, justetf_html=html)

    info = {'name': 'AMUNDI PRME ALL CTRY WLD ACC', 'tickers': ['WEBN'], 'exchange': 'GR'}
    enriched = enrich_fund_metadata(ISIN, info)
    out = capsys.readouterr().out

    assert enriched['ter'] == 0.07
    assert 'used justETF (0.07%)' in out


def test_enrich_warns_when_ftgo_name_conflicts_with_openfigi(monkeypatch, capsys):
    _mock_ftgo_enrichment(
        monkeypatch,
        names=['Amundi Prime All Country World UCITS ETF USD Dist'],
    )

    info = {'name': 'AMUNDI PRME ALL CTRY WLD ACC', 'tickers': ['WEBN'], 'exchange': 'GR'}
    enrich_fund_metadata(ISIN, info)
    out = capsys.readouterr().out
    assert 'using OpenFIGI share class (Accumulating)' in out


def test_justetf_field_parses_basics_table():
    html = (
        '<div data-testid="tl_etf-basics_value_ter">0.07% p.a.</div>'
        '<td data-testid="tl_etf-basics_value_fund-currency">USD</td>'
        '<div data-testid="tl_etf-basics_value_distribution-policy">Accumulating</div>'
        '<div data-testid="tl_etf-basics_value_investment-focus">Equity, World</div>'
    )
    assert _justetf_field(html, 'ter') == '0.07% p.a.'
    assert _justetf_field(html, 'fund-currency') == 'USD'
    assert _justetf_field(html, 'distribution-policy') == 'Accumulating'
    assert _justetf_field(html, 'investment-focus') == 'Equity, World'


def test_fetch_justetf_html(monkeypatch):
    class FakeResponse:
        text = '<div data-testid="tl_etf-basics_value_ter">0.07% p.a.</div>'

        @staticmethod
        def raise_for_status() -> None:
            return None

    monkeypatch.setattr('e1f.common.requests.get', lambda *a, **k: FakeResponse())
    html = _fetch_justetf_html(ISIN)
    assert html is not None
    assert '0.07%' in html


def test_enrich_uses_justetf_for_missing_currency(monkeypatch, capsys):
    html = '<td data-testid="tl_etf-basics_value_fund-currency">USD</td>'
    _mock_ftgo_enrichment(
        monkeypatch,
        names=['Amundi Prime All Country World UCITS ETF Acc'],
        justetf_html=html,
    )

    info = {'name': 'AMUNDI PRME ALL CTRY WLD ACC', 'tickers': ['WEBN'], 'exchange': 'GR'}
    enriched = enrich_fund_metadata(ISIN, info)
    out = capsys.readouterr().out

    assert enriched['fund_currency'] == 'USD'
    assert 'used justETF (USD)' in out


def test_enrich_uses_justetf_distribution_when_names_silent(monkeypatch):
    html = '<div data-testid="tl_etf-basics_value_distribution-policy">Distributing</div>'
    _mock_ftgo_enrichment(monkeypatch, names=[], justetf_html=html)

    info = {'name': 'Some Generic ETF Name', 'tickers': ['ABC'], 'exchange': 'LN'}
    enriched = enrich_fund_metadata(ISIN, info)

    assert enriched['distribution'] == 'Distributing'


def test_enrich_uses_justetf_investment_focus_for_asset_class(monkeypatch):
    html = (
        '<div data-testid="tl_etf-basics_value_investment-focus">'
        'Bonds, World, Aggregate, All maturities</div>'
    )
    _mock_ftgo_enrichment(
        monkeypatch,
        names=['Some Bond ETF USD (Acc)'],
        ter=0.1,
        justetf_html=html,
    )

    info = {'name': 'Some Bond ETF USD (Acc)', 'tickers': ['BND'], 'exchange': 'LN'}
    enriched = enrich_fund_metadata(ISIN, info)

    assert enriched['asset_class'] == 'Bonds'


def test_ftgo_listing_names_and_fund_name(monkeypatch):
    _mock_ftgo_enrichment(
        monkeypatch,
        names=['iShares Core S&P 500 UCITS ETF USD (Acc)'],
    )
    names, error = _ftgo_listing_names(ISIN)
    assert error is None
    assert len(names) == 1

    name, err = _ftgo_fund_name(ISIN, 'ISHARES CORE S&P 500')
    assert err is None
    assert 'Acc' in (name or '')


def test_ftgo_ter_parses_ongoing_charge(monkeypatch):
    class FakeMatches:
        def iterrows(self):
            yield 0, {'name': 'Test ETF USD (Acc)', 'xid': '123'}

        def iloc(self):
            return self

        def __getitem__(self, idx):
            return {'xid': '123'}

    monkeypatch.setattr('ftgo.get_fund_stats', lambda xid: {'Ongoing charge': '0.22%'})

    import e1f.common as common_mod
    ter = common_mod._ftgo_ter(FakeMatches(), 'Test ETF USD (Acc)')
    assert ter == 0.22


def test_short_lookup_error_normalizes_messages():
    assert _short_lookup_error(RuntimeError('HTTP Error 404: not found')) == 'quote not found'
    assert _short_lookup_error(RuntimeError('429 Too Many Requests')) == 'rate limited'


def test_enrich_fund_metadata_warns_when_ftgo_fails_but_openfigi_parses(monkeypatch, capsys):
    _mock_ftgo_enrichment(monkeypatch, error='no FT Markets listing')

    info = {'name': 'VANG FTSE AW USDA', 'tickers': ['VWRA'], 'exchange': 'LN'}
    enriched = enrich_fund_metadata(ISIN, info)
    out = capsys.readouterr().out

    assert enriched['fund_currency'] == 'USD'
    assert enriched['distribution'] == 'Accumulating'
    assert f'⚠ ftgo {ISIN}: no FT Markets listing' in out
    assert 'used OpenFIGI name for fund currency/distribution' in out
