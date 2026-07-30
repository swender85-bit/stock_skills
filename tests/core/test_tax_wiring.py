"""税引後の必須添付が実際に配線されているかのテスト (土曜設計書 提案3-⑨-1)。

受け入れ基準1: **全ての乗り換え提案に switching_hurdle が添付される**。
これはモジュール単体では担保できない。what-if / adjust の出力で確認する。
"""

from __future__ import annotations

import pytest

from src.core.portfolio import portfolio_simulation as ps
from src.core.portfolio.adjustment_advisor import (
    Action,
    ActionType,
    Urgency,
    attach_tax_and_funding,
)


def _pos(**over):
    base = {
        "symbol": "2802.T", "name": "味の素", "shares": 400,
        "cost_price": 3906.03, "current_price": 5102.0,
        "market_currency": "JPY", "evaluation": 400 * 5102.0,
        "evaluation_jpy": 400 * 5102.0,
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# what-if（スワップ）
# ---------------------------------------------------------------------------


def test_removal_gets_after_tax_view():
    rem = {"symbol": "2802.T", "shares": 400, "proceeds_jpy": 400 * 5102.0}
    ps._attach_tax_view(rem, _pos(), {"JPY": 1.0})

    assert rem["tax_available"] is True
    assert rem["net_proceeds_jpy"] < rem["proceeds_jpy"]
    assert rem["tax_jpy"] > 0
    assert rem["switching_hurdle_pct"] > 0
    assert "損益分岐" in rem["tax_note"]


def test_removal_uses_fx_for_foreign_stock():
    rem = {"symbol": "QCOM", "shares": 85, "proceeds_jpy": 85 * 175.0 * 160.0}
    ps._attach_tax_view(
        rem, _pos(symbol="QCOM", shares=85, cost_price=100.0, current_price=175.0,
                  market_currency="USD"), {"JPY": 1.0, "USD": 160.0})
    assert rem["tax_available"] is True
    assert rem["fx_cost_jpy"] > 0, "為替スプレッドを落とすと判定が楽観側に外れる"


def test_removal_marks_unavailable_rather_than_faking_net():
    """税引前の数字を黙って手取りとして扱わない。"""
    rem = {"symbol": "X", "shares": 10}
    ps._attach_tax_view(rem, {"symbol": "X"}, {"JPY": 1.0})
    assert rem["tax_available"] is False
    assert "net_proceeds_jpy" not in rem


def test_nisa_removal_has_no_tax():
    rem = {"symbol": "2802.T", "shares": 39, "proceeds_jpy": 39 * 5102.0}
    ps._attach_tax_view(rem, _pos(shares=39, account="NISA成長"), {"JPY": 1.0})
    assert rem["tax_jpy"] == 0.0
    assert rem["switching_hurdle_pct"] == pytest.approx(0.0, abs=0.01)


def test_blended_hurdle_across_multiple_removals():
    rows = [{"proceeds_jpy": 1_000_000, "net_proceeds_jpy": 800_000},
            {"proceeds_jpy": 1_000_000, "net_proceeds_jpy": 900_000}]
    assert ps._blended_hurdle(rows) == pytest.approx(17.65, abs=0.05)


def test_blended_hurdle_none_when_incomputable():
    assert ps._blended_hurdle([{"proceeds_jpy": 100}]) is None
    assert ps._blended_hurdle([]) is None


def test_sum_key_ignores_missing_values():
    assert ps._sum_key([{"a": 1}, {"a": None}, {}], "a") == 1
    assert ps._sum_key([], "a") is None


# ---------------------------------------------------------------------------
# adjust（処方箋）
# ---------------------------------------------------------------------------


def _action(kind=ActionType.SELL, target="2802.T"):
    return Action(type=kind, target=target, urgency=Urgency.HIGH,
                  reasons=["テスト"], rule_ids=["P1"])


def test_sell_action_gets_tax_view():
    actions = [_action()]
    attach_tax_and_funding(actions, [_pos()])
    t = actions[0].tax_view
    assert t["available"] is True
    assert t["switching_hurdle_pct"] > 0
    assert "税務助言ではありません" in t["disclaimer"]


def test_swap_action_gets_tax_view():
    actions = [_action(kind=ActionType.SWAP)]
    attach_tax_and_funding(actions, [_pos()])
    assert actions[0].tax_view["available"] is True


@pytest.mark.parametrize("kind", [ActionType.ADD, ActionType.FLAG,
                                  ActionType.TRIM_CLASS])
def test_non_sell_actions_get_no_tax_view(kind):
    actions = [_action(kind=kind, target="small_cap")]
    attach_tax_and_funding(actions, [_pos()])
    assert actions[0].tax_view is None


def test_sell_action_without_position_is_flagged():
    actions = [_action(target="UNKNOWN")]
    attach_tax_and_funding(actions, [_pos()])
    assert actions[0].tax_view["available"] is False
    assert "見つからず" in actions[0].tax_view["note"]


def test_sell_action_gets_funding_alternative():
    """受け入れ基準2: 売却を伴う提案に入金代替案が併記される。"""
    actions = [_action()]
    attach_tax_and_funding(actions, [_pos()])
    f = actions[0].funding_alternative
    assert f is not None
    assert "入金" in f["note"]


def test_fx_rate_is_derived_from_jpy_evaluation_when_absent():
    """fx_rate が無くても円建て評価額から逆算する（USD建てを1倍で扱わない）。"""
    pos = _pos(symbol="QCOM", shares=85, cost_price=100.0, current_price=175.0,
               market_currency="USD", evaluation=85 * 175.0,
               evaluation_jpy=85 * 175.0 * 160.0)
    actions = [_action(target="QCOM")]
    attach_tax_and_funding(actions, [pos])
    t = actions[0].tax_view
    assert t["gross_jpy"] == pytest.approx(85 * 175.0 * 160.0, rel=1e-6)


# ---------------------------------------------------------------------------
# 出力
# ---------------------------------------------------------------------------


def test_adjust_formatter_renders_tax_section():
    from src.core.portfolio.adjustment_advisor import AdjustmentPlan
    from src.core.portfolio.market_regime import MarketRegime
    from src.output.adjust_formatter import format_adjustment_plan

    actions = [_action()]
    attach_tax_and_funding(actions, [_pos()])
    plan = AdjustmentPlan(
        regime=MarketRegime(regime="neutral", sma50_above_200=True, rsi=55.0,
                            drawdown=-0.05, index_symbol="^GSPC"),
        actions=actions, candidates={}, summary="1 HIGH")
    text = format_adjustment_plan(plan)
    assert "税引後の再評価" in text
    assert "損益分岐" in text
    assert "入金代替" in text


def test_adjust_formatter_omits_tax_section_without_sell_actions():
    from src.core.portfolio.adjustment_advisor import AdjustmentPlan
    from src.core.portfolio.market_regime import MarketRegime
    from src.output.adjust_formatter import format_adjustment_plan

    actions = [_action(kind=ActionType.FLAG, target="small_cap")]
    attach_tax_and_funding(actions, [_pos()])
    plan = AdjustmentPlan(
        regime=MarketRegime(regime="neutral", sma50_above_200=True, rsi=55.0,
                            drawdown=-0.05, index_symbol="^GSPC"),
        actions=actions, candidates={}, summary="1 HIGH")
    assert "税引後の再評価" not in format_adjustment_plan(plan)


def test_simulate_formatter_shows_after_tax_columns():
    from src.output.simulate_formatter import format_what_if

    rem = {"symbol": "2802.T", "shares": 400, "proceeds_jpy": 2_040_800.0}
    ps._attach_tax_view(rem, _pos(), {"JPY": 1.0})
    result = {
        "proposed": [], "removals": [rem], "removed_health": [],
        "proceeds_jpy": 2_040_800.0, "net_cash_jpy": 2_040_800.0,
        "net_proceeds_after_tax_jpy": rem["net_proceeds_jpy"],
        "tax_friction_jpy": rem["friction_jpy"],
        "net_cash_after_tax_jpy": rem["net_proceeds_jpy"],
        "switching_hurdle_pct": rem["switching_hurdle_pct"],
        "tax_note": "手取りで表示しています。",
        "before": {}, "after": {}, "proposed_health": [],
        "required_cash_jpy": 0.0, "judgment": {},
    }
    text = format_what_if(result)
    assert "手取り（税引後）" in text
    assert "乗り換え損益分岐" in text


def test_simulate_formatter_warns_when_tax_uncomputable():
    from src.output.simulate_formatter import format_what_if

    rem = {"symbol": "X", "shares": 1, "proceeds_jpy": 100.0,
           "tax_available": False, "tax_note": "計算不能"}
    result = {
        "proposed": [], "removals": [rem], "removed_health": [],
        "proceeds_jpy": 100.0, "net_cash_jpy": 100.0,
        "before": {}, "after": {}, "proposed_health": [],
        "required_cash_jpy": 0.0, "judgment": {},
    }
    text = format_what_if(result)
    assert "算出不可" in text
    assert "手取りとして扱わないでください" in text
