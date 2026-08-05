"""長期金利ゲートと意図的不作為のテスト (改善3 / 改善6).

## 何を縛っているか

1. **意図的不作為はトリガー無しで登録できる** — 「反応しないことが政策の中身」を
   ダミー条件で偽装させない。
2. **金利ゲートは投入側だけを止める** — 売却・撤退を止めると、下落時に売れなくなる
   （危険側の誤り）。
3. **金利が取れないときは blocked にしない** — 取得失敗を理由に投入を止めると、
   データ欠損がそのまま投資判断になる（§16-1 の裏返し）。
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.core.policy import (
    build_policy,
    check_rate_gate,
    evaluate_policy,
    is_entry_policy,
    policy_response,
    rate_state_from_yield_curve,
)
from src.core.policy.ledger import AmbiguousTriggerError

CREATED = datetime(2026, 8, 5, tzinfo=timezone.utc)
TODAY = date(2026, 8, 5)
EXPIRES = "2027-08-05"


def _entry(**kw):
    params = dict(
        symbol="TECL",
        response="第1トランシェ投入（弾の1/3）",
        triggers=[{"metric": "drawdown_pct", "op": "<=", "value": -50}],
        expires_on=EXPIRES,
        intent="awaiting_trigger",
        created_at=CREATED,
    )
    params.update(kw)
    return build_policy(**params)


# ---------------------------------------------------------------------------
# 意図的不作為（改善3）
# ---------------------------------------------------------------------------


class TestDeliberateInaction:
    def test_can_be_registered_without_triggers(self):
        p = build_policy(
            symbol="QCOM", response="決算をまたぐ。事前確定済み", triggers=[],
            expires_on="2026-10-31", intent="deliberate_inaction", created_at=CREATED)
        assert p["triggers"] == []
        assert p["intent"] == "deliberate_inaction"

    def test_other_intents_still_require_a_trigger(self):
        # 条件付きコミットからトリガーを外せてしまうと、政策台帳が空手形になる
        with pytest.raises(AmbiguousTriggerError, match="最低1つ"):
            build_policy(symbol="QCOM", response="売る", triggers=[],
                         expires_on="2026-10-31", intent="conditional_commit",
                         created_at=CREATED)

    def test_evaluates_as_standing_not_unknown(self):
        p = build_policy(
            symbol="QCOM", response="決算をまたぐ", triggers=[],
            expires_on="2026-10-31", intent="deliberate_inaction", created_at=CREATED)
        assessment = evaluate_policy(p, {"price": 175.0}, today=TODAY)
        # 「判定不能」にすると取得失敗と混同される。判定する対象が無いのが正解。
        assert assessment["state"] == "standing"
        assert "意図的不作為" in assessment["label"]

    def test_response_says_do_not_re_decide(self):
        p = build_policy(
            symbol="QCOM", response="決算をまたぐ", triggers=[],
            expires_on="2026-10-31", intent="deliberate_inaction", created_at=CREATED)
        res = policy_response("QCOM", {"price": 175.0}, policies=[p], today=TODAY)
        assert res["has_policy"] is True
        assert "新たに判断せず" in res["answer"]
        assert "条件なし" in res["answer"]

    def test_position_value_metric_is_measurable(self):
        p = build_policy(
            symbol="SOXL", response="1億円到達で全利確",
            triggers=[{"metric": "position_value_jpy", "op": ">=", "value": 100_000_000}],
            expires_on=EXPIRES, intent="deliberate_inaction", created_at=CREATED)
        met = evaluate_policy(p, {"position_value_jpy": 120_000_000}, today=TODAY)
        far = evaluate_policy(p, {"position_value_jpy": 5_000_000}, today=TODAY)
        assert met["state"] == "met"
        assert far["state"] == "far"


# ---------------------------------------------------------------------------
# 長期金利ゲート（改善6）
# ---------------------------------------------------------------------------


class TestEntryDetection:
    def test_entry_policies_are_detected(self):
        assert is_entry_policy({"response": "第1トランシェ投入"}) is True
        assert is_entry_policy({"response": "資金の3%まで取得する"}) is True
        assert is_entry_policy({"response": "下落で積み増し"}) is True

    def test_exit_policies_are_not_gated(self):
        # 売却側を止めると、下落時に売れなくなる。危険側の誤り。
        assert is_entry_policy({"response": "全株売却"}) is False
        assert is_entry_policy({"response": "段階売却を起動する"}) is False


class TestRateGate:
    def test_blocks_entry_when_30y_above_threshold(self):
        gate = check_rate_gate(_entry(), {"ust30y": 5.62, "ust10y": 4.30})
        assert gate["blocked"] is True
        assert any("30年債" in r for r in gate["reasons"])

    def test_blocks_on_one_month_spike_even_if_level_is_ok(self):
        # 政策金利予想が緩んだ日に30年債が急騰する、という形を捉える
        gate = check_rate_gate(_entry(), {"ust30y": 5.10, "ust30y_change_1m": 0.62})
        assert gate["blocked"] is True
        assert any("急騰" in r for r in gate["reasons"])

    def test_allows_entry_when_rates_are_calm(self):
        gate = check_rate_gate(_entry(), {"ust30y": 4.60, "ust10y": 4.10,
                                          "ust30y_change_1m": 0.05})
        assert gate["blocked"] is False
        assert gate["available"] is True

    def test_exit_policy_is_out_of_scope(self):
        exit_policy = _entry(response="全株売却", symbol="9843.T")
        gate = check_rate_gate(exit_policy, {"ust30y": 6.50})
        assert gate["blocked"] is False
        assert "対象外" in gate["note"]

    def test_unleveraged_position_is_out_of_scope(self):
        gate = check_rate_gate(_entry(), {"ust30y": 6.50}, leverage=1)
        assert gate["blocked"] is False
        assert "対象外" in gate["note"]

    def test_missing_rates_do_not_block(self):
        # 取得失敗を理由に投入を止めると、データ欠損が投資判断になる
        gate = check_rate_gate(_entry(), {})
        assert gate["blocked"] is False
        assert gate["available"] is False
        assert "問題なし" in gate["note"]  # 「問題なし ではない」と明示している

    def test_provisional_threshold_is_disclosed(self):
        gate = check_rate_gate(_entry(), {"ust30y": 5.62})
        assert gate["provisional"] is True
        assert "暫定値" in (gate["note"] or "")


class TestRateGateWiring:
    def test_met_entry_trigger_is_held_by_rate_gate(self):
        p = _entry()
        res = policy_response(
            "TECL", {"drawdown_pct": -55, "ust30y": 5.62},
            policies=[p], today=TODAY)
        assert res["rate_blocked"] == [p["id"]]
        assert "見送ります" in res["answer"]
        assert "30年債" in res["answer"]

    def test_met_entry_trigger_passes_when_rates_are_calm(self):
        p = _entry()
        res = policy_response(
            "TECL", {"drawdown_pct": -55, "ust30y": 4.40},
            policies=[p], today=TODAY)
        assert res["rate_blocked"] == []
        assert "応答を実行" in res["answer"]

    def test_exit_policy_is_never_rate_blocked(self):
        p = _entry(response="全株売却", triggers=[{"metric": "price", "op": "<=", "value": 100}])
        res = policy_response("TECL", {"price": 90, "ust30y": 6.5},
                              policies=[p], today=TODAY)
        assert res["rate_blocked"] == []


class TestYieldCurveAdapter:
    def test_maps_dashboard_output(self):
        state = rate_state_from_yield_curve({
            "yields": {"3M": 4.1, "10Y": 4.35, "30Y": 5.05},
            "change_1m": {"10Y": -0.04, "30Y": 0.13},
        })
        assert state["ust10y"] == 4.35
        assert state["ust30y"] == 5.05
        assert state["ust30y_change_1m"] == 0.13

    def test_missing_tenor_is_absent_not_zero(self):
        # None を入れると 0 と誤評価され「金利0%＝安全」という最悪の誤読になる
        state = rate_state_from_yield_curve({"yields": {"10Y": None}, "change_1m": {}})
        assert "ust10y" not in state
        assert "ust30y" not in state

    def test_handles_missing_input(self):
        assert rate_state_from_yield_curve(None) == {}
        assert rate_state_from_yield_curve({}) == {}
