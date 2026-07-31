"""限界寄与スクリーニングのテスト (土曜設計書 提案2-⑨ 受け入れ基準)。

1. 同一銘柄が、異なるポートフォリオに対して異なる限界スコアを返す
2. 既存保有と相関0.85超の候補が、確実に減点され警告される
3. ETF保有時のルックスルー比率が正しく計算される
4. 因子推定が不安定な銘柄にフラグが立つ
5. `--standalone` で従来と完全一致した結果が出る（非破壊）
"""

from __future__ import annotations

import pytest

from src.core.screening import marginal as mg


def _pf(**betas):
    return {"available": True, "betas": betas}


def _cand(symbol="X", score=80.0, name=None):
    return {"symbol": symbol, "name": name or symbol, "value_score": score}


def _exposure(unstable_factors=None, low_r2=False, **betas):
    return {"available": True, "betas": betas,
            "unstable": bool(unstable_factors) or low_r2,
            "unstable_factors": list(unstable_factors or []),
            "low_r2": low_r2}


# ---------------------------------------------------------------------------
# 補完係数
# ---------------------------------------------------------------------------


def test_opposite_exposure_raises_the_factor():
    comp = mg.complement_factor({"usdjpy": -0.8}, {"usdjpy": 0.9})
    assert comp["factor"] > 1.0
    assert comp["contributions"][0]["effect"] == "補完"


def test_same_direction_exposure_lowers_the_factor():
    comp = mg.complement_factor({"semis": 1.5}, {"semis": 1.6})
    assert comp["factor"] < 1.0
    assert comp["contributions"][0]["effect"] == "増幅"


def test_untilted_factors_are_ignored():
    """偏っていない方向を埋めても分散は改善しない。"""
    comp = mg.complement_factor({"oil": 2.0}, {"oil": 0.05})
    assert comp["factor"] == 1.0
    assert comp["contributions"] == []


def test_factor_is_bounded():
    """因子推定の誤差が順位を支配しないよう上下限で止める。"""
    hi = mg.complement_factor({"semis": -5.0}, {"semis": 5.0})
    lo = mg.complement_factor({"semis": 5.0}, {"semis": 5.0})
    assert hi["factor"] <= mg.COMPLEMENT_MAX
    assert lo["factor"] >= mg.COMPLEMENT_MIN


def test_unstable_factors_are_discounted_individually():
    """『原油が反転した』を理由に半導体の評価まで捨てない。"""
    full = mg.complement_factor({"semis": 1.5}, {"semis": 1.6})
    damped = mg.complement_factor({"semis": 1.5}, {"semis": 1.6},
                                  unstable_factors=["semis"])
    assert abs(damped["factor"] - 1.0) < abs(full["factor"] - 1.0)
    assert damped["contributions"][0]["unstable"] is True


def test_unrelated_unstable_factor_does_not_damp_others():
    a = mg.complement_factor({"semis": 1.5}, {"semis": 1.6})
    b = mg.complement_factor({"semis": 1.5}, {"semis": 1.6},
                             unstable_factors=["oil"])
    assert a["factor"] == b["factor"]


def test_complement_unavailable_without_pf_exposure():
    comp = mg.complement_factor({"semis": 1.0}, {})
    assert comp["available"] is False
    assert comp["factor"] == 1.0, "評価できないことを理由に順位を動かさない"


# ---------------------------------------------------------------------------
# 限界スコア（受け入れ基準1・4）
# ---------------------------------------------------------------------------


def test_same_stock_scores_differently_for_different_portfolios():
    """設計書の中核: 同じ銘柄でも保有者が違えばスコアが違う。"""
    cand = _cand("NVDA", 90.0)
    exposure = _exposure(semis=1.6)

    semi_heavy = mg.marginal_score(cand, _pf(semis=1.6), exposure)
    semi_free = mg.marginal_score(cand, _pf(semis=-1.2), exposure)

    assert semi_heavy["marginal_score"] < semi_free["marginal_score"]
    assert semi_heavy["standalone_score"] == semi_free["standalone_score"]


