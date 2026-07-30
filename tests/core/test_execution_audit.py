"""執行監査のテスト (土曜設計書 提案5-⑨ 受け入れ基準)。

1. 約定履歴が判断と紐付く
2. 決定生存率が正しく計算され、未執行判断が滞留する
3. 執行ショートフォールが判断時刻基準で計算される
4. 部分約定・分割執行が正しく集約される
5. 成績表が執行済みと未執行で分離され、混ざらない
"""

from __future__ import annotations

import pytest

from src.core.portfolio import execution_audit as ea


def _decision(id="d1", symbol="AAPL", side="BUY", shares=10, price=100.0,
              decided_at="2026-07-01"):
    return {"id": id, "symbol": symbol, "side": side, "shares": shares,
            "price": price, "decided_at": decided_at}


def _execution(symbol="AAPL", side="BUY", shares=10, price=102.0,
               executed_at="2026-07-03"):
    return {"symbol": symbol, "side": side, "shares": shares, "price": price,
            "executed_at": executed_at}


# ---------------------------------------------------------------------------
# マッチング（受け入れ基準1・4）
# ---------------------------------------------------------------------------


def test_decision_matches_later_execution():
    r = ea.match_decisions([_decision()], [_execution()])
    assert len(r["matched"]) == 1
    assert r["matched"][0]["delay_days"] == 2
    assert r["unmatched_decisions"] == []


def test_execution_before_decision_is_not_matched():
    """判断より前の約定を紐付けると、後付けで成績を作れてしまう。"""
    r = ea.match_decisions([_decision(decided_at="2026-07-10")],
                           [_execution(executed_at="2026-07-01")])
    assert r["matched"] == []
    assert len(r["unmatched_decisions"]) == 1


def test_execution_outside_window_is_not_matched():
    r = ea.match_decisions([_decision(decided_at="2026-01-01")],
                           [_execution(executed_at="2026-07-01")])
    assert r["matched"] == []


def test_opposite_side_is_not_matched():
    r = ea.match_decisions([_decision(side="BUY")], [_execution(side="SELL")])
    assert r["matched"] == []


def test_closest_execution_wins_when_multiple_candidates():
    execs = [_execution(executed_at="2026-07-20"),
             _execution(executed_at="2026-07-02")]
    r = ea.match_decisions([_decision()], execs)
    assert r["matched"][0]["delay_days"] == 1


def test_each_execution_is_consumed_only_once():
    """同一銘柄への複数判断が同じ約定を二重に食わないこと。"""
    decisions = [_decision(id="d1"), _decision(id="d2")]
    r = ea.match_decisions(decisions, [_execution()])
    assert len(r["matched"]) == 1
    assert len(r["unmatched_decisions"]) == 1


def test_symbol_normalization_matches_across_formats():
    r = ea.match_decisions([_decision(symbol="7203.T")],
                           [_execution(symbol="JP.7203")])
    assert len(r["matched"]) == 1


def test_unmatched_executions_are_kept_as_independent_trades():
    """システム外の独断売買は成績に混ぜないが、存在は示す。"""
    r = ea.match_decisions([], [_execution(symbol="NVDA")])
    assert len(r["unmatched_executions"]) == 1


def test_decision_without_timestamp_is_not_forced():
    r = ea.match_decisions([{"id": "x", "symbol": "AAPL"}], [_execution()])
    assert r["matched"] == []
    assert "不明" in r["unmatched_decisions"][0]["reason"]


# ---------------------------------------------------------------------------
# 決定生存率（受け入れ基準2）
# ---------------------------------------------------------------------------


def test_survival_rate_computed():
    r = ea.match_decisions([_decision(id="a"), _decision(id="b", symbol="MSFT")],
                           [_execution()])
    s = ea.survival_rate(r)
    assert s["executed"] == 1 and s["unexecuted"] == 1
    assert s["rate_pct"] == pytest.approx(50.0)


