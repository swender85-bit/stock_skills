"""監視計画の自動導出テスト.

## 名指しする問題

何を取りに行くかが**手書きの静的設定**に固定されていた。
2737.T を 8/7 に売却したのに `config/competitors.yaml` に残り、
**売った銘柄の競合を追い続けていた**。

さらに実効エクスポージャー表に INTC が並び、
「持っていない銘柄がポートフォリオ扱い」と読まれた。
データ上は `direct_pct: 0.0 / via_etf_pct: 6.34` で正しいが、
**保有と ETF経由の曝露を同じ見た目で並べたのが誤り**だった。

## 縛っていること

1. **保有が変われば監視対象も変わる**（設定に固定しない）
2. **売却済みの設定を検出して名指しする**
3. **直接保有と ETF経由のみを混ぜない**
"""

from __future__ import annotations

import pytest

from src.core.research import watch_plan as WP

HOLDINGS = [
    {"symbol": "SOXL", "name": "半導体3x", "weight_pct": 27.5, "leverage": 3},
    {"symbol": "QCOM", "name": "クアルコム", "weight_pct": 10.2},
    {"symbol": "2802.T", "name": "味の素", "weight_pct": 9.4},
    {"symbol": None, "name": "iFreeNEXT FANG+インデックス", "weight_pct": 6.9},
]

LOOKTHROUGH = {
    "resolved_etfs": [{"symbol": "SOXL", "lookup": "SOXX",
                       "underlying": "半導体指数(SOX)"}],
    "effective": [
        {"symbol": "NVDA", "effective_pct": 20.18, "sources": ["SOXL"]},
        {"symbol": "INTC", "effective_pct": 6.34, "sources": ["SOXL", "TECL"]},
        {"symbol": "QCOM", "effective_pct": 10.16, "sources": ["SOXL"]},
        {"symbol": "TINY", "effective_pct": 0.3, "sources": ["SOXL"]},
    ],
}


class TestHoldingClassification:
    def test_etf_only_names_are_separated_from_holdings(self):
        """**INTC は保有ではない。** 混ぜて並べない。"""
        c = WP.classify_holdings(HOLDINGS, LOOKTHROUGH)
        etf_only = {r["symbol"] for r in c["etf_only"]}
        assert "INTC" in etf_only
        assert "NVDA" in etf_only
        # QCOM は直接保有なので ETF経由のみには入らない
        assert "QCOM" not in etf_only
        assert "QCOM" in c["direct_symbols"]

    def test_etf_only_rows_are_labelled(self):
        c = WP.classify_holdings(HOLDINGS, LOOKTHROUGH)
        intc = next(r for r in c["etf_only"] if r["symbol"] == "INTC")
        assert intc["holding_type"] == "etf_only"
        assert "直接保有なし" in intc["label"]

    def test_note_warns_against_mixing(self):
        c = WP.classify_holdings(HOLDINGS, LOOKTHROUGH)
        assert "ETF経由のみの銘柄は保有ではありません" in c["note"]

    def test_ticker_less_fund_is_kept(self):
        """投信（ティッカー無し）を落とさない。"""
        c = WP.classify_holdings(HOLDINGS, LOOKTHROUGH)
        names = {r["name"] for r in c["direct"]}
        assert "iFreeNEXT FANG+インデックス" in names

    def test_same_symbol_across_accounts_is_summed(self):
        holdings = [{"symbol": "2802.T", "weight_pct": 9.4},
                    {"symbol": "2802.T", "weight_pct": 0.9}]
        c = WP.classify_holdings(holdings, None)
        row = next(r for r in c["direct"] if r["symbol"] == "2802.T")
        assert row["weight_pct"] == pytest.approx(10.3)


