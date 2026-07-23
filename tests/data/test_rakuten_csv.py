"""Tests for src/data/rakuten_csv.py (楽天証券 保有商品一覧CSV)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.data.rakuten_csv import (
    SnapshotUnavailable,
    _to_number,
    find_latest_csv,
    read_asset_balance,
    to_quote_symbol,
)

# 実ファイル(assetbalance(all)_*.csv)から起こした最小再現。Shift-JIS で書く。
SAMPLE = '''"■資産合計欄"
"","時価評価額[円]","前日比[円]"
"資産合計","23,474,888","+27,380"

"■ 保有商品詳細 (すべて）"

"種別","銘柄コード・ティッカー","銘柄","口座","保有数量","［単位］","平均取得価額","［単位］","現在値","［単位］","現在値(更新日)","(参考為替)","前日比","［単位］","時価評価額[円]","時価評価額[外貨]","評価損益[円]","評価損益[％]"
"国内株式","2802","味の素","特定","400","株","3,906.03","円","5,102.0","円","","","-151.0","円","2,040,800","-","+478,385","+30.61"
"国内株式","2802","味の素","NISA成長投資枠","39","株","4,813.00","円","5,102.0","円","","","-151.0","円","198,978","-","+11,271","+6.00"
"米国株式","SOXL","Direxion デイリー 半導体株 ブル 3倍 ETF","特定","275","株","29.7495","USD","160.9900","USD","","","0.0000","USD","7,246,039","44,272.25 USD","+6,062,259","+512.11"
"投資信託","","iFreeNEXT FANG+インデックス","NISAつみたて投資枠","149,927","口","73,369.04","円","94,864","円","","","-1,056","円","1,422,267","-","+322,267","+29.29"
"外貨預り金","","米ドル","-","63.07","USD","-","-","163.67","円/USD","","","-","-","10,322.00","-","-","-"


"■参考為替レート"

"米ドル","163.67","円/USD","(07/23  21:59)"
"ユーロ","186.34","円/EUR","(07/23  21:59)"
'''


@pytest.fixture
def csv_file(tmp_path):
    p = tmp_path / "assetbalance(all)_20260723_222831.csv"
    p.write_bytes(SAMPLE.encode("cp932"))
    return p


class TestToNumber:
    @pytest.mark.parametrize("raw,expected", [
        ("3,906.03", 3906.03),
        ("+96,000", 96000.0),
        ("-375,015", -375015.0),
        ("149,927", 149927.0),
        ("44,272.25 USD", 44272.25),
    ])
    def test_parses(self, raw, expected):
        assert _to_number(raw) == pytest.approx(expected)

    @pytest.mark.parametrize("raw", ["", "-", "―", "未取得", None, "株"])
    def test_non_numeric_is_none(self, raw):
        """取れなかった値を 0 にすると「残高ゼロ」と誤読される。"""
        assert _to_number(raw) is None


class TestToQuoteSymbol:
    def test_domestic_gets_t_suffix(self):
        assert to_quote_symbol("2802", "国内株式") == "2802.T"

    def test_us_passthrough(self):
        assert to_quote_symbol("SOXL", "米国株式") == "SOXL"

    def test_fund_without_code_is_blank(self):
        assert to_quote_symbol("", "投資信託") == ""

    def test_china_gets_hk_suffix_padded(self):
        assert to_quote_symbol("700", "中国株式") == "0700.HK"


class TestReadAssetBalance:
    def test_parses_all_holdings(self, csv_file):
        data = read_asset_balance(csv_file)
        assert data["row_count"] == 4
        assert [h["quote_symbol"] for h in data["holdings"]] == [
            "2802.T", "2802.T", "SOXL", ""
        ]

    def test_cash_is_separated_from_holdings(self, csv_file):
        """外貨預り金は保有ではなく現金。混ぜると評価額が二重計上になる。"""
        data = read_asset_balance(csv_file)
        assert all(h["asset_type"] != "外貨預り金" for h in data["holdings"])
        assert data["cash"] == [
            {"name": "米ドル", "amount": 63.07, "currency": "USD",
             "asset_type": "外貨預り金"}
        ]

    def test_same_symbol_different_account_kept_separate(self, csv_file):
        ajinomoto = [h for h in read_asset_balance(csv_file)["holdings"]
                     if h["quote_symbol"] == "2802.T"]
        assert [h["account"] for h in ajinomoto] == ["特定", "NISA成長投資枠"]
        assert [h["shares"] for h in ajinomoto] == [400, 39]

    def test_currency_from_unit_column(self, csv_file):
        by_sym = {h["quote_symbol"]: h for h in read_asset_balance(csv_file)["holdings"]}
        assert by_sym["2802.T"]["currency"] == "JPY"
        assert by_sym["SOXL"]["currency"] == "USD"

    def test_cost_and_price_parsed(self, csv_file):
        soxl = next(h for h in read_asset_balance(csv_file)["holdings"]
                    if h["quote_symbol"] == "SOXL")
        assert soxl["cost_price"] == pytest.approx(29.7495)
        assert soxl["price"] == pytest.approx(160.99)
        assert soxl["shares"] == 275

    def test_fx_rates_parsed(self, csv_file):
        fx = read_asset_balance(csv_file)["fx"]
        assert fx["USD"] == pytest.approx(163.67)
        assert fx["EUR"] == pytest.approx(186.34)

    def test_metadata_present(self, csv_file):
        data = read_asset_balance(csv_file)
        assert data["source_path"] == str(csv_file)
        assert data["age_hours"] >= 0
        assert "modified_at" in data

    def test_detail_section_stops_at_blank_line(self, csv_file):
        """明細の後の為替セクションを保有として拾わない。"""
        names = [h["name"] for h in read_asset_balance(csv_file)["holdings"]]
        assert "ユーロ" not in names

    def test_utf8_file_also_readable(self, tmp_path):
        p = tmp_path / "utf8.csv"
        p.write_text(SAMPLE, encoding="utf-8")
        assert read_asset_balance(p)["row_count"] == 4


class TestReadAssetBalanceErrors:
    def test_missing_file(self, tmp_path):
        with pytest.raises(SnapshotUnavailable, match="見つかりません"):
            read_asset_balance(tmp_path / "nope.csv")

    def test_wrong_csv_without_detail_header(self, tmp_path):
        p = tmp_path / "other.csv"
        p.write_bytes('"日付","約定"\n"2026-07-23","100"\n'.encode("cp932"))
        with pytest.raises(SnapshotUnavailable, match="見出し行"):
            read_asset_balance(p)

    def test_detail_header_but_no_rows(self, tmp_path):
        p = tmp_path / "empty.csv"
        content = ('"種別","銘柄コード・ティッカー","銘柄","口座","保有数量"\n'
                   '\n')
        p.write_bytes(content.encode("cp932"))
        with pytest.raises(SnapshotUnavailable, match="0件"):
            read_asset_balance(p)


class TestFindLatestCsv:
    def test_picks_newest(self, tmp_path):
        import os
        import time

        old = tmp_path / "assetbalance(all)_1.csv"
        new = tmp_path / "assetbalance(all)_2.csv"
        old.write_text("x", encoding="utf-8")
        time.sleep(0.01)
        new.write_text("x", encoding="utf-8")
        os.utime(old, (1, 1))
        assert find_latest_csv([tmp_path]) == new

    def test_none_when_absent(self, tmp_path):
        assert find_latest_csv([tmp_path]) is None

    def test_ignores_missing_directories(self, tmp_path):
        assert find_latest_csv([tmp_path / "nope"]) is None

    def test_ignores_unrelated_csv(self, tmp_path):
        (tmp_path / "trades.csv").write_text("x", encoding="utf-8")
        assert find_latest_csv([tmp_path]) is None
