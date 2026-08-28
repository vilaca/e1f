"""Typed currency-metadata boundary and persistence."""

import pytest
import yaml

from e1f.common import CurrencyMetadata, CurrencyMetadataError


ISIN = "IE00B4L5Y983"


def test_currency_metadata_separates_funds_and_fx_pairs(tmp_path):
    path = tmp_path / "currency.yaml"
    path.write_text(
        yaml.dump(
            {
                ISIN: {"currency": "USD", "symbol": "IWDA:LSE:USD", "xid": "1"},
                "fx_pairs": {"EURUSD": {"symbol": "EURUSD", "xid": "2"}},
            }
        )
    )

    metadata = CurrencyMetadata.load(str(path))

    assert set(metadata.funds) == {ISIN}
    assert set(metadata.fx_pairs) == {"EURUSD"}
    assert "fx_pairs" not in metadata.funds


def test_currency_metadata_save_roundtrip(tmp_path):
    path = tmp_path / "nested" / "currency.yaml"
    expected = CurrencyMetadata(
        funds={ISIN: {"currency": "EUR"}},
        fx_pairs={"EURUSD": {"symbol": "EURUSD", "xid": "2"}},
    )

    expected.save(str(path))

    assert CurrencyMetadata.load(str(path)) == expected


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ([], "root must be a mapping"),
        ({"fx_pairs": []}, "'fx_pairs' must be a mapping"),
        ({"not-an-isin": []}, "invalid fund metadata entry"),
        ({"fx_pairs": {"USD": {}}}, "invalid FX-pair metadata entry"),
        ({"": {}}, "invalid fund metadata entry"),
    ],
)
def test_currency_metadata_rejects_malformed_shapes(tmp_path, raw, match):
    path = tmp_path / "currency.yaml"
    path.write_text(yaml.dump(raw))

    with pytest.raises(CurrencyMetadataError, match=match):
        CurrencyMetadata.load(str(path))
