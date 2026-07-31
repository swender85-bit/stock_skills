"""模型監査のテスト (土曜設計書 提案10-⑨ 受け入れ基準)。

1. 週次の予測と実現がペアで蓄積される
2. 26週未満では結論を出さない
3. 系統的バイアスが統計的検定を伴って報告される
4. 模型信頼度が下流出力（ストレス損失額等）に伝播する
5. 因子の自動追加が行われない
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from src.core.risk import model_audit as ma


def _rows(n, error=0.0, jitter=0.0, context=None):
    out = []
    start = date(2026, 1, 3)
    for i in range(n):
        predicted = 1.0
        realized = predicted + error + (jitter if i % 2 else -jitter)
        out.append({
            "as_of": (start + timedelta(weeks=i)).isoformat(),
            "predicted_pct": predicted,
            "realized_pct": realized,
            "error_pct": round(realized - predicted, 3),
            "context": context(i) if callable(context) else (context or {}),
        })
    return out


# ---------------------------------------------------------------------------
# 予測
# ---------------------------------------------------------------------------


def test_prediction_is_beta_weighted_sum():
    p = ma.predict_return({"market": 1.2, "usdjpy": -0.5},
                          {"market": 0.8, "usdjpy": -2.1})
    assert p["available"] is True
    assert p["predicted_pct"] == pytest.approx(1.2 * 0.8 + (-0.5) * (-2.1), abs=1e-6)


def test_prediction_reports_unusable_factors():
    p = ma.predict_return({"market": 1.0, "oil": 0.5}, {"market": 1.0})
    assert p["factors_used"] == ["market"]
    assert p["missing_factors"] == ["oil"]


def test_prediction_unavailable_rather_than_zero():
    """使える因子が無いのに 0% と書かない。"""
    assert ma.predict_return({}, {"market": 1.0})["available"] is False
    assert ma.predict_return({"market": 1.0}, {})["available"] is False
    assert ma.predict_return({"oil": 1.0}, {"market": 1.0})["available"] is False


# ---------------------------------------------------------------------------
# 記録（受け入れ基準1）
# ---------------------------------------------------------------------------


def test_record_and_load_roundtrip(tmp_path):
    ma.record_week(1.0, 0.5, as_of="2026-01-03", base_dir=str(tmp_path))
    ma.record_week(2.0, 2.5, as_of="2026-01-10", base_dir=str(tmp_path))
    rows = ma.load_scorecard(str(tmp_path))
    assert len(rows) == 2
    assert rows[0]["error_pct"] == pytest.approx(-0.5)
    assert rows[1]["error_pct"] == pytest.approx(0.5)


def test_record_keeps_incomplete_weeks(tmp_path):
    """欠測も残す。後から『なぜこの週が抜けているか』を追えるように。"""
    ma.record_week(None, 1.0, as_of="2026-01-03", base_dir=str(tmp_path))
    rows = ma.load_scorecard(str(tmp_path))
    assert len(rows) == 1
    assert rows[0]["predicted_pct"] is None
    assert rows[0]["error_pct"] is None


def test_load_tolerates_corrupt_lines(tmp_path):
    p = tmp_path / "scorecard.jsonl"
    p.write_text(json.dumps({"as_of": "2026-01-03", "predicted_pct": 1.0,
                             "realized_pct": 1.0}) + "\nbroken\n",
                 encoding="utf-8")
    assert len(ma.load_scorecard(str(tmp_path))) == 1


def test_load_missing_store_is_empty(tmp_path):
    assert ma.load_scorecard(str(tmp_path / "nope")) == []


# ---------------------------------------------------------------------------
# 採点（受け入れ基準2・3）
# ---------------------------------------------------------------------------


def test_no_conclusion_before_minimum_weeks():
    """週次データは年52点しかない。少数で結論を出すと偶然を欠陥と誤認する。"""
    r = ma.score_model(_rows(10))
    assert r["available"] is False
    assert r["weeks"] == 10
    assert "蓄積中" in r["reason"]


def test_incomplete_pairs_do_not_count_toward_minimum():
    rows = _rows(30)
    for r in rows[:10]:
        r["realized_pct"] = None
    assert ma.score_model(rows)["available"] is False


def test_systematic_bias_is_detected_with_a_test():
    """26週中ほぼ全週で実現が予測を下回るなら、偶然ではない。"""
    r = ma.score_model(_rows(30, error=-1.5))
    assert r["available"] is True
    assert r["systematic_bias"] is True
    assert r["p_value"] < ma.SIGNIFICANCE_P
    assert "過小評価" in r["bias_direction"]
    assert "偶然としては起こりにくい" in r["message"]


def test_symmetric_errors_are_not_reported_as_bias():
    """有意でない乖離は報告しない。"""
    r = ma.score_model(_rows(30, error=0.0, jitter=1.0))
    assert r["available"] is True
    assert r["systematic_bias"] is False
    assert "有意ではありません" in r["message"]


def test_score_reports_r_squared():
    r = ma.score_model(_rows(30, error=0.0, jitter=0.2))
    assert r["r2"] is not None


def test_score_carries_humility_caveat():
    r = ma.score_model(_rows(30, error=-1.0))
    assert "模型が完璧になることはありません" in r["caveat"]


def test_binomial_tail_matches_known_values():
    assert ma._binomial_tail_p(10, 10) == pytest.approx(0.5 ** 10)
    assert ma._binomial_tail_p(0, 10) == pytest.approx(1.0)
    assert ma._binomial_tail_p(5, 10) > 0.5


# ---------------------------------------------------------------------------
# 欠落因子（受け入れ基準5）
# ---------------------------------------------------------------------------


def test_missing_factor_is_only_a_hypothesis():
    """自動で因子を追加しない。人の承認を要する議題にする。"""
    rows = _rows(40, error=0.0,
                 context=lambda i: {"small_cap_week": i % 2 == 0})
    for r in rows:
        if r["context"].get("small_cap_week"):
            r["realized_pct"] -= 2.0
            r["error_pct"] = round(r["realized_pct"] - r["predicted_pct"], 3)

    h = ma.suggest_missing_factor(rows)
    assert h["available"] is True
    assert h["hypotheses"][0]["condition"] == "small_cap_week"
    assert "自動追加されません" in h["note"]


def test_missing_factor_silent_without_evidence():
    h = ma.suggest_missing_factor(_rows(40, error=0.0,
                                        context={"quiet_week": True}))
    assert h["available"] is False
    assert "見つかりませんでした" in h["reason"]


def test_missing_factor_needs_minimum_weeks():
    h = ma.suggest_missing_factor(_rows(5))
    assert h["available"] is False
    assert "蓄積中" in h["reason"]


def test_missing_factor_ignores_rare_conditions():
    rows = _rows(40, error=0.0,
                 context=lambda i: {"rare": i < 2})
    for r in rows[:2]:
        r["error_pct"] = -10.0
    h = ma.suggest_missing_factor(rows)
    assert not any(x["condition"] == "rare" for x in h.get("hypotheses") or [])


# ---------------------------------------------------------------------------
# 信頼度の伝播（受け入れ基準4）
# ---------------------------------------------------------------------------


def test_confidence_propagates_bias_to_downstream_numbers():
    score = ma.score_model(_rows(30, error=-1.0))
    r = ma.propagate_confidence(score, -18.2, "テック暴落シナリオの予測損失")
    assert r["available"] is True
    assert r["adjusted_range"] is not None
    lo, hi = r["adjusted_range"]
    assert lo < -18.2 < hi or lo < hi <= -18.2
    assert "系統的バイアス" in r["note"]


def test_confidence_leaves_unbiased_models_alone():
    score = ma.score_model(_rows(30, error=0.0, jitter=1.0))
    r = ma.propagate_confidence(score, -18.2)
    assert r["adjusted_range"] is None
    assert "有意な系統的バイアスはありません" in r["note"]


def test_confidence_says_unmeasured_before_accumulation():
    """採点前の数値をそのまま信用させない。"""
    r = ma.propagate_confidence(ma.score_model(_rows(3)), -18.2)
    assert r["available"] is False
    assert "そのまま信用しないでください" in r["note"]


def test_confidence_handles_non_numeric_value():
    assert ma.propagate_confidence(ma.score_model(_rows(30)), None)["available"] is False


# ---------------------------------------------------------------------------
# まとめ
# ---------------------------------------------------------------------------


def test_build_records_before_scoring(tmp_path):
    """分析が失敗しても来週の材料は残る。"""
    r = ma.build_model_audit({"market": 1.0}, {"market": 2.0}, 1.5,
                             base_dir=str(tmp_path))
    assert r["prediction"]["predicted_pct"] == pytest.approx(2.0)
    assert r["recorded"] is not None
    assert len(ma.load_scorecard(str(tmp_path))) == 1


def test_build_stores_missing_prediction_as_gap(tmp_path):
    r = ma.build_model_audit({}, {}, 1.5, base_dir=str(tmp_path))
    assert r["prediction"]["available"] is False
    rows = ma.load_scorecard(str(tmp_path))
    assert rows[0]["predicted_pct"] is None
    assert rows[0]["realized_pct"] == pytest.approx(1.5)


def test_build_can_skip_storing(tmp_path):
    ma.build_model_audit({"market": 1.0}, {"market": 1.0}, 1.0,
                         store=False, base_dir=str(tmp_path))
    assert ma.load_scorecard(str(tmp_path)) == []


def test_build_score_is_accumulating_at_first(tmp_path):
    r = ma.build_model_audit({"market": 1.0}, {"market": 1.0}, 1.0,
                             base_dir=str(tmp_path))
    assert r["score"]["available"] is False
    assert "蓄積中" in r["score"]["reason"]
