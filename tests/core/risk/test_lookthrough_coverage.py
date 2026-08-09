"""ETFルックスルーのカバレッジと構成指定のテスト.

## 縛っていること

1. **上位10銘柄で「そのETFのすべて」と誤読させない。**
   yfinance の top_holdings は10銘柄が上限で、SOXX は 39.5%、QQQ は 53.7% が
   個別には見えない。その残りが何なのかをセクター構成で示す。

2. **FANG+ を QQQ で近似しない。**
   NYSE FANG+ = 10銘柄・**等ウェイト**（各10%）
   QQQ        = 100銘柄・**時価総額加重**（上位10で46%）
   別物であり、近似すると実効エクスポージャーが歪む。

3. **未確認の構成を「確認済み」として扱わない。**
"""

from __future__ import annotations

import pytest

from src.core.risk import etf_lookthrough as LT


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """外部を叩かせない。構成は明示的に与える。"""
    monkeypatch.setattr(LT, "fetch_sector_weights",
                        lambda lookup, use_cache=True: {
                            "technology": 100.0} if lookup == "SOXX" else {
                            "technology": 58.0, "communication_services": 13.6,
                            "consumer_cyclical": 11.2})
    monkeypatch.setattr(LT, "fetch_holdings", lambda lookup, **k: {
        "available": True,
        "coverage_pct": 60.48 if lookup == "SOXX" else 46.31,
        "holdings": [{"symbol": s, "weight_pct": w} for s, w in (
            [("NVDA", 12.0), ("AVGO", 10.0), ("AMD", 9.0)]
            if lookup == "SOXX" else
            [("AAPL", 9.0), ("MSFT", 8.0), ("NVDA", 7.0)])],
    })


CFG = {
    "settings": {"min_effective_pct": 0.5},
    "proxies": {"SOXL": {"proxy": "SOXX", "underlying": "半導体指数", "leverage": 3}},
    "fund_proxies": {"iFreeNEXT FANG+": {"proxy": "QQQ", "note": "近似"}},
    "explicit_constituents": {
        "iFreeNEXT FANG+": {
            "verified_as_of": None,
            "weighting": "equal",
            "source": "NYSE FANG+ Index",
            "symbols": ["META", "AAPL", "AMZN", "NFLX", "GOOGL",
                        "MSFT", "NVDA", "TSLA", "AVGO", "CRWD"],
        }
    },
}

HOLDINGS = [
    {"symbol": "SOXL", "name": "Direxion 半導体3x", "weight_pct": 27.5, "leverage": 3},
    {"symbol": None, "name": "iFreeNEXT FANG+インデックス", "weight_pct": 6.9},
]


class TestResidualCoverage:
    def test_uncovered_portion_is_quantified(self):
        r = LT.build_lookthrough(HOLDINGS, cfg=CFG, use_cache=False)
        soxl = next(e for e in r["resolved_etfs"] if e["symbol"] == "SOXL")
        assert soxl["coverage_pct"] == pytest.approx(60.48)
        assert soxl["residual_pct"] == pytest.approx(39.52, abs=0.01)
        # 実質エクスポージャー換算（レバレッジ込み）
        assert soxl["residual_effective_pct"] == pytest.approx(27.5 * 0.3952 * 3, abs=0.1)

    def test_residual_is_characterised_by_sector(self):
        """残り39.5%が何なのかに答える。**空白のままにしない。**"""
        r = LT.build_lookthrough(HOLDINGS, cfg=CFG, use_cache=False)
        soxl = next(e for e in r["resolved_etfs"] if e["symbol"] == "SOXL")
        assert "残り 39.5% は個別に見えていません" in soxl["residual_note"]
        assert "テクノロジー 100.0%" in soxl["residual_note"]
        assert soxl["sectors"]["technology"] == 100.0

    def test_missing_sectors_are_admitted(self, monkeypatch):
        monkeypatch.setattr(LT, "fetch_sector_weights", lambda *a, **k: {})
        r = LT.build_lookthrough(HOLDINGS, cfg=CFG, use_cache=False)
        soxl = next(e for e in r["resolved_etfs"] if e["symbol"] == "SOXL")
        assert "セクター構成も取得できませんでした" in soxl["residual_note"]