def test_mediocre_complement_can_beat_excellent_duplicate():
    """65点が90点を上回りうること（従来出力し得なかった結論）。"""
    strong = mg.marginal_score(_cand("DUP", 90.0), _pf(semis=1.8),
                               _exposure(semis=1.9))
    mediocre = mg.marginal_score(_cand("COMP", 68.0), _pf(semis=1.8),
                                 _exposure(semis=-1.5))
    assert mediocre["marginal_score"] > strong["marginal_score"]


def test_quality_floor_blocks_recommendation():
    """分散のための分散で低品質銘柄を買わせない。"""
    r = mg.marginal_score(_cand("LOW", 45.0), _pf(semis=1.8),
                          _exposure(semis=-2.0))
    assert r["below_quality_floor"] is True
    assert r["recommendable"] is False
    assert "分散のための分散" in r["floor_note"]


def test_unstable_exposure_is_flagged():
    r = mg.marginal_score(_cand(), _pf(semis=1.5),
                          _exposure(unstable_factors=["semis"], semis=1.0))
    assert r["unstable_exposure"] is True
    assert any("符号が反転" in w for w in r["warnings"])


def test_low_r2_produces_explicit_warning():
    r = mg.marginal_score(_cand(), _pf(semis=1.5),
                          _exposure(low_r2=True, semis=1.0))
    assert any("説明力が低く" in w for w in r["warnings"])


def test_missing_standalone_score_is_unavailable():
    r = mg.marginal_score({"symbol": "X"}, _pf(semis=1.0), _exposure(semis=1.0))
    assert r["available"] is False


def test_score_without_exposure_falls_back_to_standalone():
    r = mg.marginal_score(_cand("X", 80.0), _pf(semis=1.5), None)
    assert r["marginal_score"] == pytest.approx(80.0)
    assert r["complement_factor"] == 1.0


# ---------------------------------------------------------------------------
# 因子双子（受け入れ基準2）
# ---------------------------------------------------------------------------


def test_factor_twin_forces_a_heavy_discount():
    twins = [{"symbol": "HELD", "correlation": 0.92,
              "message": "保有中の HELD と相関 0.92"}]
    r = mg.marginal_score(_cand("TWIN", 90.0), _pf(semis=1.5),
                          _exposure(semis=-1.5), twins=twins)
    assert r["complement_factor"] <= 0.45
    assert any("因子双子" in w or "買い増し" in w for w in r["warnings"])


def test_stress_correlation_divergence_warns():
    """平時相関だけで『分散した』と判断させない。"""
    r = mg.marginal_score(
        _cand(), _pf(semis=1.5), _exposure(semis=1.0),
        stress_correlations={"HELD": {"calm": 0.11, "stress": 0.68}})
    assert any("ストレス時" in w for w in r["warnings"])


def test_stress_correlation_silent_when_stable():
    r = mg.marginal_score(
        _cand(), _pf(semis=1.5), _exposure(semis=1.0),
        stress_correlations={"HELD": {"calm": 0.30, "stress": 0.35}})
    assert not any("ストレス時" in w for w in r["warnings"])


# ---------------------------------------------------------------------------
# 並べ替え（受け入れ基準5 / 非破壊）
# ---------------------------------------------------------------------------


def test_ranking_falls_back_to_standalone_when_degraded():
    """因子が取れない環境では従来の単独スコア順に戻る（非破壊）。"""
    cands = [_cand("A", 70.0), _cand("B", 90.0)]
    r = mg.rank_candidates(cands, {"available": False}, {})
    assert r["degraded"] is True
    assert r["sorted_by"] == "standalone"
    assert [x["symbol"] for x in r["ranked"]] == ["B", "A"]