class TestDerivedIndices:
    def test_japanese_holding_pulls_in_nikkei(self):
        idx = {i["symbol"] for i in WP.derive_indices(HOLDINGS, LOOKTHROUGH)}
        assert "^N225" in idx and "^TPX" in idx

    def test_semiconductor_exposure_pulls_in_sox(self):
        idx = {i["symbol"] for i in WP.derive_indices(HOLDINGS, LOOKTHROUGH)}
        assert "^SOX" in idx

    def test_semiconductor_detected_via_etf_underlying(self):
        """保有名に「半導体」が無くても、ETF の原資産から判定する。"""
        holdings = [{"symbol": "XXXL", "weight_pct": 20, "leverage": 3}]
        lt = {"resolved_etfs": [{"symbol": "XXXL", "lookup": "SOXX",
                                 "underlying": "半導体指数(SOX)"}],
              "effective": []}
        idx = {i["symbol"] for i in WP.derive_indices(holdings, lt)}
        assert "^SOX" in idx

    def test_leverage_pulls_in_long_rates(self):
        idx = {i["symbol"] for i in WP.derive_indices(HOLDINGS, LOOKTHROUGH)}
        assert "^TYX" in idx and "^TNX" in idx

    def test_no_leverage_no_long_rates(self):
        holdings = [{"symbol": "2802.T", "weight_pct": 100}]
        idx = {i["symbol"] for i in WP.derive_indices(holdings, None)}
        assert "^TYX" not in idx

    def test_foreign_assets_pull_in_fx(self):
        idx = {i["symbol"] for i in WP.derive_indices(HOLDINGS, LOOKTHROUGH)}
        assert "JPY=X" in idx

    def test_every_index_has_a_reason(self):
        for i in WP.derive_indices(HOLDINGS, LOOKTHROUGH):
            assert i["reasons"], f"{i['symbol']} に理由が無い"

    def test_vix_is_always_watched(self):
        idx = {i["symbol"] for i in WP.derive_indices([], None)}
        assert "^VIX" in idx


class TestPeerDerivation:
    def _cfg(self):
        return {"peers": {"SOXL": ["NVDA"], "QCOM": ["AVGO"],
                          "2802.T": ["2801.T"], "2737.T": ["2760.T"]}}

    def test_sold_symbol_is_flagged_as_stale(self):
        """**2737.T は 8/7 に売却済み。** 設定に残っていても追わない。"""
        r = WP.derive_peers(HOLDINGS, self._cfg())
        assert "2737.T" in r["stale_config"]
        assert "2737.T" not in r["plan"]
        assert "非保有銘柄" in r["note"]

    def test_held_symbols_are_planned(self):
        r = WP.derive_peers(HOLDINGS, self._cfg())
        assert "SOXL" in r["plan"] and "QCOM" in r["plan"]

    def test_no_stale_entries_produces_clean_note(self):
        cfg = {"peers": {"SOXL": ["NVDA"]}}
        r = WP.derive_peers([{"symbol": "SOXL", "weight_pct": 100}], cfg)
        assert r["stale_config"] == []
        assert "非保有銘柄" not in r["note"]


class TestWatchPlan:
    def test_plan_is_derived_from_current_holdings(self):
        plan = WP.build_watch_plan(HOLDINGS, LOOKTHROUGH)
        assert plan["generated_from"] == "current_holdings"
        assert "今の保有" in plan["note"]

    def test_low_exposure_constituents_are_dropped(self):
        plan = WP.build_watch_plan(HOLDINGS, LOOKTHROUGH)
        assert "TINY" not in plan["constituents"]
        assert "NVDA" in plan["constituents"]

    def test_format_separates_etf_only(self):
        text = WP.format_watch_plan(WP.build_watch_plan(HOLDINGS, LOOKTHROUGH))
        assert "保有していません" in text
        assert "保有と混ぜて読まないこと" in text
        assert "INTC" in text

    def test_format_shows_index_reasons(self):
        text = WP.format_watch_plan(WP.build_watch_plan(HOLDINGS, LOOKTHROUGH))
        assert "見る指数" in text
        assert "レバレッジ商品" in text

    def test_empty_input_does_not_crash(self):
        plan = WP.build_watch_plan([], None)
        assert plan["holdings"]["direct"] == []
        assert WP.format_watch_plan(plan)

    def test_none_plan(self):
        assert WP.format_watch_plan(None) == ""