class TestExplicitConstituents:
    def test_fang_plus_uses_equal_weights_not_qqq(self):
        """QQQ 近似だと NVDA が過大、CRWD が欠落する。"""
        r = LT.build_lookthrough(HOLDINGS, cfg=CFG, use_cache=False)
        fang = next(e for e in r["resolved_etfs"] if e.get("explicit"))
        assert fang["components"] == 10
        assert fang["coverage_pct"] == 100.0
        assert fang["residual_pct"] == 0.0

        eff = {x["symbol"]: x["effective_pct"] for x in
               r["effective"] + r["folded"]}
        # 等ウェイト: 6.9% × 10% × 1x = 0.69% がFANG+由来の各銘柄の寄与
        assert eff["CRWD"] == pytest.approx(0.69, abs=0.01)
        assert eff["NFLX"] == pytest.approx(0.69, abs=0.01)

    def test_unverified_composition_is_flagged(self):
        r = LT.build_lookthrough(HOLDINGS, cfg=CFG, use_cache=False)
        fang = next(e for e in r["resolved_etfs"] if e.get("explicit"))
        assert fang["approximate"] is True
        assert "未確認の構成です" in fang["residual_note"]
        assert fang["verified_as_of"] is None

    def test_verified_composition_is_marked_confirmed(self):
        cfg = {**CFG, "explicit_constituents": {
            "iFreeNEXT FANG+": {
                **CFG["explicit_constituents"]["iFreeNEXT FANG+"],
                "verified_as_of": "2026-08-09"}}}
        r = LT.build_lookthrough(HOLDINGS, cfg=cfg, use_cache=False)
        fang = next(e for e in r["resolved_etfs"] if e.get("explicit"))
        assert fang["approximate"] is False
        assert "確認済み" in fang["residual_note"]

    def test_explicit_beats_proxy(self):
        """明示指定があれば proxy(QQQ) を使わない。"""
        r = LT.build_lookthrough(HOLDINGS, cfg=CFG, use_cache=False)
        fang = next(e for e in r["resolved_etfs"] if e.get("explicit"))
        assert fang["lookup"] == "(明示指定)"

    def test_no_explicit_entry_falls_back_to_proxy(self):
        cfg = {**CFG, "explicit_constituents": {}}
        r = LT.build_lookthrough(HOLDINGS, cfg=cfg, use_cache=False)
        assert not any(e.get("explicit") for e in r["resolved_etfs"])


class TestExplicitLookup:
    def test_matches_by_prefix(self):
        got = LT._explicit_constituents("iFreeNEXT FANG+インデックス", CFG)
        assert got is not None
        assert len(got["holdings"]) == 10
        assert got["holdings"][0]["weight_pct"] == pytest.approx(10.0)

    def test_unknown_fund_returns_none(self):
        assert LT._explicit_constituents("eMAXIS Slim 全世界株式", CFG) is None

    def test_empty_name_returns_none(self):
        assert LT._explicit_constituents(None, CFG) is None
        assert LT._explicit_constituents("", CFG) is None

    def test_custom_weights_are_supported(self):
        cfg = {"explicit_constituents": {"Test": {
            "weighting": "custom", "symbols": ["A", "B"],
            "weights": {"A": 70.0, "B": 30.0}}}}
        got = LT._explicit_constituents("Test Fund", cfg)
        assert got["holdings"] == [{"symbol": "A", "weight_pct": 70.0},
                                   {"symbol": "B", "weight_pct": 30.0}]


def test_sector_labels_are_japanese():
    text = LT._top_sectors({"technology": 58.0, "communication_services": 13.6})
    assert "テクノロジー" in text and "通信サービス" in text


def test_top_sectors_handles_empty():
    assert LT._top_sectors({}) == ""