def test_low_survival_blames_the_system_not_the_user():
    """トーンの設計: 責任の所在をシステム側に置く。"""
    r = ea.match_decisions([_decision(id=str(i), symbol=f"S{i}")
                            for i in range(10)], [])
    s = ea.survival_rate(r)
    assert s["rate_pct"] == 0.0
    assert "怠慢ではなく" in s["message"]
    assert "実行可能でなかった" in s["message"]


def test_high_survival_omits_the_blame_message():
    r = ea.match_decisions([_decision()], [_execution()])
    assert "怠慢" not in ea.survival_rate(r)["message"]


def test_survival_unavailable_without_decisions():
    s = ea.survival_rate({"matched": [], "unmatched_decisions": []})
    assert s["available"] is False
    assert "判断の記録から始めて" in s["reason"]


def test_survival_carries_caveat_about_virtual_pnl():
    r = ea.match_decisions([_decision()], [_execution()])
    assert "資産には反映されていません" in ea.survival_rate(r)["caveat"]


def test_survival_refuses_to_compute_without_execution_history():
    """約定履歴が取れないだけなのに『執行率0%』と断定しない。

    取得失敗を結果と混同する典型的な誤り。実データで実際に踏んだので固定する。
    """
    r = ea.match_decisions([_decision(id=str(i), symbol=f"S{i}")
                            for i in range(46)], [])
    s = ea.survival_rate(r, executions_available=False)
    assert s["available"] is False
    assert s["executed"] is None
    assert "測定できていない" in s["reason"]


def test_build_audit_marks_unmeasurable_when_broker_is_down(monkeypatch):
    monkeypatch.setattr(ea, "_load_decisions", lambda days: ([_decision()], None))
    monkeypatch.setattr(ea, "_load_executions",
                        lambda days: ([], "約定履歴を取得できません: OpenD 未接続"))
    r = ea.build_execution_audit()
    assert r["executions_available"] is False
    assert r["survival"]["available"] is False
    assert "0%" not in str(r["survival"].get("rate_pct"))


def test_build_audit_measures_when_executions_are_genuinely_empty(monkeypatch):
    """約定が本当に0件だったときは、生存率0%を出してよい。"""
    monkeypatch.setattr(ea, "_load_decisions", lambda days: ([_decision()], None))
    monkeypatch.setattr(ea, "_load_executions", lambda days: ([], None))
    r = ea.build_execution_audit()
    assert r["executions_available"] is True
    assert r["survival"]["rate_pct"] == 0.0


# ---------------------------------------------------------------------------
# 未執行理由の推定
# ---------------------------------------------------------------------------


def test_funding_constraint_inferred_from_cash_history():
    r = ea.match_decisions([_decision(shares=100, price=1000.0)], [])
    reasons = ea.infer_unexecuted_reasons(r, {"2026-07-01": 10_000.0})
    assert len(reasons["funding_constrained"]) == 1
    assert any("現金残高の制約を課すべき" in m for m in reasons["messages"])


def test_not_convinced_inferred_when_cash_was_sufficient():
    r = ea.match_decisions([_decision(shares=1, price=10.0)], [])
    reasons = ea.infer_unexecuted_reasons(r, {"2026-07-01": 1_000_000.0})
    assert len(reasons["not_convinced"]) == 1


def test_reason_unknown_without_cash_history():
    """憶測を数字にしない。"""
    r = ea.match_decisions([_decision()], [])
    reasons = ea.infer_unexecuted_reasons(r, None)
    assert len(reasons["unknown"]) == 1
    assert reasons["funding_constrained"] == []


def test_reasons_always_marked_as_estimates():
    r = ea.match_decisions([_decision()], [])
    assert "推定です" in ea.infer_unexecuted_reasons(r, None)["caveat"]


# ---------------------------------------------------------------------------
# 執行ショートフォール（受け入れ基準3）
# ---------------------------------------------------------------------------


def test_buying_higher_than_decision_price_is_unfavorable():
    r = ea.match_decisions([_decision(price=100.0)], [_execution(price=105.0)])
    assert r["matched"][0]["shortfall_pct"] == pytest.approx(-5.0)


