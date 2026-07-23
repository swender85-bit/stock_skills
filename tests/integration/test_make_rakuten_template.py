"""Tests for scripts/make_rakuten_template.py (楽天 MS2 RSS テンプレート生成)."""

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

openpyxl = pytest.importorskip("openpyxl")

from make_rakuten_template import (  # noqa: E402
    HEADERS,
    _rss_code,
    build_workbook,
    main,
)

from src.data.rakuten_rss import read_snapshot  # noqa: E402


HOLDINGS = [
    {"name": "SOXL (半導体3x)", "quote_symbol": "SOXL", "account": "特定",
     "shares": 275, "cost_price": 29.7495, "currency": "USD"},
    {"name": "味の素", "quote_symbol": "2802.T", "account": "特定",
     "shares": 400, "cost_price": 3906.03, "currency": "JPY"},
    {"name": "味の素", "quote_symbol": "2802.T", "account": "NISA成長",
     "shares": 39, "cost_price": 4813.00, "currency": "JPY"},
    {"name": "iFreeNEXT FANG+", "quote_symbol": None, "account": "NISAつみたて",
     "shares": 149927, "cost_price": 73369.04, "currency": "JPY"},
]


class TestRssCode:
    def test_domestic_stock_strips_suffix(self):
        assert _rss_code({"quote_symbol": "2802.T", "currency": "JPY"}) == "2802"

    def test_us_stock_returns_none(self):
        """MS2 RSS は国内株が対象。米国株に RssMarket を書いても引けない。"""
        assert _rss_code({"quote_symbol": "SOXL", "currency": "USD"}) is None

    def test_fund_without_symbol_returns_none(self):
        assert _rss_code({"quote_symbol": None, "currency": "JPY"}) is None

    def test_jpy_without_t_suffix_returns_none(self):
        assert _rss_code({"quote_symbol": "FOO", "currency": "JPY"}) is None


class TestBuildWorkbook:
    @pytest.fixture
    def wb(self):
        return build_workbook(HOLDINGS)

    def test_sheet_named_hoyuu_is_first(self, wb):
        """リーダーは「保有」シートを優先して探す。"""
        assert wb.sheetnames[0] == "保有"

    def test_headers_match_reader_expectations(self, wb):
        row = [c.value for c in wb["保有"][1]]
        assert row == HEADERS

    def test_domestic_rows_get_rss_formula(self, wb):
        ws = wb["保有"]
        assert ws.cell(row=3, column=6).value == '=RssMarket("2802","現在値")'

    def test_foreign_rows_left_blank_for_yfinance(self, wb):
        ws = wb["保有"]
        assert ws.cell(row=2, column=6).value is None

    def test_quantities_and_costs_written(self, wb):
        ws = wb["保有"]
        assert ws.cell(row=2, column=4).value == 275
        assert ws.cell(row=2, column=5).value == 29.7495

    def test_domestic_position_sheet_exists(self, wb):
        assert "国内RSS" in wb.sheetnames

    def test_position_list_formula(self, wb):
        assert wb["国内RSS"]["A3"].value == '=RssPositionList($A$2:$F$2,,"A")'

    def test_position_list_headers(self, wb):
        row = [c.value for c in wb["国内RSS"][2]][:6]
        assert row == ["銘柄コード", "銘柄名称", "口座区分",
                       "保有数量", "平均取得価額", "時価"]


class TestReaderRoundTrip:
    """生成したブックを実際のリーダーが読めること（これが本番の契約）。"""

    def test_reader_parses_generated_file(self, tmp_path):
        out = tmp_path / "楽天保有.xlsx"
        build_workbook(HOLDINGS).save(out)

        snap = read_snapshot(out)
        assert len(snap["holdings"]) == len(HOLDINGS)

        first = snap["holdings"][0]
        assert first["name"] == "SOXL (半導体3x)"
        assert first["shares"] == 275
        assert first["cost_price"] == pytest.approx(29.7495)

    def test_extra_sheet_does_not_confuse_reader(self, tmp_path):
        """「国内RSS」シートを足しても「保有」シートが読まれる。"""
        out = tmp_path / "b.xlsx"
        build_workbook(HOLDINGS).save(out)
        codes = [h["code"] for h in read_snapshot(out)["holdings"]]
        assert "SOXL" in codes

    def test_unsaved_formulas_read_as_none(self, tmp_path):
        """Excel で開いて保存する前は現在値が None（数式は読まない）。

        これが「開いて保存が必須」の理由。値があると誤認させない。
        """
        out = tmp_path / "c.xlsx"
        build_workbook(HOLDINGS).save(out)
        prices = [h["price"] for h in read_snapshot(out)["holdings"]]
        assert all(p is None for p in prices)


class TestCli:
    def _write_holdings(self, tmp_path):
        p = tmp_path / "holdings.yaml"
        p.write_text(yaml.safe_dump({"holdings": HOLDINGS}, allow_unicode=True),
                     encoding="utf-8")
        return p

    def test_generates_file(self, tmp_path, monkeypatch, capsys):
        src = self._write_holdings(tmp_path)
        out = tmp_path / "out.xlsx"
        monkeypatch.setattr(sys, "argv",
                            ["prog", "--holdings", str(src), "--output", str(out)])
        assert main() == 0
        assert out.exists()

    def test_refuses_to_overwrite_without_force(self, tmp_path, monkeypatch):
        """RSS 関数を手編集した内容を黙って壊さない。"""
        src = self._write_holdings(tmp_path)
        out = tmp_path / "out.xlsx"
        out.write_text("existing", encoding="utf-8")
        monkeypatch.setattr(sys, "argv",
                            ["prog", "--holdings", str(src), "--output", str(out)])
        assert main() == 1
        assert out.read_text(encoding="utf-8") == "existing"

    def test_force_overwrites(self, tmp_path, monkeypatch):
        src = self._write_holdings(tmp_path)
        out = tmp_path / "out.xlsx"
        out.write_text("existing", encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["prog", "--holdings", str(src),
                                          "--output", str(out), "--force"])
        assert main() == 0
        assert read_snapshot(out)["row_count"] == len(HOLDINGS)

    def test_missing_holdings_file_errors(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "argv", [
            "prog", "--holdings", str(tmp_path / "nope.yaml"),
            "--output", str(tmp_path / "o.xlsx")])
        assert main() == 1

    def test_empty_holdings_errors(self, tmp_path, monkeypatch):
        src = tmp_path / "empty.yaml"
        src.write_text("holdings: []\n", encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["prog", "--holdings", str(src),
                                          "--output", str(tmp_path / "o.xlsx")])
        assert main() == 1

    def test_real_config_is_usable(self, tmp_path, monkeypatch):
        """リポジトリの実 config で生成できること（形式ドリフト検出）。"""
        out = tmp_path / "real.xlsx"
        monkeypatch.setattr(sys, "argv", ["prog", "--output", str(out)])
        assert main() == 0
        assert read_snapshot(out)["row_count"] > 0
