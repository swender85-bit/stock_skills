"""反証条件の点検テスト (土曜設計書 提案8)。

守るべき性質:
- 測定できない条件は保存前に拒否する（書いた気になるだけのものを残さない）
- 条件が複数あるとき、1つでも成立すれば反証（AND にすると間違いを認めない構造になる）
- 指標が取れないときは「問題なし」ではなく「未点検」
- 反証成立は売り推奨を作らない
"""

from __future__ import annotations

import pytest

from src.core.portfolio import falsification as fx


def _holding(**kw):
    base = {
        "symbol": "1111.T", "name": "テスト", "price": 1000.0,
        "pl_pct": 10.0, "weight_pct": 8.0,
        "fundamentals": {"per": 20.0, "pbr": 1.2, "operating_margin": 0.12,
                         "revenue_growth": 0.05, "roe": 0.15},
        "technicals": {"rsi": 55.0},
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# パース
# ---------------------------------------------------------------------------


def test_parse_string_condition():
    c = fx.parse_condition("operating_margin < 8")
    assert c == {"metric": "operating_margin", "op": "<", "value": 8.0,
                 "label": "営業利益率(%)"}


def test_parse_dict_condition():
    c = fx.parse_condition({"metric": "rsi", "op": ">=", "value": 70})
    assert c["metric"] == "rsi" and c["value"] == 70.0


@pytest.mark.parametrize("bad", [
    "業績が悪化したら", "margin < 8", "operating_margin ~ 8",
    "operating_margin < abc", 42, None,
])
def test_parse_rejects_unmeasurable(bad):
    """曖昧な自然文は受け付けない。点検できない条件は害になる。"""
    if bad is None:
        assert fx.parse_conditions(None) == []
        return
    with pytest.raises(fx.InvalidFalsification):
        fx.parse_condition(bad)


def test_parse_conditions_rejects_all_if_one_is_broken():
    with pytest.raises(fx.InvalidFalsification):
        fx.parse_conditions(["operating_margin < 8", "なんか悪化"])


def test_falsification_metrics_extend_policy_metrics_without_changing_them():
    from src.core.policy.ledger import MEASURABLE_METRICS

    assert set(MEASURABLE_METRICS) <= set(fx.FALSIFICATION_METRICS)
    assert "revenue_growth" in fx.FALSIFICATION_METRICS
    assert "revenue_growth" not in MEASURABLE_METRICS, "政策側の集合は変えない"


# ---------------------------------------------------------------------------
# 市場状態
# ---------------------------------------------------------------------------


def test_ratio_fields_are_converted_to_percent():
    """yfinance は 0.12 のような小数で返す。条件は % で書かれるので揃える。"""
    st = fx.market_state_from_holding(_holding())
    assert st["operating_margin"] == pytest.approx(12.0)
    assert st["roe"] == pytest.approx(15.0)


def test_already_percent_values_are_not_double_scaled():
    h = _holding(fundamentals={"operating_margin": 12.0})
    assert fx.market_state_from_holding(h)["operating_margin"] == pytest.approx(12.0)


def test_missing_metrics_are_absent_not_zero():
    """None を 0 として入れると、条件 `< 8` が誤成立する。"""
    h = _holding(fundamentals={}, technicals={})
    st = fx.market_state_from_holding(h)
    assert "operating_margin" not in st
    assert "rsi" not in st


# ---------------------------------------------------------------------------
# 点検
# ---------------------------------------------------------------------------


def _thesis(**kw):
    base = {"symbol": "1111.T", "id": "n1", "content": "営業利益率10%を維持できる"}
    base.update(kw)
    return base


def test_falsified_when_condition_met():
    h = _holding(fundamentals={"operating_margin": 0.072})
    r = fx.check_thesis(_thesis(falsification="operating_margin < 8"),
                        fx.market_state_from_holding(h))
    assert r["falsified"] is True
    assert r["state"] == "met"
    assert "7.2" in r["message"], "桁ノイズを人に見せない"


def test_intact_when_condition_not_met():
    r = fx.check_thesis(_thesis(falsification="operating_margin < 8"),
                        fx.market_state_from_holding(_holding()))
    assert r["falsified"] is False
    assert r["state"] == "far"


def test_any_single_condition_falsifies():
    """OR。全部壊れないと認めない設計は、間違いを認めない構造そのもの。"""
    h = _holding(fundamentals={"operating_margin": 0.20, "revenue_growth": -0.03})
    r = fx.check_thesis(
        _thesis(falsification=["operating_margin < 8", "revenue_growth < 0"]),
        fx.market_state_from_holding(h))
    assert r["falsified"] is True


def test_undefined_falsification_is_flagged():
    r = fx.check_thesis(_thesis(), {})
    assert r["state"] == "undefined"
    assert r["has_falsification"] is False


def test_invalid_falsification_is_flagged_not_silently_ignored():
    r = fx.check_thesis(_thesis(falsification="なんか悪くなったら"), {})
    assert r["state"] == "invalid"
    assert "解釈できません" in r["message"]


def test_unknown_when_metric_unavailable():
    """指標が取れないのを『抵触なし』と言ってはいけない。"""
    h = _holding(fundamentals={})
    r = fx.check_thesis(_thesis(falsification="operating_margin < 8"),
                        fx.market_state_from_holding(h))
    assert r["state"] == "unknown"
    assert r["falsified"] is False
    assert "未点検" in r["message"]


def test_check_all_buckets_results():
    h = _holding(fundamentals={"operating_margin": 0.072})
    theses = {
        "1111.T": [
            _thesis(id="a", falsification="operating_margin < 8"),
            _thesis(id="b"),
            _thesis(id="c", falsification="revenue_growth < 0"),
        ]
    }
    r = fx.check_all([h], theses)
    assert len(r["falsified"]) == 1
    assert len(r["missing"]) == 1
    assert len(r["unchecked"]) == 1  # revenue_growth が無い
    # 条件の無い thesis は「点検した」に数えない（数えると N件点検が嘘になる）
    assert r["checked"] == 2
    assert r["total"] == 3


def test_check_all_note_warns_against_sell_signal():
    h = _holding(fundamentals={"operating_margin": 0.072})
    r = fx.check_all([h], {"1111.T": [_thesis(falsification="operating_margin < 8")]})
    assert "売り推奨ではありません" in r["note"]


def test_check_all_without_theses_is_empty_not_error():
    assert fx.check_all([_holding()], {})["checked"] == 0


# ---------------------------------------------------------------------------
# 保存
# ---------------------------------------------------------------------------


def test_save_note_persists_falsification(tmp_path):
    from src.data.note_manager import load_notes, save_note

    d = str(tmp_path)
    save_note(symbol="1111.T", note_type="thesis", content="テスト",
              falsification="operating_margin < 8", base_dir=d)
    notes = load_notes(symbol="1111.T", note_type="thesis", base_dir=d)
    assert notes[0]["falsification"] == "operating_margin < 8"


def test_save_note_ignores_falsification_for_non_thesis(tmp_path):
    from src.data.note_manager import load_notes, save_note

    d = str(tmp_path)
    save_note(symbol="1111.T", note_type="lesson", content="テスト",
              falsification="operating_margin < 8", base_dir=d)
    assert "falsification" not in load_notes(symbol="1111.T", base_dir=d)[0]