def test_selling_lower_than_decision_price_is_unfavorable():
    """符号を揃えないと、買い売り混在で摩擦が相殺されて消える。"""
    r = ea.match_decisions([_decision(side="SELL", price=100.0)],
                           [_execution(side="SELL", price=95.0)])
    assert r["matched"][0]["shortfall_pct"] == pytest.approx(-5.0)


def test_shortfall_aggregates_median_delay():
    decisions = [_decision(id="a", symbol="A"), _decision(id="b", symbol="B"),
                 _decision(id="c", symbol="C")]
    execs = [_execution(symbol="A", executed_at="2026-07-02"),
             _execution(symbol="B", executed_at="2026-07-04"),
             _execution(symbol="C", executed_at="2026-07-11")]
    s = ea.execution_shortfall(ea.match_decisions(decisions, execs))
    assert s["median_delay_days"] == 3


def test_shortfall_points_to_policy_as_the_fix():
    """分析の改良では回収できない損失であることを明示する。"""
    r = ea.match_decisions([_decision(price=100.0)], [_execution(price=110.0)])
    s = ea.execution_shortfall(r)
    assert s["avg_shortfall_pct"] < 0
    assert "政策台帳" in s["message"]


def test_shortfall_unavailable_without_matches():
    s = ea.execution_shortfall({"matched": []})
    assert s["available"] is False


def test_buy_dip_note_when_buys_lag_sells():
    decisions = ([_decision(id=f"b{i}", symbol=f"B{i}", side="BUY")
                  for i in range(3)]
                 + [_decision(id="s1", symbol="S1", side="SELL")])
    execs = ([_execution(symbol=f"B{i}", side="BUY", executed_at="2026-07-08")
              for i in range(3)]
             + [_execution(symbol="S1", side="SELL", executed_at="2026-07-02")])
    s = ea.execution_shortfall(ea.match_decisions(decisions, execs))
    assert s["buy_dip_note"] is not None
    assert "押し目が終わってから" in s["buy_dip_note"]


def test_buy_dip_note_absent_with_too_few_samples():
    s = ea.execution_shortfall(ea.match_decisions([_decision()], [_execution()]))
    assert s["buy_dip_note"] is None


# ---------------------------------------------------------------------------
# 成績の分離（受け入れ基準5）
# ---------------------------------------------------------------------------


def test_performance_is_split_and_never_merged():
    match = ea.match_decisions(
        [_decision(id="done"), _decision(id="skipped", symbol="MSFT")],
        [_execution()])
    perf = ea.split_performance(match, {"done": 50_000.0, "skipped": 200_000.0})

    assert perf["executed"]["total_pnl"] == 50_000.0
    assert perf["unexecuted_virtual"]["total_pnl"] == 200_000.0
    assert "合算した『精度』は架空" in perf["note"]


def test_performance_unavailable_without_outcomes():
    match = ea.match_decisions([_decision()], [_execution()])
    perf = ea.split_performance(match, {})
    assert perf["executed"]["available"] is False


# ---------------------------------------------------------------------------
# まとめ
# ---------------------------------------------------------------------------


def test_build_audit_with_explicit_inputs():
    r = ea.build_execution_audit([_decision()], [_execution()])
    assert r["survival"]["rate_pct"] == 100.0
    assert r["errors"] == []
    assert "怠慢ではなく" in r["tone"]


def test_build_audit_records_errors_when_sources_fail(monkeypatch):
    monkeypatch.setattr(ea, "_load_decisions",
                        lambda days: ([], "判断履歴を読めません"))
    monkeypatch.setattr(ea, "_load_executions",
                        lambda days: ([], "約定履歴を取得できません"))
    r = ea.build_execution_audit()
    assert len(r["errors"]) == 2
    assert r["survival"]["available"] is False


def test_side_normalization():
    assert ea._side("買い") == "BUY"
    assert ea._side("SELL") == "SELL"
    assert ea._side("???") is None
