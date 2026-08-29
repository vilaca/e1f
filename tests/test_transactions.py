"""Trade Republic CSV ingest: parsing, dedup, CLI."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pandas as pd
import pytest
import yaml

from e1f import transactions as transactions_mod
from e1f.transactions import (
    BROKER_TRADE_REPUBLIC,
    BROKER_XTB,
    TradeRepublicImporter,
    XtbImporter,
    is_etf_trade_row,
    is_xtb_etf_trade_row,
    list_transaction_rows,
    load_xtb_cash_operations,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAMPLE_CSV = FIXTURES / "trade_republic_sample.csv"
SAMPLE_XTB_XLSX = FIXTURES / "xtb_cash_operations_sample.xlsx"
ISIN_ETF = "IE00B4L5Y983"
ISIN_WEBN = "IE0003XJA0J9"
ISIN_STOCK = "US0378331005"


def make_importer(tmp_path, config_isins=()) -> TradeRepublicImporter:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.dump({"etfs": {isin: {"name": isin} for isin in config_isins}})
    )
    return TradeRepublicImporter(
        db_path=str(tmp_path / "t.db"),
        config_path=str(config_path),
    )


def test_init_creates_transactions_table(tmp_path):
    importer = make_importer(tmp_path)
    with closing(sqlite3.connect(importer.db_path)) as conn:
        cols = conn.execute("PRAGMA table_info(transactions)").fetchall()
    assert [c[1] for c in cols] == [
        "broker",
        "transaction_id",
        "datetime",
        "symbol",
        "side",
        "shares",
        "price",
        "fee",
        "tax",
    ]
    not_null = {column[1]: bool(column[3]) for column in cols}
    assert not_null["side"]
    assert not_null["shares"]
    assert not_null["price"]


@pytest.mark.parametrize(
    "row",
    [
        ("tr", "bad-side", "2024-01-01", ISIN_ETF, "DIVIDEND", 1.0, 10.0, 0.0, 0.0),
        ("tr", "null-shares", "2024-01-01", ISIN_ETF, "BUY", None, 10.0, 0.0, 0.0),
        ("tr", "zero-shares", "2024-01-01", ISIN_ETF, "BUY", 0.0, 10.0, 0.0, 0.0),
        ("tr", "null-price", "2024-01-01", ISIN_ETF, "BUY", 1.0, None, 0.0, 0.0),
        ("tr", "infinite-price", "2024-01-01", ISIN_ETF, "BUY", 1.0, float("inf"), 0.0, 0.0),
    ],
)
def test_transactions_table_rejects_invalid_financial_rows(tmp_path, row):
    importer = make_importer(tmp_path)
    with (
        closing(sqlite3.connect(importer.db_path)) as conn,
        pytest.raises(sqlite3.IntegrityError),
    ):
        conn.execute("INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", row)


def test_init_migrates_valid_legacy_transactions_table(tmp_path):
    db = tmp_path / "legacy.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "CREATE TABLE transactions (broker TEXT, transaction_id TEXT, datetime TEXT, "
            "symbol TEXT, side TEXT, shares REAL, price REAL, fee REAL, tax REAL, "
            "PRIMARY KEY (broker, transaction_id))"
        )
        row = ("tr", "1", "2024-01-01", ISIN_ETF, "BUY", 1.0, 10.0, 0.0, 0.0)
        conn.execute("INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", row)
        conn.commit()

    transactions_mod.init_transactions_database(str(db))

    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT * FROM transactions").fetchone() == row
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("tr", "2", "2024-01-01", ISIN_ETF, "BUY", None, 10.0, 0.0, 0.0),
            )


def test_init_refuses_invalid_legacy_transaction_without_mutating(tmp_path):
    db = tmp_path / "legacy-invalid.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "CREATE TABLE transactions (broker TEXT, transaction_id TEXT, datetime TEXT, "
            "symbol TEXT, side TEXT, shares REAL, price REAL, fee REAL, tax REAL, "
            "PRIMARY KEY (broker, transaction_id))"
        )
        conn.execute(
            "INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("tr", "bad", "2024-01-01", ISIN_ETF, "BUY", None, 10.0, 0.0, 0.0),
        )
        conn.commit()

    with pytest.raises(ValueError, match="cannot harden transactions table"):
        transactions_mod.init_transactions_database(str(db))
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT shares FROM transactions").fetchone() == (None,)


def test_is_etf_trade_row_matches_sample_buy():
    df = pd.read_csv(SAMPLE_CSV, dtype=str, keep_default_na=False)
    assert is_etf_trade_row(df.iloc[1])
    assert not is_etf_trade_row(df.iloc[0])  # cash
    assert not is_etf_trade_row(df.iloc[2])  # stock dividend


def test_import_sample_csv(tmp_path):
    importer = make_importer(tmp_path)
    summary = importer.import_csv(SAMPLE_CSV)
    assert summary == transactions_mod.ImportSummary(
        inserted=1,
        skipped=0,
        filtered=2,
        errors=0,
        missing_isins=((ISIN_ETF, "Core MSCI World USD (Acc)"),),
    )

    with closing(sqlite3.connect(importer.db_path)) as conn:
        count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        buy = conn.execute(
            "SELECT symbol, side, shares, price FROM transactions WHERE transaction_id = ?",
            ("tr-uuid-002",),
        ).fetchone()

    assert count == 1
    assert buy == ("IE00B4L5Y983", "BUY", 2.243133, 89.161)


def test_stock_buy_is_filtered(tmp_path):
    csv_path = tmp_path / "stock_buy.csv"
    df = pd.read_csv(SAMPLE_CSV, dtype=str, keep_default_na=False)
    stock_buy = df.iloc[1].copy()
    stock_buy["transaction_id"] = "tr-uuid-stock-buy"
    stock_buy["type"] = "BUY"
    stock_buy["asset_class"] = "STOCK"
    stock_buy["name"] = "Apple Inc"
    stock_buy["symbol"] = ISIN_STOCK
    pd.concat([df, pd.DataFrame([stock_buy])], ignore_index=True).to_csv(
        csv_path, index=False
    )

    summary = make_importer(tmp_path).import_csv(csv_path)
    assert summary.inserted == 1
    assert summary.filtered == 3


def test_import_is_idempotent(tmp_path):
    importer = make_importer(tmp_path)
    first = importer.import_csv(SAMPLE_CSV)
    second = importer.import_csv(SAMPLE_CSV)
    assert first.inserted == 1 and first.skipped == 0 and first.filtered == 2
    assert second.inserted == 0 and second.skipped == 1 and second.filtered == 2


def test_missing_columns_raises(tmp_path):
    bad_csv = tmp_path / "bad.csv"
    bad_csv.write_text("date,type,transaction_id\n2024-01-01,BUY,abc\n")
    importer = make_importer(tmp_path)
    with pytest.raises(ValueError, match="missing required Trade Republic columns"):
        importer.import_csv(bad_csv)


def test_missing_transaction_id_counts_as_error(tmp_path):
    csv_path = tmp_path / "no_id.csv"
    df = pd.read_csv(SAMPLE_CSV, dtype=str, keep_default_na=False)
    df.loc[1, "transaction_id"] = ""
    df.to_csv(csv_path, index=False)

    importer = make_importer(tmp_path)
    summary = importer.import_csv(csv_path)
    assert summary.inserted == 0
    assert summary.skipped == 0
    assert summary.filtered == 2
    assert summary.errors == 1


def test_parse_float_enforces_required_field_contract():
    assert transactions_mod._parse_float("0.5", "shares", required=True) == 0.5
    with pytest.raises(ValueError, match=r"^shares is required$"):
        transactions_mod._parse_float("", "shares", required=True)
    with pytest.raises(ValueError, match=r"^shares must be positive$"):
        transactions_mod._parse_float("0", "shares", required=True)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("shares", ""),
        ("shares", "not-a-number"),
        ("shares", "0"),
        ("price", ""),
        ("price", "inf"),
        ("fee", "not-a-number"),
        ("tax", "nan"),
    ],
)
def test_invalid_trade_numeric_counts_as_error_and_is_not_stored(tmp_path, field, value):
    csv_path = tmp_path / "bad_numeric.csv"
    df = pd.read_csv(SAMPLE_CSV, dtype=str, keep_default_na=False)
    df.loc[1, field] = value
    df.to_csv(csv_path, index=False)

    importer = make_importer(tmp_path)
    summary = importer.import_csv(csv_path)
    assert summary.inserted == 0
    assert summary.errors == 1
    with closing(sqlite3.connect(importer.db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def test_missing_file_raises(tmp_path):
    importer = make_importer(tmp_path)
    with pytest.raises(FileNotFoundError):
        importer.import_csv(tmp_path / "missing.csv")


def test_missing_isins_omit_configured(tmp_path):
    importer = make_importer(tmp_path, config_isins=[ISIN_ETF])
    summary = importer.import_csv(SAMPLE_CSV)
    assert summary.missing_isins == ()


def test_missing_isins_none_when_all_configured(tmp_path):
    importer = make_importer(tmp_path, config_isins=[ISIN_ETF])
    summary = importer.import_csv(SAMPLE_CSV)
    assert summary.missing_isins == ()


def test_main_trade_republic_success(tmp_path, capsys):
    db = tmp_path / "t.db"
    config = tmp_path / "config.yaml"
    config.write_text(yaml.dump({"etfs": {}}))
    code = transactions_mod.main(
        [
            "trade-republic",
            str(SAMPLE_CSV),
            "--db",
            str(db),
            "--config",
            str(config),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "1 inserted" in out
    assert "2 filtered" in out
    assert ISIN_ETF in out
    assert ISIN_STOCK not in out
    assert f"e1f config add {ISIN_ETF}" in out


def test_main_tr_alias_success(tmp_path, capsys):
    db = tmp_path / "t.db"
    config = tmp_path / "config.yaml"
    config.write_text(yaml.dump({"etfs": {}}))
    code = transactions_mod.main(
        ["tr", str(SAMPLE_CSV), "--db", str(db), "--config", str(config)]
    )
    assert code == 0
    assert "1 inserted" in capsys.readouterr().out


def test_main_duplicate_reports_skipped(tmp_path, capsys):
    db = tmp_path / "t.db"
    config = tmp_path / "config.yaml"
    config.write_text(yaml.dump({"etfs": {}}))
    args = [
        "trade-republic",
        str(SAMPLE_CSV),
        "--db",
        str(db),
        "--config",
        str(config),
    ]
    assert transactions_mod.main(args) == 0
    code = transactions_mod.main(args)
    out = capsys.readouterr().out
    assert code == 0
    assert "0 inserted" in out
    assert "1 skipped" in out
    assert "2 filtered" in out


def test_main_missing_file_returns_1(capsys):
    code = transactions_mod.main(["trade-republic", "/no/such/file.csv"])
    assert code == 1
    assert "Error" in capsys.readouterr().out


def test_broker_constant_on_rows(tmp_path):
    importer = make_importer(tmp_path)
    importer.import_csv(SAMPLE_CSV)
    with closing(sqlite3.connect(importer.db_path)) as conn:
        brokers = {
            row[0]
            for row in conn.execute("SELECT DISTINCT broker FROM transactions").fetchall()
        }
    assert brokers == {BROKER_TRADE_REPUBLIC}


def test_list_rows_after_import(tmp_path):
    db = tmp_path / "t.db"
    importer = make_importer(tmp_path)
    importer.import_csv(SAMPLE_CSV)
    rows = list_transaction_rows(str(db))
    assert len(rows) == 1
    broker, dt, symbol, side, shares, price, _fee, _tax = rows[0]
    assert broker == BROKER_TRADE_REPUBLIC
    assert symbol == ISIN_ETF
    assert side == "BUY"
    assert shares == pytest.approx(2.243133)
    assert price == pytest.approx(89.161)
    assert dt.startswith("2024-04-16")


def test_list_rows_empty(tmp_path):
    db = tmp_path / "t.db"
    rows = list_transaction_rows(str(db))
    assert rows == []


def test_main_list_empty(tmp_path, capsys):
    db = tmp_path / "t.db"
    code = transactions_mod.main(["list", "--db", str(db)])
    out = capsys.readouterr().out
    assert code == 0
    assert "No transactions" in out


def test_main_list_after_import(tmp_path, capsys):
    db = tmp_path / "t.db"
    config = tmp_path / "config.yaml"
    config.write_text(yaml.dump({"etfs": {}}))
    assert transactions_mod.main(
        ["trade-republic", str(SAMPLE_CSV), "--db", str(db), "--config", str(config)]
    ) == 0
    code = transactions_mod.main(["list", "--db", str(db)])
    out = capsys.readouterr().out
    assert code == 0
    assert ISIN_ETF in out
    assert "Total: 1 transactions" in out


def test_sort_transaction_rows_by_isin_and_date():
    rows = [
        ("tr", "2024-01-02", "BBB", "BUY", 1.0, 10.0, 0.0, 0.0),
        ("tr", "2024-01-01", "AAA", "BUY", 2.0, 20.0, 0.0, 0.0),
    ]
    by_isin = transactions_mod.sort_transaction_rows(rows, sort_by="isin")
    assert [r[2] for r in by_isin] == ["AAA", "BBB"]
    by_date = transactions_mod.sort_transaction_rows(rows, sort_by="date", reverse=True)
    assert [r[2] for r in by_date] == ["BBB", "AAA"]


def test_build_ticker_to_isin_uses_listings(tmp_path):
    config_path = tmp_path / "config.yaml"
    isin = "IE00BDBRDM35"
    config_path.write_text(yaml.dump({
        "etfs": {
            isin: {
                "name": "Global Bond",
                "tickers": ["0GGH", "EUNA"],
                "exchange": "LN",
                "listings": [
                    {"ticker": "0GGH", "exchange": "LN"},
                    {"ticker": "EUNA", "exchange": "GR"},
                ],
            }
        }
    }))
    mapping = transactions_mod.build_ticker_to_isin(str(config_path))
    assert transactions_mod.resolve_xtb_ticker("EUNA.DE", mapping) == isin
    assert transactions_mod.resolve_xtb_ticker("0GGH.UK", mapping) == isin


def test_load_xtb_cash_operations_sample():
    df = load_xtb_cash_operations(SAMPLE_XTB_XLSX)
    assert list(df.columns) == [
        "type",
        "instrument",
        "ticker",
        "category",
        "time",
        "amount",
        "id",
        "comment",
    ]
    assert len(df) == 3


def test_load_xtb_cash_operations_keeps_ids_as_opaque_strings(tmp_path):
    path = tmp_path / "xtb_ids.xlsx"
    ids = [9007199254740990, None, 9007199254740991, "9007199254740993"]
    pd.DataFrame(
        {
            "Type": ["BUY"] * len(ids),
            "Instrument": ["ETF"] * len(ids),
            "Ticker": ["ETF.DE"] * len(ids),
            "Category": ["STOCK PURCHASE"] * len(ids),
            "Time": ["2026-01-01"] * len(ids),
            "Amount": [-100.0] * len(ids),
            "ID": ids,
            "Comment": ["OPEN BUY 1/1 @ 100"] * len(ids),
        }
    ).to_excel(path, sheet_name="Cash Operations", index=False)

    loaded = load_xtb_cash_operations(path)
    assert loaded["id"].tolist() == [
        "9007199254740990",
        "",
        "9007199254740991",
        "9007199254740993",
    ]
    assert all(isinstance(value, str) for value in loaded["id"])


def test_is_xtb_etf_trade_row_sample():
    df = load_xtb_cash_operations(SAMPLE_XTB_XLSX)
    flags = [is_xtb_etf_trade_row(row) for _, row in df.iterrows()]
    assert flags == [True, False, True]


def test_import_xtb_sample(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.dump(
            {
                "etfs": {
                    ISIN_WEBN: {
                        "name": "Prime All Country World",
                        "tickers": ["WEBN"],
                        "exchange": "GR",
                    }
                }
            }
        )
    )
    importer = XtbImporter(db_path=str(tmp_path / "t.db"), config_path=str(config_path))
    summary = importer.import_excel(SAMPLE_XTB_XLSX)
    assert summary == transactions_mod.ImportSummary(
        inserted=2,
        skipped=0,
        filtered=1,
        errors=0,
        missing_isins=(),
    )

    with closing(sqlite3.connect(importer.db_path)) as conn:
        rows = conn.execute(
            "SELECT broker, symbol, side, shares, price, fee, tax "
            "FROM transactions ORDER BY transaction_id"
        ).fetchall()

    assert rows == [
        (BROKER_XTB, ISIN_WEBN, "BUY", 7.0, 87.51 / 7.0, None, None),
        (BROKER_XTB, ISIN_WEBN, "SELL", 1.0, 50.0, None, None),
    ]


def test_xtb_shares_and_price_uses_amount(tmp_path):
    row = pd.Series(
        {
            "comment": "OPEN BUY 0.9987/7.9987 @ 12.502",
            "amount": "-12.49",
        }
    )
    shares, price = transactions_mod.xtb_shares_and_price(row)
    assert shares == pytest.approx(0.9987)
    assert price == pytest.approx(12.49 / 0.9987)
    assert shares * price == pytest.approx(12.49)


def test_xtb_shares_and_price_rejects_malformed_amount():
    row = pd.Series({"comment": "OPEN BUY 1/1 @ 12.502", "amount": "not-a-number"})
    with pytest.raises(ValueError, match="amount is not numeric"):
        transactions_mod.xtb_shares_and_price(row)


def test_xtb_filters_unmapped_tickers_without_config(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump({"etfs": {}}))
    summary = XtbImporter(
        db_path=str(tmp_path / "t.db"),
        config_path=str(config_path),
    ).import_excel(SAMPLE_XTB_XLSX)
    assert summary.inserted == 0
    assert summary.filtered == 3
    assert summary.missing_isins == ()
    assert summary.unmapped_tickers == (
        ("WEBN.DE", "Prime All Country World"),
    )


def test_main_xtb_reports_unmapped_tickers(tmp_path, capsys):
    db = tmp_path / "t.db"
    config = tmp_path / "config.yaml"
    config.write_text(yaml.dump({"etfs": {}}))
    code = transactions_mod.main(
        ["xtb", str(SAMPLE_XTB_XLSX), "--db", str(db), "--config", str(config)]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "Tickers not mapped to any ISIN" in out
    assert "WEBN.DE" in out
    assert "e1f config add <ISIN>" in out


def test_main_xtb_success(tmp_path, capsys):
    db = tmp_path / "t.db"
    config = tmp_path / "config.yaml"
    config.write_text(yaml.dump({"etfs": {ISIN_WEBN: {"name": "WEBN", "tickers": ["WEBN"]}}}))
    code = transactions_mod.main(
        ["xtb", str(SAMPLE_XTB_XLSX), "--db", str(db), "--config", str(config)]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "2 inserted" in out
    assert "1 filtered" in out


# ---------------------------------------------------------------------------
# Field parsers — malformed / edge-case broker-export values
# ---------------------------------------------------------------------------


def test_parse_float_handles_finite_and_blank_values():
    assert transactions_mod._parse_float("12.5", "amount") == pytest.approx(12.5)
    assert transactions_mod._parse_float("", "amount") is None
    assert transactions_mod._parse_float(None, "amount") is None
    with pytest.raises(ValueError, match="not numeric"):
        transactions_mod._parse_float("n/a", "amount")
    with pytest.raises(ValueError, match="must be finite"):
        transactions_mod._parse_float("inf", "amount")


def test_format_datetime_timestamp_and_plain_string():
    ts = pd.Timestamp("2024-01-02 09:30:00")
    assert transactions_mod._format_datetime(ts) == "2024-01-02 09:30:00"
    # Non-Timestamp values fall through to _parse_str (trimmed).
    assert transactions_mod._format_datetime("  2024-01-02  ") == "2024-01-02"


def test_parse_xtb_id_variants():
    assert transactions_mod._parse_xtb_id(None) == ""
    assert transactions_mod._parse_xtb_id(float("nan")) == ""
    assert transactions_mod._parse_xtb_id(42.0) == "42"
    assert transactions_mod._parse_xtb_id(float(2**53)) == ""
    assert transactions_mod._parse_xtb_id("") == ""
    assert transactions_mod._parse_xtb_id("100.0") == "100"
    assert transactions_mod._parse_xtb_id("00100") == "00100"
    assert transactions_mod._parse_xtb_id("9007199254740992") == "9007199254740992"
    assert transactions_mod._parse_xtb_id("9007199254740993") == "9007199254740993"
    assert transactions_mod._parse_xtb_id("9007199254740993.0") == "9007199254740993"
    assert transactions_mod._parse_xtb_id("100.5") == "100.5"
    # Non-numeric text is preserved verbatim.
    assert transactions_mod._parse_xtb_id("ORDER-7") == "ORDER-7"


def test_normalize_tr_type_uppercases_and_underscores():
    assert transactions_mod._normalize_tr_type("savings plan") == "SAVINGS_PLAN"


def test_resolve_xtb_ticker_paths():
    mapping = {"WEBN": ISIN_WEBN}
    assert transactions_mod.resolve_xtb_ticker("", mapping) == ""
    # A bare ISIN is passed through untouched.
    assert transactions_mod.resolve_xtb_ticker(ISIN_WEBN, mapping) == ISIN_WEBN
    # Direct hit, then the TICKER.EXCHANGE base-split fallback.
    assert transactions_mod.resolve_xtb_ticker("WEBN", mapping) == ISIN_WEBN
    assert transactions_mod.resolve_xtb_ticker("WEBN.DE", mapping) == ISIN_WEBN
    assert transactions_mod.resolve_xtb_ticker("UNKNOWN", mapping) == ""


def test_parse_xtb_trade_comment_no_match_returns_none():
    assert transactions_mod.parse_xtb_trade_comment("dividend payout") is None


def test_xtb_shares_and_price_without_amount_uses_comment_price():
    row = pd.Series({"comment": "OPEN BUY 2/2 @ 10.0", "amount": ""})
    shares, price = transactions_mod.xtb_shares_and_price(row)
    assert shares == pytest.approx(2.0)
    assert price == pytest.approx(10.0)


def test_xtb_shares_and_price_rejects_non_positive_shares():
    row = pd.Series({"comment": "OPEN BUY 0/0 @ 10.0", "amount": "-5"})
    assert transactions_mod.xtb_shares_and_price(row) is None


def test_xtb_shares_and_price_no_comment_returns_none():
    row = pd.Series({"comment": "not a trade", "amount": "-5"})
    assert transactions_mod.xtb_shares_and_price(row) is None


def test_normalize_xtb_side_buy_sell_and_unknown():
    assert transactions_mod.normalize_xtb_side("Stock purchase") == "BUY"
    assert transactions_mod.normalize_xtb_side("Stock sale") == "SELL"
    assert transactions_mod.normalize_xtb_side("dividend") == ""
