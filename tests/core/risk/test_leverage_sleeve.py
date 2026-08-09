"""3xスリーブの実体分析のテスト.

## 縛っていること

1. **同じ量を2箇所で計算しない。** ボラドラッグとσ前提は既存実装に委譲する。
   実装中に (L·σ)²/2 で 55.1% を出したが、既存は L(L-1)σ²/2 で 36.7% だった。
   **同一システム内で数字が食い違うのが最悪。**
2. **3本を「3つのポジション」と数えない。** 重複を明示する。
3. **感応度は金額で出す。** 「実効20%」では大きさが伝わらない。
"""

from __future__ import annotations

import pytest

from src.core.risk import leverage_sleeve as LS

HOLDINGS = [
    {"symbol": "SOXL", "name": "半導体3x", "leverage": 3,
     "value_jpy": 6_084_027, "weight_pct": 27.5},
    {"symbol": "TECL", "name": "テック3x", "leverage": 3,
     "value_jpy": 5_141_313, "weight_pct": 23.2},
    {"symbol": "TQQQ", "name": "ナス3x", "leverage": 3,
     "value_jpy": 2_513_916, "weight_pct": 11.3},
    {"symbol": "MDT", "name": "メドトロニック", "value_jpy": 1_374_905,
     "weight_pct": 6.2},
]

LOOKTHROUGH = {
    "resolved_etfs": [
        {"symbol": "SOXL", "lookup": "SOXX", "underlying": "半導体指数"},
        {"symbol": "TECL", "lookup": "XLK", "underlying": "米テック"},
        {"symbol": "TQQQ", "lookup": "QQQ", "underlying": "ナスダック100"},
    ],
    "effective": [
        {"symbol": "NVDA", "effective_pct": 20.18,
         "sources": ["SOXL", "TECL", "TQQQ"]},
        {"symbol": "AAPL", "effective_pct": 12.20, "sources": ["TECL", "TQQQ"]},
        {"symbol": "MDT", "effective_pct": 6.21, "sources": ["MDT"]},
    ],
}

TOTAL = 22_157_222.0


class TestDragConsistency:
    """**既存実装と同じ数字を出す。** 二重実装しない。"""

    @pytest.mark.parametrize("sigma,expected", [
        (0.35, 36.75),   # SOXL — rules/weekly-report.md の表と一致
        (0.26, 20.28),   # TECL
        (0.22, 14.52),   # TQQQ
    ])
    def test_matches_projection_module(self, sigma, expected):
        assert LS.volatility_drag(3, sigma) * 100 == pytest.approx(expected, abs=0.1)

    def test_uses_excess_drag_not_total_variance(self):
        """L(L-1)σ²/2 であって (Lσ)²/2 ではない。

        後者は 55.1% になり、rules の表（36.7%）と食い違う。
        """
        total_variance = (3 * 0.35) ** 2 / 2
        assert LS.volatility_drag(3, 0.35) != pytest.approx(total_variance)
        assert LS.volatility_drag(3, 0.35) == pytest.approx(3 * 2 * 0.35 ** 2 / 2)

    def test_no_drag_for_unleveraged(self):
        assert LS.volatility_drag(1, 0.35) == 0.0

    def test_sigma_comes_from_the_single_source(self):
        """σ前提も再定義しない。weekly.py の表を引く。"""
        from src.core.portfolio.weekly import UNDERLYING_ASSUMPTIONS

        for sym in ("SOXL", "TECL", "TQQQ"):
            expected = UNDERLYING_ASSUMPTIONS[sym]["annual_vol_pct"] / 100.0
            assert LS._sigma_for(sym, None) == pytest.approx(expected)

    def test_unknown_symbol_has_no_sigma(self):
        assert LS._sigma_for("UNKNOWN", None) is None


