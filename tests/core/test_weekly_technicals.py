"""週次レポート用テクニカル集約と過熱判定のテスト.

RSI/ボリンジャーの計算そのものは src/core/screening/technicals.py に委譲しており、
そちらのテストは tests/core/test_technicals.py が担当する。ここでは集約層の
振る舞い（データ不足時の縮退、多数決による過熱判定）を検証する。
"""

import pytest

from src.core.technicals import (
    NEUTRAL,
    OVERBOUGHT,
    OVERSOLD,
    UNKNOWN,
    analyze_prices,
    assess_heat,
    bollinger,
    ema,
    high_low_range,
    macd,
    rsi,
    sma,
    volatility,
)


def _rising(n=300, start=100.0, step=1.0):
    return [start + i * step for i in range(n)]


def _falling(n=300, start=400.0, step=1.0):
    return [start - i * step for i in range(n)]


def _flat(n=300, value=100.0):
    return [value] * n


class TestSMA:
    def test_average_of_last_window(self):
        assert sma([1, 2, 3, 4, 5], 3) == 4.0

    def test_insufficient_data_returns_none(self):
        assert sma([1, 2], 5) is None

    def test_zero_window_returns_none(self):
        assert sma([1, 2, 3], 0) is None

    def test_empty_returns_none(self):
        assert sma([], 3) is None


class TestRSI:
    def test_monotonic_rise_is_maximal(self):
        assert rsi(_rising(50)) == pytest.approx(100.0, abs=0.01)

    def test_monotonic_fall_is_minimal(self):
        assert rsi(_falling(50)) == pytest.approx(0.0, abs=0.01)

    def test_insufficient_data_returns_none(self):
        assert rsi([1, 2, 3]) is None

    def test_result_is_bounded(self):
        values = [100, 105, 98, 110, 95, 120, 90, 130, 85, 140, 80, 150, 75, 160, 70, 170]
        assert 0.0 <= rsi(values) <= 100.0

    def test_fallback_is_close_to_delegated_result(self):
        """pandas経路とフォールバックが同水準の値を出すこと。

        既存の compute_rsi は ewm ベース、フォールバックは単純平均シードの
        Wilder 平滑化で、初期値の扱いが違うため厳密には一致しない。
        乖離が数ポイントに収まっていればフォールバックが壊れていないと言える。
        """
        from src.core.technicals import _rsi_fallback, _series

        values = [100, 105, 98, 110, 95, 120, 90, 130, 85, 140, 80, 150, 75, 160, 70, 170]
        assert rsi(values) == pytest.approx(_rsi_fallback(_series(values), 14), abs=2.0)


class TestEMAAndMACD:
    def test_ema_of_flat_equals_value(self):
        assert ema(_flat(50), 10) == pytest.approx(100.0)

    def test_macd_needs_enough_data(self):
        assert macd(_flat(20)) is None

    def test_macd_positive_in_uptrend(self):
        assert macd(_rising(120))["macd"] > 0

    def test_macd_negative_in_downtrend(self):
        assert macd(_falling(120))["macd"] < 0

    def test_histogram_is_macd_minus_signal(self):
        result = macd(_rising(120))
        assert result["histogram"] == pytest.approx(result["macd"] - result["signal"])


class TestBollinger:
    def test_flat_series_has_zero_width(self):
        result = bollinger(_flat(50))
        assert result["upper"] == pytest.approx(result["lower"])

    def test_percent_b_above_one_when_breaking_out(self):
        result = bollinger(_flat(19, 100.0) + [200.0], window=20)
        assert result["percent_b"] > 1.0

    def test_insufficient_data_returns_none(self):
        assert bollinger([1, 2, 3], window=20) is None

    def test_middle_is_close_to_sma(self):
        values = _rising(50)
        assert bollinger(values, window=20)["middle"] == pytest.approx(
            sma(values, 20), rel=1e-6
        )


class TestHighLowRange:
    def test_position_at_top_when_at_high(self):
        assert high_low_range(_rising(100))["position"] == pytest.approx(1.0)

    def test_position_at_bottom_when_at_low(self):
        assert high_low_range(_falling(100))["position"] == pytest.approx(0.0)

    def test_flat_series_position_is_mid(self):
        assert high_low_range(_flat(100))["position"] == 0.5

    def test_from_high_is_negative_after_decline(self):
        values = _rising(100) + _falling(20, start=199.0)
        assert high_low_range(values)["from_high_pct"] < 0

    def test_empty_returns_none(self):
        assert high_low_range([]) is None

    def test_too_few_points_returns_none(self):
        """3日分から『52週レンジ位置』を出すと無意味な数字が判定に混ざる。"""
        assert high_low_range([100, 101, 102]) is None


class TestVolatility:
    def test_flat_series_has_zero_volatility(self):
        assert volatility(_flat(50)) == pytest.approx(0.0)

    def test_insufficient_data_returns_none(self):
        assert volatility([1, 2]) is None

    def test_volatile_series_exceeds_calm_one(self):
        calm = [100 + (i % 2) * 0.1 for i in range(60)]
        wild = [100 + (i % 2) * 10.0 for i in range(60)]
        assert volatility(wild) > volatility(calm)


class TestAssessHeat:
    def test_no_indicators_is_unknown_not_neutral(self):
        """算出できなければ『中立』ではなく『判定不能』。過熱を見落とさせない。"""
        result = assess_heat({})
        assert result["state"] == UNKNOWN
        assert result["available"] == 0

    def test_single_indicator_is_not_enough_for_a_verdict(self):
        assert assess_heat({"rsi": 80})["state"] == NEUTRAL

    def test_multiple_hot_indicators_yield_overbought(self):
        result = assess_heat({"rsi": 78, "percent_b": 1.1, "range_position": 0.95})
        assert result["state"] == OVERBOUGHT
        assert result["score"] >= 2

    def test_multiple_cold_indicators_yield_oversold(self):
        assert assess_heat(
            {"rsi": 22, "percent_b": -0.1, "range_position": 0.05}
        )["state"] == OVERSOLD

    def test_conflicting_indicators_stay_neutral(self):
        assert assess_heat({"rsi": 78, "percent_b": -0.1})["state"] == NEUTRAL

    def test_signals_are_human_readable(self):
        assert any("RSI" in s for s in assess_heat({"rsi": 78})["signals"])

    def test_deviation_contributes_to_the_verdict(self):
        assert assess_heat({"sma_deviation_pct": 30, "rsi": 75})["state"] == OVERBOUGHT


class TestAnalyzePrices:
    def test_full_analysis_on_uptrend(self):
        result = analyze_prices(_rising(300))
        assert result["last"] == 399.0
        assert result["sma200"] is not None
        assert result["trend"].startswith("上昇トレンド")
        assert result["heat"]["state"] == OVERBOUGHT

    def test_analysis_on_downtrend(self):
        result = analyze_prices(_falling(300))
        assert result["trend"].startswith("下降トレンド")
        assert result["heat"]["state"] == OVERSOLD

    def test_short_series_degrades_gracefully(self):
        result = analyze_prices([100, 101, 102])
        assert result["last"] == 102
        assert result["sma200"] is None
        assert result["rsi14"] is None
        assert result["heat"]["state"] == UNKNOWN

    def test_empty_series(self):
        result = analyze_prices([])
        assert result["last"] is None
        assert result["data_points"] == 0

    def test_nan_values_are_dropped(self):
        assert analyze_prices([100.0, float("nan"), 102.0])["data_points"] == 2
