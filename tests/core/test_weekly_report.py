"""週次レポート（予測・楽天RSS読み取り・整形）のテスト."""

import math
from pathlib import Path

import pytest

from src.core.portfolio.projection import (
    drawdown_scenario,
    project_portfolio,
    project_value,
    volatility_drag,
)
from src.data.rakuten_rss import (
    SnapshotUnavailable,
    merge_with_fallback,
    normalize_code,
    read_snapshot,
    snapshot_freshness,
)


# ---------------------------------------------------------------------------
# 推移予測
# ---------------------------------------------------------------------------


class TestVolatilityDrag:
    def test_unleveraged_has_no_drag(self):
        assert volatility_drag(40.0, leverage=1) == 0.0

    def test_3x_drag_grows_with_volatility(self):
        assert volatility_drag(40.0, 3) > volatility_drag(20.0, 3)

    def test_3x_at_40pct_vol_is_material(self):
        """σ=40%, L=3 → 3*2*0.16/2 = 48%/年。3xを『3倍儲かる』と扱わせない。"""
        assert volatility_drag(40.0, 3) == pytest.approx(48.0)

    def test_2x_drag_is_smaller_than_3x(self):
        assert volatility_drag(40.0, 2) < volatility_drag(40.0, 3)


class TestProjectValue:
    def test_range_is_ordered(self):
        p = project_value(1_000_000, 10.0, 20.0, 252)
        assert p["low"] < p["mid"] < p["high"]

    def test_longer_horizon_widens_the_range(self):
        short = project_value(1_000_000, 10.0, 20.0, 21)
        long = project_value(1_000_000, 10.0, 20.0, 252)
        assert (long["high"] - long["low"]) > (short["high"] - short["low"])

    def test_zero_value_stays_zero(self):
        p = project_value(0.0, 10.0, 20.0, 252)
        assert p["mid"] == 0.0

    def test_zero_days_returns_current(self):
        p = project_value(1_000_000, 10.0, 20.0, 0)
        assert p["mid"] == 1_000_000

    def test_leverage_widens_the_range(self):
        plain = project_value(1_000_000, 12.0, 25.0, 252, leverage=1)
        levered = project_value(1_000_000, 12.0, 25.0, 252, leverage=3)
        assert (levered["high"] - levered["low"]) > (plain["high"] - plain["low"])
        assert levered["effective_vol_pct"] == pytest.approx(75.0)
        assert levered["drag_pct"] > 0

    def test_high_underlying_vol_kills_leveraged_median(self):
        """σ=35%の原資産に3xを掛けると、μ=14%でも中央値は元本割れ方向に傾く。

        L·μ − (L·σ)²/2 = 0.42 − 0.551 = −0.13/年。
        『3xは3倍儲かる』という素朴な期待を数式が否定する。
        """
        result = project_value(1_000_000, 14.0, 35.0, 252, leverage=3)
        assert result["mid"] < 1_000_000

    def test_low_underlying_vol_keeps_leveraged_median_positive(self):
        """同じ3xでも原資産σ=22%なら中央値はプラス。3xを一括りにできない。"""
        result = project_value(1_000_000, 12.0, 22.0, 252, leverage=3)
        assert result["mid"] > 1_000_000

    def test_drag_is_reported_but_not_double_counted(self):
        """drag_pct は表示用。ドリフト計算から重ねて引いてはいけない。"""
        import math

        result = project_value(1_000_000, 12.0, 22.0, 252, leverage=3)
        sigma_eff = 3 * 0.22
        expected = 1_000_000 * math.exp(3 * 0.12 - sigma_eff ** 2 / 2)
        assert result["mid"] == pytest.approx(expected, rel=1e-9)


