"""前方イベント脊椎のテスト (土曜設計書 提案4-⑨ 受け入れ基準)。

1. 翌週の保有銘柄の決算日・権利付最終日が正しく列挙される
2. イベント集中度が評価額加重で計算される
3. 政策未定義の決算銘柄が確実に警告される
4. 配当落ちが週次騰落率から分離表示される
5. 決算日変更が前週比で検出される
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.core.risk import forward_events as fe

#: 2026-08-01 は土曜。翌週は 8/3(月)〜8/7(金)。
SATURDAY = date(2026, 8, 1)


def _holding(symbol="1111.T", name="テスト", weight=10.0, **over):
    base = {"symbol": symbol, "name": name, "weight_pct": weight,
            "fundamentals": {}, "technicals": {}}
    base.update(over)
    return base


def _events(**by_symbol):
    out = {}
    for sym, spec in by_symbol.items():
        sym = sym.replace("__", ".")
        out[sym] = {"symbol": sym, "available": True, "source": "test",
                    "fetched_at": "2026-08-01T00:00:00+00:00",
                    "earnings_dates": spec.get("earnings", []),
                    "ex_dividend_date": spec.get("ex_dividend"),
                    "dividend_date": None, "error": None}
    return out


# ---------------------------------------------------------------------------
# 期間
# ---------------------------------------------------------------------------


def test_next_week_range_from_saturday():
    start, end = fe.next_week_range(SATURDAY)
    assert start == date(2026, 8, 3)
    assert end == date(2026, 8, 7)


def test_next_week_range_from_weekday_does_not_crash():
    start, end = fe.next_week_range(date(2026, 8, 5))  # 水曜
    assert start.weekday() == 0
    assert start > date(2026, 8, 5)


def test_prior_business_day_skips_weekend():
    assert fe.prior_business_day(date(2026, 8, 3)) == date(2026, 7, 31)  # 月→金
    assert fe.prior_business_day(date(2026, 8, 5)) == date(2026, 8, 4)


# ---------------------------------------------------------------------------
# カレンダー（受け入れ基準1）
# ---------------------------------------------------------------------------


def test_earnings_inside_next_week_is_listed():
    cal = fe.build_calendar([_holding()], as_of=SATURDAY,
                            events_by_symbol=_events(**{"1111__T": {
                                "earnings": ["2026-08-06"]}}))
    kinds = [e["kind"] for e in cal["events"]]
    assert "earnings" in kinds
    assert cal["events"][0]["day_label"].startswith("木")


def test_earnings_outside_next_week_is_ignored():
    cal = fe.build_calendar([_holding()], as_of=SATURDAY,
                            events_by_symbol=_events(**{"1111__T": {
                                "earnings": ["2026-08-20"]}}))
    assert cal["events"] == []


def test_jp_ex_dividend_becomes_record_date_one_business_day_earlier():
    """日本株は権利付最終日（買うならその日まで）の方が行動に関係する。"""
    cal = fe.build_calendar([_holding()], as_of=SATURDAY,
                            events_by_symbol=_events(**{"1111__T": {
                                "ex_dividend": "2026-08-05"}}))
    ev = cal["events"][0]
    assert ev["kind"] == "ex_dividend"
    assert ev["date"] == "2026-08-04"
    assert ev["ex_date"] == "2026-08-05"
    assert ev["holiday_caveat"] is True, "祝日でずれる可能性を明示する"


def test_us_ex_dividend_uses_the_ex_date_itself():
    cal = fe.build_calendar([_holding(symbol="QCOM", name="Qualcomm")],
                            as_of=SATURDAY,
                            events_by_symbol=_events(QCOM={
                                "ex_dividend": "2026-08-05"}))
    ev = cal["events"][0]
    assert ev["date"] == "2026-08-05"
    assert ev["holiday_caveat"] is False


def test_unavailable_symbols_are_reported_not_treated_as_no_events():
    events = {"SOXL": {"symbol": "SOXL", "available": False,
                       "earnings_dates": [], "error": "取得不可"}}
    cal = fe.build_calendar([_holding(symbol="SOXL", name="SOXL")],
                            as_of=SATURDAY, events_by_symbol=events)
    assert cal["unavailable_symbols"] == ["SOXL"]
    assert "取得できなかった" in cal["note"]


def test_same_symbol_in_two_accounts_is_not_duplicated():
    """特定とNISAで持っているだけで決算が二重に出てはいけない。"""
    holdings = [_holding(symbol="2802.T", name="味の素", weight=10.4),
                _holding(symbol="2802.T", name="味の素", weight=1.0)]
    cal = fe.build_calendar(holdings, as_of=SATURDAY,
                            events_by_symbol=_events(**{"2802__T": {
                                "earnings": ["2026-08-06"]}}))
    earnings = [e for e in cal["events"] if e["kind"] == "earnings"]
    assert len(earnings) == 1
    assert earnings[0]["weight_pct"] == pytest.approx(11.4), "比率は合算する"


def test_small_events_are_folded_not_dropped():
    holdings = [_holding(symbol="A.T", weight=0.2),
                _holding(symbol="B.T", weight=20.0)]
    ev = _events(**{"A__T": {"earnings": ["2026-08-05"]},
                    "B__T": {"earnings": ["2026-08-05"]}})
    cal = fe.build_calendar(holdings, as_of=SATURDAY, events_by_symbol=ev,
                            min_weight_pct=1.0)
    assert len(cal["events"]) == 1
    assert len(cal["folded"]) == 1, "折り畳みは削除ではない"


def test_macro_events_are_never_folded_by_weight():
    moomoo = {"fed_watch": {"next_meeting": "2026-08-05", "top_range": "4.00-4.25",
                            "top_prob": "82%"}}
    cal = fe.build_calendar([], as_of=SATURDAY, events_by_symbol={}, moomoo=moomoo)
    assert [e["kind"] for e in cal["events"]] == ["fomc"]


def test_macro_events_outside_window_are_ignored():
    moomoo = {"economic_events": [{"date": "2026-09-01", "title": "CPI"}]}
    cal = fe.build_calendar([], as_of=SATURDAY, events_by_symbol={}, moomoo=moomoo)
    assert cal["events"] == []


# ---------------------------------------------------------------------------
# イベント集中度（受け入れ基準2）
# ---------------------------------------------------------------------------


def test_concentration_is_value_weighted_not_count_based():
    """小さい保有3件と主力1件は、同じ『件数』でもリスクが違う。"""
    small = fe.build_calendar(
        [_holding(symbol=f"{i}.T", weight=1.0) for i in range(1, 4)],
        as_of=SATURDAY,
        events_by_symbol=_events(**{f"{i}__T": {"earnings": ["2026-08-05"]}
                                    for i in range(1, 4)}))
    big = fe.build_calendar([_holding(symbol="9.T", weight=30.0)], as_of=SATURDAY,
                            events_by_symbol=_events(**{"9__T": {
                                "earnings": ["2026-08-05"]}}))
    assert fe.event_concentration(small)["pct"] < fe.event_concentration(big)["pct"]
    assert fe.event_concentration(big)["level"] == "danger"


def test_concentration_counts_folded_events_too():
    """折り畳んだ小さいイベントも集中度には算入する（見た目で消えても存在する）。"""
    holdings = [_holding(symbol="A.T", weight=0.5),
                _holding(symbol="B.T", weight=20.0)]
    ev = _events(**{"A__T": {"earnings": ["2026-08-05"]},
                    "B__T": {"earnings": ["2026-08-05"]}})
    cal = fe.build_calendar(holdings, as_of=SATURDAY, events_by_symbol=ev)
    assert fe.event_concentration(cal)["pct"] == pytest.approx(20.5)


def test_concentration_zero_when_no_earnings():
    cal = fe.build_calendar([_holding()], as_of=SATURDAY, events_by_symbol={})
    c = fe.event_concentration(cal)
    assert c["pct"] == 0.0 and c["level"] == "ok"


def test_concentration_notes_unweighted_holdings():
    ev = _events(**{"1111__T": {"earnings": ["2026-08-05"]}})
    cal = fe.build_calendar([_holding(weight=None)], as_of=SATURDAY,
                            events_by_symbol=ev, min_weight_pct=0.0)
    assert fe.event_concentration(cal)["unweighted_note"] is not None


# ---------------------------------------------------------------------------
# 政策カバレッジの穴（受け入れ基準3）
# ---------------------------------------------------------------------------


def test_policy_gap_detected_for_earnings_without_policy():
    cal = fe.build_calendar([_holding()], as_of=SATURDAY,
                            events_by_symbol=_events(**{"1111__T": {
                                "earnings": ["2026-08-06"]}}))
    g = fe.policy_coverage_gaps(cal, policies_by_symbol={})
    assert len(g["gaps"]) == 1
    assert "月曜以降は決められません" in g["message"]
    assert "manage_policy.py add" in g["how_to"]


def test_policy_gap_absent_when_policy_exists():
    cal = fe.build_calendar([_holding()], as_of=SATURDAY,
                            events_by_symbol=_events(**{"1111__T": {
                                "earnings": ["2026-08-06"]}}))
    g = fe.policy_coverage_gaps(cal, policies_by_symbol={"1111.T": [{"id": "p1"}]})
    assert g["gaps"] == []
    assert len(g["covered"]) == 1


def test_policy_gap_ignores_non_earnings_events():
    """権利付最終日に政策が無いことは警告しない（決算ギャップとは別物）。"""
    cal = fe.build_calendar([_holding()], as_of=SATURDAY,
                            events_by_symbol=_events(**{"1111__T": {
                                "ex_dividend": "2026-08-05"}}))
    g = fe.policy_coverage_gaps(cal, policies_by_symbol={})
    assert g["gaps"] == []
    assert g["message"] is None


def test_approaching_triggers_classifies_met_and_near():
    policies = [{"id": "p1", "symbol": "1111.T", "response": "全株売却",
                 "triggers": [{"metric": "drawdown_pct", "op": "<=", "value": -25}]}]
    met = fe.approaching_triggers(policies, {"1111.T": {"drawdown_pct": -30}})
    near = fe.approaching_triggers(policies, {"1111.T": {"drawdown_pct": -22}})
    assert len(met["met"]) == 1
    assert len(near["approaching"]) == 1


def test_approaching_triggers_unknown_metric_is_neither():
    policies = [{"id": "p1", "symbol": "1111.T",
                 "triggers": [{"metric": "operating_cf", "op": "<=", "value": 0}]}]
    r = fe.approaching_triggers(policies, {"1111.T": {}})
    assert r["met"] == [] and r["approaching"] == []


# ---------------------------------------------------------------------------
# 配当落ちの分離（受け入れ基準4）
# ---------------------------------------------------------------------------


def test_recent_ex_dividend_is_surfaced():
    as_of = date(2026, 8, 1)
    ex = (as_of - timedelta(days=3)).isoformat()
    h = _holding(week_change_pct=-3.1,
                 fundamentals={"dividend_yield": 0.032})
    r = fe.dividend_drop_adjustments(
        [h], _events(**{"1111__T": {"ex_dividend": ex}}), as_of=as_of)
    assert len(r["items"]) == 1
    assert r["items"][0]["dividend_yield_pct"] == pytest.approx(3.2)
    assert "損失ではありません" in r["items"][0]["note"]


def test_old_ex_dividend_is_ignored():
    as_of = date(2026, 8, 1)
    ex = (as_of - timedelta(days=40)).isoformat()
    r = fe.dividend_drop_adjustments(
        [_holding()], _events(**{"1111__T": {"ex_dividend": ex}}), as_of=as_of)
    assert r["items"] == []
    assert r["message"] is None


def test_percent_dividend_yield_is_not_double_scaled():
    as_of = date(2026, 8, 1)
    h = _holding(fundamentals={"dividend_yield": 3.2})
    r = fe.dividend_drop_adjustments(
        [h], _events(**{"1111__T": {"ex_dividend": as_of.isoformat()}}), as_of=as_of)
    assert r["items"][0]["dividend_yield_pct"] == pytest.approx(3.2)


# ---------------------------------------------------------------------------
# 月曜寄付（予測装置にしない）
# ---------------------------------------------------------------------------


def test_monday_outlook_reports_gap():
    out = fe.monday_outlook([{"symbol": "^N225", "price": 41420.0}],
                            {"nikkei_futures": {"price": 41850.0}})
    assert out["available"] is True
    assert out["gap_pct"] == pytest.approx(1.04, abs=0.01)
    assert "織り込まれています" in out["message"]


def test_monday_outlook_never_claims_prediction():
    out = fe.monday_outlook([{"symbol": "^N225", "price": 41420.0}],
                            {"nikkei_futures": {"price": 41850.0}})
    assert "予測ではなく" in out["disclaimer"]
    assert "的中率は記録しません" in out["disclaimer"]


def test_monday_outlook_says_unavailable_rather_than_neutral():
    out = fe.monday_outlook([{"symbol": "^N225", "price": 41420.0}], {})
    assert out["available"] is False
    assert "取得できず" in out["message"]


# ---------------------------------------------------------------------------
# 日程変更（受け入れ基準5）
# ---------------------------------------------------------------------------


def test_schedule_change_detected():
    prior = {"events": [{"symbol": "1111.T", "kind": "earnings",
                         "date": "2026-08-06"}], "folded": []}
    current = {"events": [{"symbol": "1111.T", "kind": "earnings",
                           "date": "2026-08-11"}], "folded": []}
    r = fe.detect_schedule_changes(current, prior)
    assert r["available"] is True
    assert r["changes"][0]["previous_date"] == "2026-08-06"
    assert r["changes"][0]["current_date"] == "2026-08-11"


def test_schedule_change_detects_disappearance():
    prior = {"events": [{"symbol": "1111.T", "kind": "earnings",
                         "date": "2026-08-06"}], "folded": []}
    r = fe.detect_schedule_changes({"events": [], "folded": []}, prior)
    assert r["changes"][0]["current_date"] is None


def test_schedule_change_says_incomparable_without_prior():
    """前週分が無いことを『変更なし』と言ってはいけない。"""
    r = fe.detect_schedule_changes({"events": [], "folded": []}, None)
    assert r["available"] is False
    assert "初回実行" in r["reason"]


def test_no_change_reported_when_identical():
    cal = {"events": [{"symbol": "1111.T", "kind": "earnings",
                       "date": "2026-08-06"}], "folded": []}
    r = fe.detect_schedule_changes(cal, cal)
    assert r["changes"] == []


# ---------------------------------------------------------------------------
# まとめ
# ---------------------------------------------------------------------------


def test_build_forward_section_survives_fetch_failure(monkeypatch):
    monkeypatch.setattr(fe, "_safe_fetch_events", lambda s: {})
    r = fe.build_forward_section([_holding()], as_of=SATURDAY)
    assert "calendar" in r
    assert r["errors"] == []


def test_build_forward_section_extracts_actionable(monkeypatch):
    monkeypatch.setattr(fe, "_safe_fetch_events",
                        lambda s: _events(**{"1111__T": {
                            "earnings": ["2026-08-06"]}}))
    monkeypatch.setattr(fe, "_load_policies", lambda syms: {})
    r = fe.build_forward_section([_holding(weight=30.0)], as_of=SATURDAY)
    titles = [a["title"] for a in r["actionable"]]
    assert any("対応政策がありません" in t for t in titles)
    assert any("イベント集中度" in t for t in titles)


# ---------------------------------------------------------------------------
# 出力
# ---------------------------------------------------------------------------


def test_formatter_renders_all_parts():
    from src.output.forward_formatter import format_forward_section

    cal = fe.build_calendar([_holding(weight=30.0)], as_of=SATURDAY,
                            events_by_symbol=_events(**{"1111__T": {
                                "earnings": ["2026-08-06"]}}))
    bundle = {
        "calendar": cal,
        "concentration": fe.event_concentration(cal),
        "policy_gaps": fe.policy_coverage_gaps(cal, policies_by_symbol={}),
        "triggers": {"available": True, "met": [], "approaching": [],
                     "message": "なし"},
        "dividend_drops": {"items": [], "message": None},
        "schedule_changes": fe.detect_schedule_changes(cal, None),
        "monday_outlook": fe.monday_outlook([], {}),
        "errors": [],
    }
    text = format_forward_section(bundle)
    assert "翌週の確定イベント" in text
    assert "イベント集中度" in text
    assert "政策カバレッジの穴" in text
    assert "月曜寄付の見通し" in text


def test_formatter_shows_errors_when_material_missing():
    from src.output.forward_formatter import format_forward_section

    text = format_forward_section({"calendar": {}, "errors": ["カレンダー: boom"]})
    assert "取得できませんでした" in text
    assert "boom" in text


def test_compact_formatter_is_one_line():
    from src.output.forward_formatter import format_compact

    cal = fe.build_calendar([_holding(weight=30.0)], as_of=SATURDAY,
                            events_by_symbol=_events(**{"1111__T": {
                                "earnings": ["2026-08-06"]}}))
    line = format_compact({"calendar": cal,
                           "policy_gaps": fe.policy_coverage_gaps(
                               cal, policies_by_symbol={}),
                           "concentration": fe.event_concentration(cal)})
    assert line.startswith("翌週")
    assert "\n" not in line