class TestSleeveAnalysis:
    def test_effective_leverage_is_computed(self):
        r = LS.analyze_sleeve(HOLDINGS, LOOKTHROUGH, TOTAL)
        # 想定元本 = 各評価額 × 倍率
        assert r["notional_jpy"] == pytest.approx(
            (6_084_027 + 5_141_313 + 2_513_916) * 3, rel=0.001)
        assert r["effective_leverage"] == pytest.approx(
            r["notional_jpy"] / TOTAL, abs=0.01)

    def test_drag_is_reported_per_etf(self):
        r = LS.analyze_sleeve(HOLDINGS, LOOKTHROUGH, TOTAL)
        soxl = next(e for e in r["etfs"] if e["symbol"] == "SOXL")
        assert soxl["drag_pct"] == pytest.approx(36.75, abs=0.1)
        assert soxl["effective_sigma"] == pytest.approx(1.05, abs=0.01)
        assert "二重計上" in soxl["drag_note"]

    def test_unleveraged_holdings_are_excluded(self):
        r = LS.analyze_sleeve(HOLDINGS, LOOKTHROUGH, TOTAL)
        assert {e["symbol"] for e in r["etfs"]} == {"SOXL", "TECL", "TQQQ"}

    def test_missing_sigma_is_admitted_not_zero(self):
        holdings = [{"symbol": "XXXL", "leverage": 3, "value_jpy": 1_000_000}]
        r = LS.analyze_sleeve(holdings, {"resolved_etfs": [], "effective": []}, TOTAL)
        e = r["etfs"][0]
        assert e["drag_pct"] is None
        assert "0ではありません" in e["drag_note"]

    def test_no_leveraged_holdings(self):
        r = LS.analyze_sleeve([{"symbol": "MDT", "value_jpy": 100}], None, TOTAL)
        assert r["available"] is False


class TestOverlap:
    def test_shared_names_are_detected(self):
        r = LS.analyze_sleeve(HOLDINGS, LOOKTHROUGH, TOTAL)
        ov = r["overlap"]
        shared = {s["symbol"]: s for s in ov["shared_names"]}
        assert shared["NVDA"]["count"] == 3
        assert shared["AAPL"]["count"] == 2
        # MDT は直接保有のみでレバETFに入っていない
        assert "MDT" not in shared

    def test_overlap_message_warns_against_counting_three_positions(self):
        r = LS.analyze_sleeve(HOLDINGS, LOOKTHROUGH, TOTAL)
        assert "集中を過小評価" in r["overlap"]["message"]

    def test_shared_effective_pct_is_summed(self):
        r = LS.analyze_sleeve(HOLDINGS, LOOKTHROUGH, TOTAL)
        assert r["overlap"]["shared_effective_pct"] == pytest.approx(32.38, abs=0.01)

    def test_missing_lookthrough_is_admitted(self):
        r = LS.analyze_sleeve(HOLDINGS, {"resolved_etfs": []}, TOTAL)
        assert r["overlap"]["available"] is False
        assert "測れません" in r["overlap"]["reason"]


class TestSensitivity:
    def test_impact_is_expressed_in_yen(self):
        """「実効20%」では大きさが伝わらない。金額で出す。"""
        r = LS.analyze_sleeve(HOLDINGS, LOOKTHROUGH, TOTAL)
        nvda = next(s for s in r["sensitivity"] if s["symbol"] == "NVDA")
        minus10 = next(i for i in nvda["impacts"] if i["shock_pct"] == -10.0)
        assert minus10["pf_change_jpy"] == pytest.approx(
            -TOTAL * 0.2018 * 0.10, rel=0.01)
        assert minus10["pf_change_pct"] == pytest.approx(-2.018, abs=0.01)

    def test_all_shocks_present(self):
        r = LS.analyze_sleeve(HOLDINGS, LOOKTHROUGH, TOTAL)
        shocks = {i["shock_pct"] for i in r["sensitivity"][0]["impacts"]}
        assert shocks == {-10.0, -20.0, -35.0}


class TestFormatting:
    def test_table_has_drag_and_overlap_and_sensitivity(self):
        text = LS.format_sleeve(LS.analyze_sleeve(HOLDINGS, LOOKTHROUGH, TOTAL))
        assert "年率ドラッグ" in text
        # 36.75 は表示上 36.8 に丸まる。**数値の一致は数値で確認する**
        # （文字列で照合すると丸めでテストが壊れ、実装が正しいのに赤くなる）
        assert "36.8%" in text
        assert "3本を別ポジションと数えない" in text
        assert "単一銘柄が動いたときの PF への影響" in text
        assert "NVDA" in text

    def test_unavailable_is_explained(self):
        assert "分析できませんでした" in LS.format_sleeve(None)
