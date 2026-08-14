"""市況レジーム判定のテスト.

## なぜ必要か

見通しは「1ヶ月で ▲22% 〜 +30%」としか出せていなかった。
正直だが**判断には使えない**。3xレバレッジ62%ならそうなる、というだけで、
**いま上下どちらに傾いているか**を何も言っていない。

## 縛っていること

1. **取れなかった軸を「中立」にしない。** 中立にすると「問題なし」と読まれる
2. **確率を出さない。** 出せる根拠がないので、出せば予言に化ける
3. 低VIXを「安心」と読ませない（**警戒の不在**である）
"""

from __future__ import annotations

import pytest

from src.core.risk.regime import assess_regime, format_regime


class TestAxes:
    def test_extreme_greed_is_flagged(self):
        r = assess_regime(fear_greed=85.4)
        fg = r["axes"][0]
        assert fg["state"] == "極度の強欲"
        assert "新規リスク追加は分が悪い" in fg["note"]

    def test_extreme_fear(self):
        assert assess_regime(fear_greed=15.0)["axes"][0]["state"] == "極度の恐怖"

    def test_low_vix_is_not_read_as_safety(self):
        """低VIXは「安心」ではなく「**警戒の不在**」。"""
        vix = assess_regime(vix=14.9)["axes"][1]
        assert vix["state"] == "低位（楽観）"
        assert "警戒の不在" in vix["note"]
        assert "安心ではなく" in vix["note"]

    def test_high_long_rate_is_flagged_for_leveraged_etfs(self):
        rate = assess_regime(ust30y=5.211)["axes"][2]
        assert rate["state"] == "高位"
        assert "ロング・デュレーション" in rate["note"]
        assert "短期金利ではなく" in rate["note"]

    def test_normal_long_rate(self):
        assert assess_regime(ust30y=4.2)["axes"][2]["state"] == "通常"


class TestMissingDataIsNotNeutral:
    def test_missing_axis_is_undeterminable(self):
        """**「中立」にすると「問題なし」と読まれる。**"""
        r = assess_regime()
        for a in r["axes"]:
            assert a["available"] is False
            assert a["state"] == "判定不能"
            assert "中立ではありません" in a["note"]

    def test_all_missing_gives_undeterminable_tilt(self):
        r = assess_regime()
        assert r["tilt"] == "判定不能"
        assert "測れていない" in r["tilt_note"]

    def test_partial_data_is_counted(self):
        r = assess_regime(fear_greed=85.4)
        assert r["available_axes"] == 1
        assert r["total_axes"] == 3
        assert "取得できなかった軸があります" in r["note"]


class TestTilt:
    def test_optimism_is_detected(self):
        r = assess_regime(fear_greed=85.4, vix=14.9, ust30y=4.2)
        assert r["tilt"] == "楽観に傾いている"
        assert "楽観の最中に取るリスクは高くつく" in r["tilt_note"]

    def test_caution_is_detected(self):
        r = assess_regime(fear_greed=20.0, vix=30.0, ust30y=5.5)
        assert r["tilt"] == "警戒に傾いている"

    def test_no_probability_is_emitted(self):
        """**確率を出さない。** 出せる根拠が無い。"""
        r = assess_regime(fear_greed=85.4, vix=14.9, ust30y=5.2)
        blob = format_regime(r)
        assert "確率" in blob and "出していません" in blob
        # 「◯%の確率で下落」のような表現を作らない
        assert "の確率で" not in blob.replace("確率も出していません", "")


class TestPortfolioCautions:
    def test_high_effective_leverage_is_called_out(self):
        r = assess_regime(effective_leverage=1.86)
        assert any("実効レバレッジ 1.86倍" in c for c in r["cautions"])

    def test_zero_cash_breaks_the_stated_policy(self):
        r = assess_regime(cash_ratio=0.0005)
        assert any("買えない状態は、方針を持っていないのと同じ" in c
                   for c in r["cautions"])

    def test_healthy_cash_produces_no_warning(self):
        r = assess_regime(cash_ratio=0.10, effective_leverage=1.0)
        assert r["cautions"] == []


class TestConstituentShape:
    def test_rebound_majority_is_called_not_a_breakout(self):
        r = assess_regime(constituent_signals={
            "戻り": ["MU", "QCOM", "INTC", "AMAT", "LRCX", "2802.T"],
            "継続上昇": ["NVDA", "AVGO", "MSFT", "MDT"]})
        assert any("上放れではない" in c for c in r["cautions"])

    def test_narrow_leadership_is_flagged(self):
        r = assess_regime(constituent_signals={
            "戻り": ["MU", "QCOM", "INTC", "AMAT", "LRCX", "2802.T"],
            "継続上昇": ["NVDA", "AVGO", "MSFT", "MDT"]})
        assert any("担い手が絞られており" in c for c in r["cautions"])

    def test_broad_advance_is_recognised(self):
        r = assess_regime(constituent_signals={
            "戻り": ["MU"],
            "継続上昇": ["NVDA", "AVGO", "MSFT", "MDT", "AMD", "AAPL"]})
        assert any("裾野が広い" in c for c in r["cautions"])


class TestFormatting:
    def test_header_says_state_not_prediction(self):
        text = format_regime(assess_regime(fear_greed=85.4))
        assert "状態の記述。予測ではない" in text

    def test_cautions_are_rendered(self):
        text = format_regime(assess_regime(
            fear_greed=85.4, effective_leverage=1.86, cash_ratio=0.0005))
        assert "この状態で特に効くこと" in text
        assert "1.86倍" in text

    def test_none_input(self):
        assert "判定できませんでした" in format_regime(None)
