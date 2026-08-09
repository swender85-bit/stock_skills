"""構成銘柄インテリジェンスのテスト.

## なぜ必要か

ルックスルーが出す実効エクスポージャーは、レポートでは
**銘柄名と比率の一覧**にしかなっていなかった。

> このFANG＋の構成についても、中身の企業が与える今後の影響とか分析していないから、
> なんの意味もなしていない

**「NVDA が20%」は情報だが、「だから何を見るべきか」が無ければ判断に使えない。**

## 縛っていること

1. **単独指標では見えない形に名前を付ける**
   （週次プラス×月次マイナス＝「戻り」。これは実データで決定的だった）
2. **取れなかったものを「無い」と書かない**
3. ニュースは**外部言説（深度1）**であり一次観測ではない
"""

from __future__ import annotations

import pytest

from src.core.research import constituent_intel as CI


class TestSignalClassification:
    def test_rebound_is_named(self):
        """週次プラス×月次マイナス＝「戻り」。上放れではない。

        2026-08-09 の実データでは MU/QCOM/INTC/AMAT/LRCX がこの形で、
        SOXL +22% を「上放れ」と読むか「戻り」と読むかを分けた。
        """
        s = CI._classify(week=6.6, month=-11.5, rsi=48, dev=10)
        assert any("戻り" in x for x in s)
        assert any("上放れではない" in x for x in s)

    def test_sustained_advance_is_distinguished(self):
        s = CI._classify(week=11.6, month=10.4, rsi=64, dev=15)
        assert any("継続上昇" in x for x in s)
        assert not any("戻り" in x for x in s)

    def test_downtrend(self):
        s = CI._classify(week=-3.0, month=-12.0, rsi=40, dev=5)
        assert any("下降継続" in x for x in s)

    def test_overheated_and_oversold(self):
        assert any("過熱" in x for x in CI._classify(1, 1, 78, 5))
        assert any("売られすぎ" in x for x in CI._classify(-1, -1, 22, 5))

    def test_stretched_from_200day(self):
        s = CI._classify(week=6.6, month=-11.5, rsi=48, dev=64.1)
        assert any("平均回帰の余地" in x for x in s)

    def test_missing_inputs_produce_no_false_signal(self):
        assert CI._classify(None, None, None, None) == []


class TestPctChange:
    def test_computes_lookback(self):
        closes = [100.0] * 25 + [110.0]
        assert CI._pct_change(closes, 5) == pytest.approx(10.0)

    def test_short_series_returns_none(self):
        assert CI._pct_change([100.0, 101.0], 21) is None

    def test_empty_returns_none(self):
        assert CI._pct_change([], 5) is None


class TestBuildIntel:
    def _lookthrough(self):
        return {"effective": [
            {"symbol": "NVDA", "effective_pct": 20.18, "sources": ["SOXL", "TECL"]},
            {"symbol": "MU", "effective_pct": 10.35, "sources": ["SOXL"]},
            {"symbol": "TINY", "effective_pct": 0.2, "sources": ["SOXL"]},
        ]}

    def test_low_exposure_names_are_dropped(self, monkeypatch):
        monkeypatch.setattr(CI, "build_dossier",
                            lambda sym, **k: {"symbol": sym, "missing": [],
                                              "signals": [], "news": [],
                                              "effective_pct": k.get("effective_pct", 0)})
        r = CI.build_constituent_intel(self._lookthrough(), [], min_effective_pct=1.0)
        syms = {d["symbol"] for d in r["dossiers"]}
        assert syms == {"NVDA", "MU"}          # TINY(0.2%) は落ちる

    def test_signals_are_aggregated_across_names(self, monkeypatch):
        def fake(sym, **k):
            return {"symbol": sym, "missing": [], "news": [],
                    "effective_pct": k.get("effective_pct", 0),
                    "signals": ["戻り（週次プラス・月次マイナス。上放れではない）"]}

        monkeypatch.setattr(CI, "build_dossier", fake)
        r = CI.build_constituent_intel(self._lookthrough(), [])
        assert r["signals"]["戻り"] == ["NVDA", "MU"]

    def test_missing_news_is_reported_not_silently_dropped(self, monkeypatch):
        monkeypatch.setattr(CI, "build_dossier",
                            lambda sym, **k: {"symbol": sym, "missing": ["ニュース"],
                                              "signals": [], "news": [],
                                              "effective_pct": k.get("effective_pct", 0)})
        r = CI.build_constituent_intel(self._lookthrough(), [])
        assert set(r["missing_news"]) == {"NVDA", "MU"}
        assert "『材料なし』ではありません" in r["note"]

    def test_no_lookthrough_is_not_reported_as_empty_contents(self):
        r = CI.build_constituent_intel(None, [])
        assert r["available"] is False
        assert "『中身が無い』ではありません" in r["note"]

    def test_covered_pct_is_summed(self, monkeypatch):
        monkeypatch.setattr(CI, "build_dossier",
                            lambda sym, **k: {"symbol": sym, "missing": [],
                                              "signals": [], "news": [],
                                              "effective_pct": k.get("effective_pct", 0)})
        r = CI.build_constituent_intel(self._lookthrough(), [])
        assert r["covered_pct"] == pytest.approx(30.53, abs=0.01)


class TestFormatting:
    def _intel(self):
        return {
            "available": True, "covered_pct": 30.5,
            "dossiers": [
                {"symbol": "MU", "effective_pct": 10.35, "week_change_pct": 6.6,
                 "month_change_pct": -11.5, "rsi14": 47.6,
                 "sma200_deviation_pct": 64.1, "per": 19.8,
                 "next_earnings": "2026-09-24", "days_to_earnings": 46,
                 "signals": ["戻り（週次プラス・月次マイナス。上放れではない）"],
                 "news": [{"headline": "Micron corrected 30% from ATH"}],
                 "missing": []},
            ],
            "signals": {"戻り": ["MU"]},
            "missing_news": ["2802.T"],
            "note": "上位1銘柄を分析しました。",
        }

    def test_table_includes_shape_and_earnings(self):
        text = CI.format_constituent_intel(self._intel())
        assert "MU" in text and "10.35%" in text
        assert "2026-09-24(46日)" in text
        assert "戻り" in text

    def test_common_shapes_section(self):
        text = CI.format_constituent_intel(self._intel())
        assert "共通して現れている形" in text

    def test_news_is_labelled_external_discourse(self):
        text = CI.format_constituent_intel(self._intel())
        assert "外部言説" in text
        assert "Micron corrected" in text

    def test_missing_news_warning_is_shown(self):
        text = CI.format_constituent_intel(self._intel())
        assert "『材料なし』ではありません" in text

    def test_unavailable_intel_is_explained(self):
        text = CI.format_constituent_intel(None)
        assert "展開できませんでした" in text


def test_news_is_marked_as_external_discourse(monkeypatch):
    """ニュースは深度1。一次観測に格上げしない。"""
    import src.data.finnhub_client as fh

    monkeypatch.setattr(fh, "is_available", lambda: True)
    monkeypatch.setattr(fh, "get_company_news",
                        lambda s, days=7, limit=5: [{"headline": "x"}])
    got = CI._news_for("NVDA", 7, 3)
    assert got[0]["provenance"] == "external_discourse"


def test_news_unavailable_returns_empty(monkeypatch):
    import src.data.finnhub_client as fh

    monkeypatch.setattr(fh, "is_available", lambda: False)
    assert CI._news_for("NVDA", 7, 3) == []
