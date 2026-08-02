"""予測前提ボラの較正 — 比較してよい量だけを比較する。

2026-08-01 の週次レポートは「トーメンデバイス 想定30% vs 実測129.8% = 4倍以上、
予測寄与は事実上意味を持たない」と書いたが、この実測値は **20日窓**の年率換算で、
数年スパンの構造的前提とは別の量だった。250日窓で測ると 69.0%（2.3倍）で、
警告の大半は窓の不一致による水増しだった。

守るべき性質:
- 短窓のスポット推定を前提σと突き合わせない
- 置き換えではなく縮小推定で混ぜる（実測ボラは平均回帰する）
- 観測が足りなければ数字をひねり出さない
- レバレッジ商品の実測は倍率で割って原資産に戻してから比較する
"""

from __future__ import annotations

import math

import pytest

from src.core.portfolio import vol_calibration as vc


def _series(daily_vol: float, n: int = 400, seed: int = 7) -> list[float]:
    """指定した日次σを持つ疑似価格列。年率は daily_vol*sqrt(252)。"""
    import random

    rnd = random.Random(seed)
    px = [100.0]
    for _ in range(n):
        px.append(px[-1] * math.exp(rnd.gauss(0.0, daily_vol)))
    return px


def _annual(daily_vol: float) -> float:
    return daily_vol * math.sqrt(252) * 100.0


# ---------------------------------------------------------------------------
# 窓の区別
# ---------------------------------------------------------------------------


def test_structural_window_is_much_longer_than_spot():
    """20日窓は観測20個。前提と比較してよい量ではない。"""
    assert vc.STRUCTURAL_WINDOW >= 250
    assert vc.SPOT_WINDOW == 20
    assert vc.STRUCTURAL_WINDOW > vc.SPOT_WINDOW * 5


def test_spot_vol_is_reported_but_flagged_as_incomparable():
    r = vc.calibrate("X", {"annual_vol_pct": 30.0}, _series(0.02))
    assert r["spot_vol_pct"] is not None
    assert "比較してはいけない" in r["spot_note"]


def test_spot_vol_is_not_used_as_the_calibration_input():
    """スポットが極端でも、採用値は長窓の実測から決まる。"""
    import math
    import random

    closes = _series(0.02, n=400)
    # 直近20日だけ日次σを3倍にする（決算期の荒れに相当）
    rnd = random.Random(11)
    spiky = list(closes[:-20])
    for _ in range(20):
        spiky.append(spiky[-1] * math.exp(rnd.gauss(0.0, 0.06)))

    calm = vc.calibrate("X", {"annual_vol_pct": 30.0}, closes)
    noisy = vc.calibrate("X", {"annual_vol_pct": 30.0}, spiky)

    # スポットは大きく跳ねる
    assert noisy["spot_vol_pct"] > calm["spot_vol_pct"] * 1.8
    # 一方 250日窓では、20/250 の寄与しか無いので跳ね方はずっと小さい。
    # ここが崩れると「直近の荒れ」がそのまま将来の前提になってしまう。
    spot_jump = noisy["spot_vol_pct"] / calm["spot_vol_pct"]
    used_jump = noisy["used_underlying_vol_pct"] / calm["used_underlying_vol_pct"]
    assert used_jump < spot_jump / 2


# ---------------------------------------------------------------------------
# 較正の中身
# ---------------------------------------------------------------------------


def test_realized_vol_matches_the_generating_process():
    closes = _series(0.02, n=400)
    got = vc.realized_vol(closes, window=250)
    assert got == pytest.approx(_annual(0.02), rel=0.2)


def test_calibration_blends_rather_than_replaces():
    """実測をそのまま採用すると予測がノイズを追いかける。"""
    closes = _series(0.03, n=400)          # 年率 ~47.6%
    r = vc.calibrate("X", {"annual_vol_pct": 20.0}, closes)
    used, realized = r["used_underlying_vol_pct"], r["implied_underlying_vol_pct"]
    assert 20.0 < used < realized, "前提と実測の間に入るべき"
    expected = vc.REALIZED_WEIGHT * realized + (1 - vc.REALIZED_WEIGHT) * 20.0
    assert used == pytest.approx(expected, rel=0.01)


