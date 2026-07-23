"""Tests for scripts/import_rakuten_csv.py (保有構成の取り込み)."""

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from import_rakuten_csv import (  # noqa: E402
    build_cash,
    build_holdings,
    diff_holdings,
    main,
)

from tests.data.test_rakuten_csv import SAMPLE  # noqa: E402
from src.data.rakuten_csv import read_asset_balance  # noqa: E402


@pytest.fixture
def csv_file(tmp_path):
    p = tmp_path / "assetbalance(all)_20260723_222831.csv"
    p.write_bytes(SAMPLE.encode("cp932"))
    return p


@pytest.fixture
def csv_data(csv_file):
    return read_asset_balance(csv_file)


EXISTING = [
    {"name": "SOXL (半導体3x)", "quote_symbol": "SOXL", "account": "特定",
     "shares": 275, "cost_price": 29.7495, "currency": "USD",
     "leverage": 3, "category": "米国レバレッジETF"},
    {"name": "iFreeNEXT FANG+", "quote_symbol": None, "account": "NISAつみたて",
     "shares": 144592, "cost_price": 72618.13, "currency": "JPY",
     "unit_divisor": 10000, "note": "積立継続中"},
]


class TestBuildHoldings:
    def test_all_rows_converted(self, csv_data):
        assert len(build_holdings(csv_data, [])) == 4

    def test_account_names_normalized(self, csv_data):
        accounts = {h["account"] for h in build_holdings(csv_data, [])}
        assert "NISA成長" in accounts
        assert "NISAつみたて" in accounts
        assert "NISA成長投資枠" not in accounts

    def test_leverage_is_carried_over(self, csv_data):
        """CSVにレバレッジ倍率は無い。失うと下落シナリオが壊れる。"""
        soxl = next(h for h in build_holdings(csv_data, EXISTING)
                    if h["quote_symbol"] == "SOXL")
        assert soxl["leverage"] == 3
        assert soxl["category"] == "米国レバレッジETF"

    def test_fund_note_survives_name_drift(self, csv_data):
        """楽天の投信名が「〜インデックス」に揺れても note を引き継ぐ。"""
        fund = next(h for h in build_holdings(csv_data, EXISTING)
                    if h["quote_symbol"] is None)
        assert fund["note"] == "積立継続中"
        assert fund["unit_divisor"] == 10000

    def test_fund_price_stored_as_last_known(self, csv_data):
        fund = next(h for h in build_holdings(csv_data, EXISTING)
                    if h["quote_symbol"] is None)
        assert fund["last_known_price"] == 94864

    def test_fund_symbol_is_none_not_empty(self, csv_data):
        """空文字だと価格取得を試みて失敗する。None でスキップさせる。"""
        fund = [h for h in build_holdings(csv_data, []) if not h["quote_symbol"]]
        assert fund[0]["quote_symbol"] is None

    def test_shares_are_ints_when_whole(self, csv_data):
        for h in build_holdings(csv_data, []):
            assert isinstance(h["shares"], int)

    def test_unknown_symbol_gets_no_carried_keys(self, csv_data):
        mdt = [h for h in build_holdings(csv_data, EXISTING)
               if h["quote_symbol"] == "2802.T"]
        assert "leverage" not in mdt[0]


class TestBuildCash:
    def test_converts_cash(self, csv_data):
        assert build_cash(csv_data, []) == [
            {"name": "米ドル", "amount": 63.07, "currency": "USD"}
        ]

    def test_note_carried_by_currency(self, csv_data):
        prior = [{"currency": "USD", "note": "INTC弾"}]
        assert build_cash(csv_data, prior)[0]["note"] == "INTC弾"


