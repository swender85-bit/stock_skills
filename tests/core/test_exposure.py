"""因子エクスポージャーのテスト (土曜設計書 提案2)。

守るべき性質:
- 因子は5個以内に固定（増やさない）
- 推定期間で結果が変わることを隠さない（不安定フラグを因子単位で立てる）
- 日本株は米国因子に1日ラグを掛ける（同日だと感応度が消える）
- レバレッジETFは倍率を掛ける（1xとして扱うとリスクを3分の1に見誤る）
"""

from __future__ import annotations

import math

import pytest

from src.core import exposure as ex


def _series(values: list[float], start_day: int = 1) -> dict[str, float]:
    return {f"2026-{(start_day + i) // 30 + 1:02d}-{(start_day + i) % 30 + 1:02d}": v
            for i, v in enumerate(values)}


def _linear_factor(n: int = 200, seed: float = 0.01) -> dict[str, float]:
    return {f"d{i:04d}": math.sin(i * seed) * 0.02 for i in range(n)}


# ---------------------------------------------------------------------------
# 因子集合
# ---------------------------------------------------------------------------


def test_factor_set_is_capped_at_five():
    """因子は最小限に固定する。増やすと恣意性が支配する（設計書 提案2-⑧）。"""
    assert len(ex.FACTORS) <= 5
    assert set(ex.FACTOR_TICKERS) == set(ex.FACTORS)
    assert set(ex.FACTOR_LABELS) >= set(ex.FACTORS)


# ---------------------------------------------------------------------------
# リターン系列
# ---------------------------------------------------------------------------


class _FakeHistory:
    def __init__(self, closes):
        self._closes = closes

    def __getitem__(self, key):
        assert key == "Close"
        return self

    def dropna(self):
        return self

    def items(self):
        return iter(self._closes)


def test_daily_returns_from_history():
    hist = _FakeHistory([("2026-01-01", 100.0), ("2026-01-02", 110.0),
                         ("2026-01-03", 99.0)])
    r = ex.daily_returns(hist)
    assert r["2026-01-02"] == pytest.approx(0.10)
    assert r["2026-01-03"] == pytest.approx(-0.10)


def test_daily_returns_on_missing_history():
    assert ex.daily_returns(None) == {}


# ---------------------------------------------------------------------------
# ラグ調整
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("symbol,expected", [
    ("7203.T", True), ("2802.T", True), ("0700.HK", True), ("D05.SI", True),
    ("AAPL", False), ("SOXL", False), (None, False),
])
def test_needs_lag(symbol, expected):
    assert ex.needs_lag(symbol) is expected


def test_lag_shifts_us_factors_by_one_day():
    """日本株を米国因子に同日で当てると、時差のぶん感応度が消える。"""
    target = {"d1": 0.01, "d2": 0.02, "d3": 0.03}
    factor = {"d1": 0.1, "d2": 0.2, "d3": 0.3}
    shifted = ex._shift_one_day(factor, ["d1", "d2", "d3"])
    assert shifted == {"d2": 0.1, "d3": 0.2}
    assert "d1" not in shifted, "初日は前日が無いので落ちる"
    assert target  # 対象系列は変えない


def test_align_applies_lag_only_to_us_factors():
    target = {f"d{i}": 0.01 for i in range(1, 60)}
    us = {f"d{i}": float(i) for i in range(1, 60)}
    fx = {f"d{i}": float(i) * 10 for i in range(1, 60)}
    y, x = ex._align(target, {"market": us, "usdjpy": fx}, 100, lag_us=True)
    assert len(y) >= ex.MIN_SAMPLES
    # 米国因子はずれ、為替はずれない
    assert x["market"][0] != x["usdjpy"][0] / 10


# ---------------------------------------------------------------------------
# 回帰
# ---------------------------------------------------------------------------


def test_exposure_recovers_known_beta():
    """既知のβを持つ合成データで、回帰が正しい係数を返すこと。"""
    factor = _linear_factor(260)
    target = {d: v * 2.5 for d, v in factor.items()}
    r = ex.estimate_exposure("SYNTH", factor_returns={"market": factor},
                             target_returns=target)
    assert r["available"] is True
    assert r["betas"]["market"] == pytest.approx(2.5, abs=0.05)
    assert r["r2"] == pytest.approx(1.0, abs=0.01)


def test_exposure_unavailable_without_target():
    r = ex.estimate_exposure("X", factor_returns={"market": _linear_factor()},
                             target_returns={})
    assert r["available"] is False
    assert "価格履歴" in r["reason"]


def test_exposure_unavailable_without_factors():
    r = ex.estimate_exposure("X", factor_returns={},
                             target_returns=_linear_factor())
    assert r["available"] is False
    assert "因子系列" in r["reason"]


def test_exposure_requires_minimum_samples():
    short = _linear_factor(20)
    r = ex.estimate_exposure("X", factor_returns={"market": short},
                             target_returns=short)
    assert r["available"] is False
    assert "サンプル" in r["reason"]


def test_low_r2_marks_all_factors_unstable():
    """説明力が低いと、βの符号自体がノイズになる。"""
    import random

    random.seed(7)
    factor = _linear_factor(260)
    target = {d: random.gauss(0, 0.02) for d in factor}
    r = ex.estimate_exposure("NOISE", factor_returns={"market": factor},
                             target_returns=target)
    assert r["available"] is True
    assert r["low_r2"] is True
    assert r["unstable"] is True
    assert "market" in r["unstable_factors"]


def test_stability_is_reported_per_factor():
    """『原油が反転した』を理由に半導体まで信用しない、という粗さを避ける。"""
    windows = {
        "60d": {"betas": {"semis": 1.2, "oil": 0.3}},
        "120d": {"betas": {"semis": 1.1, "oil": -0.4}},
        "250d": {"betas": {"semis": 1.3, "oil": 0.2}},
    }
    unstable, reasons, flipped = ex._stability(windows)
    assert unstable is True
    assert flipped == ["oil"]
    assert "semis" not in flipped