class TestProjectPortfolio:
    def _positions(self):
        return [
            {"name": "A", "value": 1_000_000, "annual_return_pct": 10, "annual_vol_pct": 20},
            {"name": "B", "value": 2_000_000, "annual_return_pct": 5, "annual_vol_pct": 15},
        ]

    def test_all_horizons_present(self):
        result = project_portfolio(self._positions(), cash_value=500_000)
        for key in ("short", "mid", "long"):
            assert key in result
            assert result[key]["low"] < result[key]["high"]

    def test_current_total_includes_cash(self):
        result = project_portfolio(self._positions(), cash_value=500_000)
        assert result["current_total"] == 3_500_000

    def test_cash_is_not_projected_to_grow(self):
        """現金だけのPFは(積立なしなら)中央値が動かない。"""
        result = project_portfolio([], cash_value=1_000_000)
        assert result["short"]["mid"] == pytest.approx(1_000_000)

    def test_contributions_increase_the_projection(self):
        without = project_portfolio(self._positions(), monthly_contribution=0)
        with_dca = project_portfolio(self._positions(), monthly_contribution=50_000)
        assert with_dca["long"]["mid"] > without["long"]["mid"]

    def test_empty_portfolio_does_not_crash(self):
        result = project_portfolio([], cash_value=0)
        assert result["current_total"] == 0
        assert result["short"]["change_pct"] == 0.0


class TestDrawdownScenario:
    def test_3x_amplifies_the_drop(self):
        positions = [{"name": "SOXL", "value": 1_000_000, "leverage": 3}]
        result = drawdown_scenario(positions, -20.0)
        assert result["after"] == pytest.approx(400_000)
        assert result["change_pct"] == pytest.approx(-60.0)

    def test_cash_cushions_the_portfolio(self):
        positions = [{"name": "SOXL", "value": 1_000_000, "leverage": 3}]
        result = drawdown_scenario(positions, -20.0, cash_value=1_000_000)
        assert result["change_pct"] == pytest.approx(-30.0)

    def test_value_never_goes_negative(self):
        """3xで原資産-50%は理論上-150%。ETFは0で止まる。"""
        positions = [{"name": "SOXL", "value": 1_000_000, "leverage": 3}]
        assert drawdown_scenario(positions, -50.0)["after"] == 0.0

    def test_unleveraged_matches_the_drop(self):
        positions = [{"name": "X", "value": 1_000_000, "leverage": 1}]
        assert drawdown_scenario(positions, -20.0)["change_pct"] == pytest.approx(-20.0)


# ---------------------------------------------------------------------------
# 楽天 RSS スナップショット
# ---------------------------------------------------------------------------


class TestNormalizeCode:
    def test_four_digit_becomes_tokyo_ticker(self):
        assert normalize_code("2802") == "2802.T"

    def test_existing_suffix_is_untouched(self):
        assert normalize_code("2802.T") == "2802.T"

    def test_us_ticker_is_untouched(self):
        assert normalize_code("SOXL") == "SOXL"

    def test_empty_returns_empty(self):
        assert normalize_code(None) == ""
        assert normalize_code("  ") == ""


def _write_csv(tmp_path: Path, rows: list[str]) -> Path:
    p = tmp_path / "保有.csv"
    p.write_text("\n".join(rows), encoding="utf-8-sig")
    return p