class TestDiffHoldings:
    def test_no_change(self, csv_data):
        built = build_holdings(csv_data, [])
        assert diff_holdings(built, built) == []

    def test_detects_new_position(self, csv_data):
        built = build_holdings(csv_data, [])
        lines = diff_holdings(built[1:], built)
        assert any("新規" in line for line in lines)

    def test_detects_removed_position(self, csv_data):
        built = build_holdings(csv_data, [])
        lines = diff_holdings(built, built[1:])
        assert any("消滅" in line for line in lines)

    def test_detects_share_change(self, csv_data):
        built = build_holdings(csv_data, [])
        changed = [dict(h) for h in built]
        changed[0]["shares"] = 999
        lines = diff_holdings(built, changed)
        assert any("数量" in line and "999" in line for line in lines)

    def test_fund_name_drift_is_not_reported_as_replacement(self, csv_data):
        """「FANG+」→「FANG+インデックス」を消滅＋新規と誤報しない。

        積立で数量は実際に増えているので「数量」の差分は出て正しい。
        禁じたいのは同一ポジションが別建てに割れることだけ。
        """
        built = build_holdings(csv_data, EXISTING)
        lines = [line for line in diff_holdings(EXISTING, built)
                 if "iFreeNEXT" in line]
        assert lines, "積立による数量・取得単価の変化は報告されるべき"
        assert all(line.strip().startswith("~") for line in lines)
        assert not any("消滅" in line or "新規" in line for line in lines)


class TestCli:
    def test_writes_target(self, csv_file, tmp_path, capsys):
        target = tmp_path / "holdings.yaml"
        assert main(["--path", str(csv_file), "--target", str(target)]) == 0

        config = yaml.safe_load(target.read_text(encoding="utf-8"))
        assert len(config["holdings"]) == 4
        assert config["cash"][0]["amount"] == 63.07
        assert config["fx"]["fallback_rate"] == pytest.approx(163.67)

    def test_records_provenance(self, csv_file, tmp_path):
        """取り込み元が無いと、レポートが事実と逆の警告を出す。"""
        target = tmp_path / "holdings.yaml"
        main(["--path", str(csv_file), "--target", str(target)])

        source = yaml.safe_load(target.read_text(encoding="utf-8"))["source"]
        assert source["type"] == "rakuten_csv"
        assert source["file"] == csv_file.name
        assert source["exported_at"]

    def test_dry_run_does_not_write(self, csv_file, tmp_path):
        target = tmp_path / "holdings.yaml"
        assert main(["--path", str(csv_file), "--target", str(target),
                     "--dry-run"]) == 0
        assert not target.exists()

    def test_preserves_metadata_across_reimport(self, csv_file, tmp_path):
        target = tmp_path / "holdings.yaml"
        target.write_text(
            yaml.safe_dump({"holdings": EXISTING}, allow_unicode=True),
            encoding="utf-8",
        )
        main(["--path", str(csv_file), "--target", str(target)])

        config = yaml.safe_load(target.read_text(encoding="utf-8"))
        soxl = next(h for h in config["holdings"] if h["quote_symbol"] == "SOXL")
        assert soxl["leverage"] == 3

    def test_reimport_is_idempotent(self, csv_file, tmp_path):
        target = tmp_path / "holdings.yaml"
        main(["--path", str(csv_file), "--target", str(target)])
        first = yaml.safe_load(target.read_text(encoding="utf-8"))["holdings"]
        main(["--path", str(csv_file), "--target", str(target)])
        second = yaml.safe_load(target.read_text(encoding="utf-8"))["holdings"]
        assert first == second

    def test_missing_csv_reports_error(self, tmp_path, capsys):
        assert main(["--path", str(tmp_path / "nope.csv"),
                     "--target", str(tmp_path / "h.yaml")]) == 1
        assert "見つかりません" in capsys.readouterr().out

    def test_no_csv_found_gives_instructions(self, tmp_path, monkeypatch, capsys):
        import import_rakuten_csv as mod

        monkeypatch.setattr(mod, "find_latest_csv", lambda: None)
        assert main(["--target", str(tmp_path / "h.yaml")]) == 1
        assert "保有商品一覧" in capsys.readouterr().out

    def test_generated_yaml_is_loadable_by_weekly(self, csv_file, tmp_path):
        """週次レポートのローダーが読める形であること。"""
        from src.core.portfolio.weekly import load_holdings_config

        target = tmp_path / "holdings.yaml"
        main(["--path", str(csv_file), "--target", str(target)])
        config = load_holdings_config(str(target))
        assert len(config["holdings"]) == 4
