"""較正窓が観測数に合わせて縮むことのテスト.

## 名指しする問題

`volatility()` は `len(closes) < window + 1` で None を返す。
較正窓を 250 に固定していたため **251本以上ないと較正できなかった**。

日本の年間営業日は約245日で、1年分の履歴は 244本しか無い。
つまり**東証銘柄は一度も較正されず、前提σのまま**レンジを出していた。

このモジュールの docstring 自身が
「2802.T の250日実測は 42.9%、前提 22% は 1.95倍の本物の乖離」と書いていたのに、
パイプラインはその較正に到達していなかった。
"""

from __future__ import annotations

import math

from src.core.portfolio import vol_calibration as VC


def _series(n: int, daily_sigma: float = 0.02, seed: int = 7) -> list[float]:
    """決定論的な擬似ランダム終値列。"""
    import random

    rnd = random.Random(seed)
    price = 100.0
    out = [price]
    for _ in range(n - 1):
        price *= math.exp(rnd.gauss(0.0, daily_sigma))
        out.append(price)
    return out


class TestRealizedVolWindow:
    def test_japanese_year_length_still_calibrates(self):
        """244本（東証1年分）で較正できること。**ここが本丸。**"""
        assert VC.realized_vol(_series(244)) is not None

    def test_full_window_still_works(self):
        assert VC.realized_vol(_series(300)) is not None

    def test_below_minimum_observations_returns_none(self):
        """本当に観測不足のときは黙って値を作らない。"""
        assert VC.realized_vol(_series(VC.MIN_OBSERVATIONS - 5)) is None

    def test_shrunken_window_is_close_to_full_window(self):
        """窓を縮めても水準が壊れないこと（244 vs 300 で大差ない）。"""
        long_v = VC.realized_vol(_series(300))
        short_v = VC.realized_vol(_series(244))
        assert abs(long_v - short_v) < long_v * 0.5


class TestCalibrateDisclosesWindow:
    def test_effective_window_is_reported(self):
        got = VC.calibrate("2802.T", {"annual_vol_pct": 22.0}, _series(244))
        assert got["available"] is True
        # 実際に使った窓を黙らない
        assert got["effective_window"] == 243
        assert got["window"] == VC.STRUCTURAL_WINDOW

    def test_us_length_uses_full_window(self):
        got = VC.calibrate("QCOM", {"annual_vol_pct": 20.0}, _series(300))
        assert got["effective_window"] == VC.STRUCTURAL_WINDOW

    def test_insufficient_data_is_not_silently_calibrated(self):
        got = VC.calibrate("X", {"annual_vol_pct": 20.0}, _series(50))
        assert got["available"] is False
        assert got["verdict"] == "insufficient_data"
        # 前提をそのまま使ったことを明示する
        assert got["used_underlying_vol_pct"] == 20.0