class TestReadSnapshot:
    def test_reads_a_basic_csv(self, tmp_path):
        path = _write_csv(tmp_path, [
            "銘柄名,コード,口座,数量,取得単価,現在値,通貨",
            "味の素,2802,特定,400,3906.03,5158,JPY",
            "SOXL,SOXL,特定,275,29.7495,,USD",
        ])
        snap = read_snapshot(path)
        assert snap["row_count"] == 2
        assert snap["holdings"][0]["quote_symbol"] == "2802.T"
        assert snap["holdings"][0]["shares"] == 400
        assert snap["holdings"][1]["quote_symbol"] == "SOXL"

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(SnapshotUnavailable, match="見つかりません"):
            read_snapshot(tmp_path / "nope.csv")

    def test_unsupported_extension_raises(self, tmp_path):
        p = tmp_path / "x.txt"
        p.write_text("hi", encoding="utf-8")
        with pytest.raises(SnapshotUnavailable, match="未対応"):
            read_snapshot(p)

    def test_missing_header_raises(self, tmp_path):
        path = _write_csv(tmp_path, ["適当,データ", "1,2"])
        with pytest.raises(SnapshotUnavailable, match="見出し行"):
            read_snapshot(path)

    def test_empty_body_raises(self, tmp_path):
        path = _write_csv(tmp_path, ["銘柄名,コード,数量,取得単価"])
        with pytest.raises(SnapshotUnavailable, match="0件"):
            read_snapshot(path)

    def test_header_aliases_are_accepted(self, tmp_path):
        path = _write_csv(tmp_path, [
            "銘柄,ティッカー,保有数,平均取得単価,時価",
            "味の素,2802,400,3906.03,5158",
        ])
        assert read_snapshot(path)["holdings"][0]["shares"] == 400

    def test_numbers_with_commas_and_symbols(self, tmp_path):
        path = _write_csv(tmp_path, [
            "銘柄名,コード,数量,取得単価,現在値",
            "味の素,2802,\"1,400\",\"¥3,906\",\"5,158\"",
        ])
        h = read_snapshot(path)["holdings"][0]
        assert h["shares"] == 1400
        assert h["price"] == 5158

    def test_rows_without_shares_are_skipped(self, tmp_path):
        path = _write_csv(tmp_path, [
            "銘柄名,コード,数量,取得単価,現在値",
            "味の素,2802,400,3906.03,5158",
            "合計,,,,",
        ])
        assert read_snapshot(path)["row_count"] == 1

    def test_currency_is_inferred_when_absent(self, tmp_path):
        path = _write_csv(tmp_path, [
            "銘柄名,コード,数量,取得単価,現在値",
            "味の素,2802,400,3906.03,5158",
            "SOXL,SOXL,275,29.75,234",
        ])
        holdings = read_snapshot(path)["holdings"]
        assert holdings[0]["currency"] == "JPY"
        assert holdings[1]["currency"] == "USD"

    def test_snapshot_reports_its_age(self, tmp_path):
        path = _write_csv(tmp_path, [
            "銘柄名,コード,数量,取得単価,現在値",
            "味の素,2802,400,3906.03,5158",
        ])
        snap = read_snapshot(path)
        assert snap["age_hours"] < 1.0
        assert snap["modified_at"]


class TestFreshness:
    def test_recent_snapshot_is_fresh(self):
        result = snapshot_freshness({"age_hours": 12.0}, max_age_hours=72)
        assert result["fresh"] is True

    def test_stale_snapshot_warns(self):
        """古いファイルを黙って使う事故を防ぐ。"""
        result = snapshot_freshness({"age_hours": 200.0}, max_age_hours=72)
        assert result["fresh"] is False
        assert "⚠️" in result["message"]


class TestMergeWithFallback:
    def test_rss_price_wins(self):
        merged = merge_with_fallback(
            [{"quote_symbol": "2802.T", "price": 5158}], {"2802.T": 9999}
        )
        assert merged[0]["price"] == 5158
        assert merged[0]["price_source"] == "rakuten-rss"

    def test_fallback_fills_missing_price(self):
        """MS2 RSS が米国株の現在値を返さなくても株価が埋まる。"""
        merged = merge_with_fallback(
            [{"quote_symbol": "SOXL", "price": None}], {"SOXL": 234.68}
        )
        assert merged[0]["price"] == 234.68
        assert merged[0]["price_source"] == "fallback"

    def test_missing_everywhere_is_marked(self):
        merged = merge_with_fallback([{"quote_symbol": "XYZ", "price": None}], {})
        assert merged[0]["price_source"] == "missing"

    def test_original_records_are_not_mutated(self):
        original = {"quote_symbol": "SOXL", "price": None}
        merge_with_fallback([original], {"SOXL": 234.68})
        assert original["price"] is None
