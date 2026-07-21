"""政策台帳のテスト -- Fable5 第2弾 案A.

受け入れ基準:
  P1 登録・改訂・失効が動作する
  P2 急変系の照会で政策が(分析ではなく)先に提示される
  P4 擬似ティックでトリガー判定が正しく発火・非発火する
  P5 逸脱が検出される
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from src.core.policy import (
    AmbiguousTriggerError,
    CoolingPeriodError,
    build_policy,
    coverage_rate,
    detect_deviations,
    evaluate_policy,
    evaluate_trigger,
    expire_policy,
    list_policies,
    load_policy,
    policy_response,
    record_deviation,
    revise_policy,
    save_policy,
    trigger_distance,
    validate_trigger,
)
from src.core.policy.ledger import is_expired


CREATED = datetime(2026, 7, 18, tzinfo=timezone.utc)
TODAY = date(2026, 7, 18)
EXPIRES = "2027-01-31"


def _policy(**kw):
    params = dict(
        symbol="7203.T",
        response="全株売却",
        triggers=[{"metric": "drawdown_pct", "op": "<=", "value": -25}],
        expires_on=EXPIRES,
        created_at=CREATED,
    )
    params.update(kw)
    return build_policy(**params)


class TestTriggerValidation:
    """曖昧政策の登録拒否 = 将来の自分が従えるかの審査。"""

    def test_free_text_trigger_is_rejected(self):
        with pytest.raises(AmbiguousTriggerError, match="自由文"):
            validate_trigger("なんか下がったら")

    def test_unknown_metric_is_rejected(self):
        with pytest.raises(AmbiguousTriggerError, match="測定不能"):
            validate_trigger({"metric": "雰囲気", "op": "<=", "value": -10})

    def test_bad_operator_is_rejected(self):
        with pytest.raises(AmbiguousTriggerError, match="演算子"):
            validate_trigger({"metric": "rsi", "op": "≒", "value": 70})

    def test_non_numeric_threshold_is_rejected(self):
        with pytest.raises(AmbiguousTriggerError, match="数値"):
            validate_trigger({"metric": "rsi", "op": ">", "value": "かなり高い"})

    def test_valid_trigger_is_normalized(self):
        t = validate_trigger({"metric": "rsi", "op": ">", "value": "70"})
        assert t == {"metric": "rsi", "op": ">", "value": 70.0}

    def test_contradictory_triggers_are_rejected(self):
        with pytest.raises(AmbiguousTriggerError, match="矛盾"):
            _policy(triggers=[
                {"metric": "drawdown_pct", "op": "<=", "value": -25},
                {"metric": "drawdown_pct", "op": ">=", "value": -5},
            ])


class TestPolicyCreation:
    """案A P1 + 無期限政策の禁止。"""

    def test_expiry_is_mandatory(self):
        with pytest.raises(ValueError, match="無期限"):
            _policy(expires_on=None)

    def test_past_expiry_is_rejected(self):
        with pytest.raises(ValueError, match="過去"):
            _policy(expires_on="2026-01-01")

    def test_empty_response_is_rejected(self):
        with pytest.raises(ValueError, match="応答"):
            _policy(response="  ")

    def test_policy_requires_at_least_one_trigger(self):
        with pytest.raises(AmbiguousTriggerError, match="最低1つ"):
            _policy(triggers=[])

    def test_policy_carries_market_and_status(self):
        p = _policy()
        assert p["market"] == "JP"
        assert p["status"] == "active"
        assert p["revision"] == 1

    def test_deliberate_inaction_is_a_valid_intent(self):
        """『見ない/動かない』も一級の意思決定状態。"""
        p = _policy(response="何もしない", intent="deliberate_inaction")
        assert p["intent"] == "deliberate_inaction"


class TestExpiry:
    def test_policy_expires_after_its_date(self):
        p = _policy()
        assert is_expired(p, date(2026, 12, 1)) is False
        assert is_expired(p, date(2027, 3, 1)) is True

    def test_explicit_expiry_marks_status(self):
        p = expire_policy(_policy(), reason="テーゼ変更")
        assert p["status"] == "expired"
        assert is_expired(p, TODAY) is True


class TestTriggerEvaluation:
    """案A P4: 擬似ティックで正しく発火・非発火する。"""

    def test_trigger_fires_when_threshold_crossed(self):
        t = {"metric": "drawdown_pct", "op": "<=", "value": -25}
        assert evaluate_trigger(t, {"drawdown_pct": -30})["state"] == "met"

    def test_trigger_does_not_fire_before_threshold(self):
        t = {"metric": "drawdown_pct", "op": "<=", "value": -25}
        assert evaluate_trigger(t, {"drawdown_pct": -6})["state"] == "far"

    def test_near_state_between_far_and_met(self):
        t = {"metric": "drawdown_pct", "op": "<=", "value": -25}
        assert evaluate_trigger(t, {"drawdown_pct": -22})["state"] == "near"

    def test_missing_metric_is_unknown_not_fired(self):
        """データ源停止中に『成立』と誤判定しないこと。"""
        t = {"metric": "operating_cf", "op": "<", "value": 0}
        assert evaluate_trigger(t, {"drawdown_pct": -30})["state"] == "unknown"

    def test_distance_is_zero_when_met(self):
        t = {"metric": "rsi", "op": ">", "value": 70}
        assert trigger_distance(t, {"rsi": 80}) == 0.0

    def test_distance_measures_remaining_gap(self):
        t = {"metric": "rsi", "op": ">", "value": 70}
        assert trigger_distance(t, {"rsi": 65}) == 5.0

    def test_multiple_triggers_any_met_makes_policy_met(self):
        p = _policy(triggers=[
            {"metric": "drawdown_pct", "op": "<=", "value": -25},
            {"metric": "rsi", "op": "<", "value": 20},
        ])
        result = evaluate_policy(p, {"drawdown_pct": -30, "rsi": 50}, TODAY)
        assert result["state"] == "met"
        assert len(result["met_triggers"]) == 1

    def test_simultaneous_triggers_are_all_reported(self):
        p = _policy(triggers=[
            {"metric": "drawdown_pct", "op": "<=", "value": -25},
            {"metric": "rsi", "op": "<", "value": 20},
        ])
        result = evaluate_policy(p, {"drawdown_pct": -30, "rsi": 15}, TODAY)
        assert len(result["met_triggers"]) == 2


class TestPolicyResponse:
    """案A P2 / 具体例12: 分析ではなく政策参照が返る。"""

    def test_no_policy_reports_absence(self):
        result = policy_response("AAPL", {"drawdown_pct": -6}, policies=[])
        assert result["has_policy"] is False
        assert "有効な政策がありません" in result["answer"]

    def test_unmet_trigger_answers_hold(self):
        """『日経-3%。トヨタ売るべき？』に対して現状維持を政策から返す。"""
        result = policy_response("7203.T", {"drawdown_pct": -6}, policies=[_policy()])
        assert result["has_policy"] is True
        assert "条件不成立" in result["answer"]
        assert "現状維持" in result["answer"]
        assert "全株売却" in result["answer"]

    def test_met_trigger_answers_execute(self):
        result = policy_response("7203.T", {"drawdown_pct": -30}, policies=[_policy()])
        assert "成立しています" in result["answer"]
        assert result["requires_cooling"] is True

    def test_near_trigger_warns_about_cooling_and_deviation(self):
        result = policy_response("7203.T", {"drawdown_pct": -22}, policies=[_policy()])
        assert "接近中" in result["answer"]
        assert "冷却期間" in result["answer"]
        assert "逸脱" in result["answer"]
        assert result["requires_cooling"] is True

    def test_expired_policy_is_not_applied(self):
        """失効済み政策の誤適用を防ぐ。"""
        expired = expire_policy(_policy())
        result = policy_response("7203.T", {"drawdown_pct": -30}, policies=[expired])
        assert result["has_policy"] is False
        assert len(result["expired_policies"]) == 1


class TestRevision:
    """平時の柔軟性と有事の拘束の分離。"""

    def test_calm_revision_is_free(self):
        p = _policy()
        revise_policy(p, response="半分売却", market_state={"drawdown_pct": -3})
        assert p["response"] == "半分売却"
        assert p["revision"] == 2

    def test_revision_without_market_state_is_free(self):
        p = _policy()
        revise_policy(p, response="半分売却")
        assert p["revision"] == 2

    def test_revision_near_trigger_hits_cooling_period(self):
        p = _policy()
        with pytest.raises(CoolingPeriodError, match="冷却期間"):
            revise_policy(p, response="やっぱり保有", market_state={"drawdown_pct": -22})
        assert p["response"] == "全株売却", "冷却期間中は改訂されない"

    def test_revision_while_trigger_met_hits_cooling_period(self):
        p = _policy()
        with pytest.raises(CoolingPeriodError, match="成立"):
            revise_policy(p, response="やっぱり保有", market_state={"drawdown_pct": -30})

    def test_revision_allowed_after_cooling_period_elapses(self):
        p = _policy()
        with pytest.raises(CoolingPeriodError):
            revise_policy(
                p, response="やっぱり保有",
                market_state={"drawdown_pct": -30},
                at=datetime(2026, 7, 18, 0, 0, tzinfo=timezone.utc),
            )
        revise_policy(
            p, response="やっぱり保有",
            market_state={"drawdown_pct": -30},
            at=datetime(2026, 7, 19, 1, 0, tzinfo=timezone.utc),
        )
        assert p["response"] == "やっぱり保有"

    def test_force_bypasses_cooling(self):
        p = _policy()
        revise_policy(p, response="強制改訂", market_state={"drawdown_pct": -30}, force=True)
        assert p["response"] == "強制改訂"

    def test_revision_history_records_the_change_of_mind(self):
        p = _policy()
        revise_policy(p, response="半分売却", rationale="想定変更")
        assert len(p["history"]) == 1
        assert p["history"][0]["response"] == "全株売却"

    def test_revision_rejects_ambiguous_triggers(self):
        p = _policy()
        with pytest.raises(AmbiguousTriggerError):
            revise_policy(p, triggers=["適当に下がったら"])

    def test_extending_expiry_reactivates(self):
        p = expire_policy(_policy())
        revise_policy(p, expires_on="2028-01-01")
        assert p["status"] == "active"


class TestPersistence:
    def test_save_load_roundtrip(self, tmp_path):
        p = _policy()
        save_policy(p, base_dir=str(tmp_path))
        assert load_policy(p["id"], base_dir=str(tmp_path))["response"] == "全株売却"

    def test_list_hides_expired_by_default(self, tmp_path):
        active = _policy()
        dead = expire_policy(_policy())
        save_policy(active, base_dir=str(tmp_path))
        save_policy(dead, base_dir=str(tmp_path))

        assert len(list_policies(base_dir=str(tmp_path), today=TODAY)) == 1
        assert len(list_policies(base_dir=str(tmp_path), active_only=False)) == 2

    def test_list_missing_dir_returns_empty(self, tmp_path):
        assert list_policies(base_dir=str(tmp_path / "nope")) == []


class TestCoverageAndDeviation:
    """案A P5: カバレッジ率と逸脱監査。"""

    def test_coverage_counts_policy_backed_holdings(self):
        holdings = [
            {"symbol": "7203.T", "value": 1000},
            {"symbol": "AAPL", "value": 3000},
        ]
        result = coverage_rate(holdings, policies=[_policy()], today=TODAY)
        assert result["covered_count"] == 1
        assert result["count_rate"] == 0.5
        assert result["value_rate"] == 0.25
        assert result["uncovered_symbols"] == ["AAPL"]

    def test_coverage_on_empty_portfolio(self):
        result = coverage_rate([], policies=[], today=TODAY)
        assert result["count_rate"] == 0.0

    def test_selling_without_trigger_is_a_deviation(self):
        devs = detect_deviations(
            trades=[{"symbol": "7203.T", "action": "sell"}],
            policies=[_policy()],
            market_states={"7203.T": {"drawdown_pct": -6}},
            today=TODAY,
        )
        assert len(devs) == 1
        assert devs[0]["kind"] == "acted_without_trigger"

    def test_ignoring_a_met_trigger_is_a_deviation(self):
        devs = detect_deviations(
            trades=[],
            policies=[_policy()],
            market_states={"7203.T": {"drawdown_pct": -30}},
            today=TODAY,
        )
        assert len(devs) == 1
        assert devs[0]["kind"] == "ignored_trigger"

    def test_following_the_policy_is_not_a_deviation(self):
        devs = detect_deviations(
            trades=[{"symbol": "7203.T", "action": "sell"}],
            policies=[_policy()],
            market_states={"7203.T": {"drawdown_pct": -30}},
            today=TODAY,
        )
        assert devs == []

    def test_missing_market_data_yields_no_false_deviation(self):
        devs = detect_deviations(
            trades=[{"symbol": "7203.T", "action": "sell"}],
            policies=[_policy()],
            market_states={},
            today=TODAY,
        )
        assert devs == []

    def test_deviation_becomes_a_process_lesson(self):
        """逸脱は過程の誤り = 案Bの制約になる資格を持つ。"""
        devs = detect_deviations(
            trades=[{"symbol": "7203.T", "action": "sell"}],
            policies=[_policy()],
            market_states={"7203.T": {"drawdown_pct": -6}},
            today=TODAY,
        )
        lesson = record_deviation(devs[0])
        assert lesson["origin"] == "process"

        from src.core.lesson_gate import is_constraint_eligible

        assert is_constraint_eligible(lesson) is True