def test_leverage_is_divided_out_before_comparing():
    """前提は原資産ベース。実測はETF側。倍率で揃えないと3倍ずれる。"""
    closes = _series(0.06, n=400)          # ETF年率 ~95%
    r = vc.calibrate("SOXL", {"annual_vol_pct": 32.0}, closes, leverage=3)
    assert r["implied_underlying_vol_pct"] == pytest.approx(
        r["realized_vol_pct"] / 3, rel=0.01)
    assert r["verdict"] == "ok"


def test_effective_vol_multiplies_leverage_back():
    closes = _series(0.06, n=400)
    r = vc.calibrate("SOXL", {"annual_vol_pct": 32.0}, closes, leverage=3)
    assert r["effective_used_vol_pct"] == pytest.approx(
        r["used_underlying_vol_pct"] * 3, rel=0.01)


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------


def test_large_divergence_is_called_unreliable():
    closes = _series(0.045, n=400)         # ~71%
    r = vc.calibrate("2737.T", {"annual_vol_pct": 30.0}, closes)
    assert r["verdict"] == "unreliable"
    assert "妥当でない" in r["message"]


def test_matching_assumption_is_ok_not_flagged():
    """TECL は実測とほぼ一致していた。誤警報を出さないこと。"""
    closes = _series(0.02, n=400)          # ~31.7%
    r = vc.calibrate("TECL", {"annual_vol_pct": 31.0}, closes)
    assert r["verdict"] == "ok"


def test_conservative_assumption_is_named_as_such():
    closes = _series(0.01, n=400)          # ~15.9%
    r = vc.calibrate("TQQQ", {"annual_vol_pct": 30.0}, closes)
    assert r["verdict"] == "conservative"
    assert "保守的" in r["message"]


# ---------------------------------------------------------------------------
# 取れないときに数字をひねり出さない
# ---------------------------------------------------------------------------


def test_insufficient_observations_keeps_the_assumption():
    r = vc.calibrate("X", {"annual_vol_pct": 30.0}, _series(0.02, n=40))
    assert r["available"] is False
    assert r["verdict"] == "insufficient_data"
    assert r["used_underlying_vol_pct"] == 30.0


def test_missing_assumption_does_not_fabricate_one():
    r = vc.calibrate("X", {}, _series(0.02, n=400))
    assert r["available"] is False
    assert r["used_underlying_vol_pct"] is None


def test_empty_closes_are_handled():
    r = vc.calibrate("X", {"annual_vol_pct": 30.0}, [])
    assert r["available"] is False
    assert r["observations"] == 0


# ---------------------------------------------------------------------------
# まとめ
# ---------------------------------------------------------------------------


def test_summary_counts_and_lists_problem_symbols():
    out = vc.calibrate_positions([
        {"symbol": "A", "assumption": {"annual_vol_pct": 30.0},
         "closes": _series(0.045, n=400)},                      # unreliable
        {"symbol": "B", "assumption": {"annual_vol_pct": 31.0},
         "closes": _series(0.02, n=400, seed=9)},                # ok
        {"symbol": "C", "assumption": {"annual_vol_pct": 30.0},
         "closes": _series(0.02, n=30)},                         # 較正不可
    ])
    assert out["total"] == 3
    assert out["calibrated"] == 2
    assert out["unreliable"] == ["A"]
    assert "2/3" in out["summary"]


def test_caveat_explains_the_window_difference():
    out = vc.calibrate_positions([])
    assert str(vc.STRUCTURAL_WINDOW) in out["caveat"]
    assert str(vc.SPOT_WINDOW) in out["caveat"]


def test_calibrate_positions_survives_a_bad_row():
    out = vc.calibrate_positions([
        {"symbol": "A", "assumption": {"annual_vol_pct": 30.0},
         "closes": _series(0.02, n=400)},
        {"symbol": "B", "assumption": None, "closes": None},
    ])
    assert out["total"] == 2