def test_ranking_uses_marginal_when_available():
    cands = [_cand("DUP", 90.0), _cand("COMP", 70.0)]
    exposures = {"DUP": _exposure(semis=1.9), "COMP": _exposure(semis=-1.6)}
    r = mg.rank_candidates(cands, _pf(semis=1.8), exposures)
    assert r["sorted_by"] == "marginal"
    assert r["ranked"][0]["symbol"] == "COMP"


def test_below_floor_candidates_sink_to_the_bottom():
    cands = [_cand("LOW", 40.0), _cand("OK", 65.0)]
    exposures = {"LOW": _exposure(semis=-2.0), "OK": _exposure(semis=0.1)}
    r = mg.rank_candidates(cands, _pf(semis=1.8), exposures)
    assert r["ranked"][-1]["symbol"] == "LOW"


def test_ranking_keeps_unscored_separate():
    r = mg.rank_candidates([{"symbol": "NOSCORE"}], _pf(semis=1.0), {})
    assert r["ranked"] == []
    assert len(r["unscored"]) == 1


def test_ranking_preserves_source_rows():
    cands = [_cand("A", 70.0)]
    r = mg.rank_candidates(cands, _pf(semis=1.0), {})
    assert r["ranked"][0]["source_row"] is cands[0]


# ---------------------------------------------------------------------------
# ルックスルー（受け入れ基準3）
# ---------------------------------------------------------------------------


def test_lookthrough_sums_direct_and_etf_exposure():
    holdings = [{"symbol": "AAPL", "weight_pct": 4.1},
                {"symbol": "VOO", "weight_pct": 30.0}]
    etf = {"VOO": {"AAPL": 0.123}}
    r = mg.lookthrough_exposure(holdings, etf)
    assert r["available"] is True
    assert r["effective"]["AAPL"]["effective_pct"] == pytest.approx(7.79, abs=0.02)
    assert "AAPL" in r["hidden_amplification"]


def test_lookthrough_unavailable_without_components():
    """構成情報が無いのに『分散している』と誤読させない。"""
    r = mg.lookthrough_exposure([{"symbol": "VOO", "weight_pct": 30.0}], None)
    assert r["available"] is False
    assert "実態より大きい可能性" in r["reason"]


def test_lookthrough_ignores_non_etf_holdings():
    r = mg.lookthrough_exposure([{"symbol": "AAPL", "weight_pct": 5.0}],
                                {"VOO": {"AAPL": 0.1}})
    assert r["effective"]["AAPL"]["via_etf_pct"] == 0.0


# ---------------------------------------------------------------------------
# 出力
# ---------------------------------------------------------------------------


def test_formatter_renders_tilt_and_ranking():
    from src.output.marginal_formatter import format_marginal_section

    pf = _pf(semis=1.6, usdjpy=0.7)
    cands = [_cand("DUP", 90.0), _cand("COMP", 70.0)]
    exposures = {"DUP": _exposure(semis=1.9), "COMP": _exposure(semis=-1.6)}
    ranked = mg.rank_candidates(cands, pf, exposures)
    text = format_marginal_section(pf, ranked, tilt_lines=["円安に傾斜"])

    assert "因子偏り" in text
    assert "限界スコア順" in text
    assert "保有者が違えばスコアが違います" in text


def test_formatter_says_unmeasured_rather_than_unbiased():
    from src.output.marginal_formatter import format_portfolio_tilt

    text = format_portfolio_tilt({"available": False, "reason": "因子取得失敗"})
    assert "測れていません" in text


def test_bridge_degrades_without_holdings():
    from src.core.screening.marginal_bridge import build_marginal_view, render

    view = build_marginal_view([_cand()], holdings=[])
    assert view["available"] is False
    assert "単独スコアで表示" in view["reason"]
    assert "限界寄与" in render(view)


def test_bridge_returns_empty_render_when_no_reason():
    from src.core.screening.marginal_bridge import render

    assert render({"available": False}) == ""