def test_stability_needs_two_windows():
    assert ex._stability({"60d": {"betas": {"a": 1.0}}}) == (False, [], [])


def test_estimate_many_isolates_failures(monkeypatch):
    monkeypatch.setattr(ex, "build_factor_returns", lambda period="2y": {})
    out = ex.estimate_many(["A", "B"])
    assert set(out) == {"A", "B"}
    assert all(v["available"] is False for v in out.values())


# ---------------------------------------------------------------------------
# ポートフォリオ集約
# ---------------------------------------------------------------------------


def _exposure(symbol, **betas):
    return {"symbol": symbol, "available": True, "betas": betas,
            "unstable": False, "unstable_factors": []}


def test_portfolio_exposure_is_value_weighted():
    holdings = [{"symbol": "A", "weight_pct": 75.0},
                {"symbol": "B", "weight_pct": 25.0}]
    exposures = {"A": _exposure("A", market=2.0), "B": _exposure("B", market=0.0)}
    pf = ex.portfolio_exposure(holdings, exposures)
    assert pf["betas"]["market"] == pytest.approx(1.5)


def test_leverage_multiplies_effective_exposure():
    """3xを1xとして扱うと実効エクスポージャーを3分の1に見誤る。"""
    plain = ex.portfolio_exposure(
        [{"symbol": "A", "weight_pct": 50.0}], {"A": _exposure("A", semis=1.0)})
    levered = ex.portfolio_exposure(
        [{"symbol": "A", "weight_pct": 50.0, "leverage": 3}],
        {"A": _exposure("A", semis=1.0)})
    assert levered["effective_weight_pct"] == pytest.approx(150.0)
    assert plain["effective_weight_pct"] == pytest.approx(50.0)


def test_portfolio_exposure_reports_missing_coverage():
    holdings = [{"symbol": "A", "weight_pct": 50.0},
                {"symbol": "B", "weight_pct": 50.0}]
    pf = ex.portfolio_exposure(holdings, {"A": _exposure("A", market=1.0)})
    assert pf["missing"] == ["B"]
    assert pf["coverage_pct"] == pytest.approx(50.0)
    assert "含まれていません" in pf["note"]


def test_portfolio_exposure_unavailable_without_any_estimate():
    pf = ex.portfolio_exposure([{"symbol": "A", "weight_pct": 100.0}], {})
    assert pf["available"] is False


# ---------------------------------------------------------------------------
# 傾斜の説明
# ---------------------------------------------------------------------------


def test_currency_double_long_is_called_out():
    """通貨の二重ロングは日本の個人投資家の最大の隠れ集中。"""
    lines = ex.describe_tilt({"available": True, "betas": {"usdjpy": 0.71}})
    assert any("円安で儲かる" in x for x in lines)
    assert any("為替は分散していない" in x for x in lines)


def test_tilt_silent_when_flat():
    assert ex.describe_tilt({"available": True, "betas": {"usdjpy": 0.05}}) == []


def test_tilt_empty_when_unavailable():
    assert ex.describe_tilt({"available": False}) == []


# ---------------------------------------------------------------------------
# 相関 / 因子双子
# ---------------------------------------------------------------------------


def test_correlation_of_identical_series_is_one():
    s = _linear_factor(100)
    assert ex.correlation(s, s) == pytest.approx(1.0, abs=1e-9)


def test_correlation_needs_minimum_overlap():
    a = _linear_factor(10)
    assert ex.correlation(a, a) is None


def test_factor_twin_detected_for_near_identical_movement():
    base = _linear_factor(120)
    twin = {d: v * 1.01 for d, v in base.items()}
    hits = ex.find_factor_twins("CAND", {"HELD": twin}, candidate_returns=base)
    assert hits and hits[0]["symbol"] == "HELD"
    assert "実質的な買い増し" in hits[0]["message"]


def test_factor_twin_not_flagged_for_uncorrelated():
    import random

    random.seed(3)
    base = _linear_factor(160)
    other = {d: random.gauss(0, 0.02) for d in base}
    assert ex.find_factor_twins("CAND", {"HELD": other},
                                candidate_returns=base) == []


def test_worst_days_picks_the_bottom_tail():
    market = {f"d{i:03d}": float(i) for i in range(200)}
    worst = ex.worst_days(market, pct=10.0)
    assert worst[0] == "d000"
    assert len(worst) >= ex.MIN_SAMPLES // 4


def test_stress_correlation_requires_days():
    s = _linear_factor(100)
    assert ex.stress_correlation(s, s, None) is None


# ---------------------------------------------------------------------------
# 週次の因子変化（模型監査の入力）
# ---------------------------------------------------------------------------


def test_weekly_factor_moves_compounds_daily_returns(monkeypatch):
    """指数ウォッチの percent_change は日次。週次予測に流用してはいけない。"""
    monkeypatch.setattr(ex, "fetch_returns",
                        lambda t, period="2y": {f"d{i}": 0.01 for i in range(10)})
    moves = ex.weekly_factor_moves(days=5, tickers={"market": "^GSPC"})
    assert moves["market"] == pytest.approx((1.01 ** 5 - 1) * 100, abs=1e-3)


def test_weekly_factor_moves_omits_unavailable_factors(monkeypatch):
    """取れない因子を 0% にすると『動かなかった』と誤読される。"""
    monkeypatch.setattr(ex, "fetch_returns", lambda t, period="2y": {})
    assert ex.weekly_factor_moves(tickers={"market": "^GSPC"}) == {}
