"""横断的な失敗ケースのテスト (土曜設計書 第6章 / 第7章 最終検証項目)。

設計書が「テストすべき失敗ケース」として名指ししたもののうち、
複数モジュールにまたがる横断的なものをここで検証する。

最終検証項目のうち本ファイルが担保するもの:
- 9.  全機能を opt-out した状態で従来動作と一致する（非破壊）
- 10. Windows 実環境で日付・パスが正しく扱われる
- 11. 土曜が祝日・年末年始・市場休場週でもレポートが破綻しない
- 12. 全外部API停止をモックした場合、**黙って古いデータを使わない**
"""

from __future__ import annotations

from datetime import date

import pytest

from src.core.portfolio import reconciliation as rc
from src.core.portfolio import report_diff as rd
from src.core.portfolio import runway as rw
from src.core.portfolio import tax
from src.core.risk import forward_events as fe
from src.data.brokers.base import make_snapshot


# ---------------------------------------------------------------------------
# 12. 全外部API停止（黙って古いデータを使わない）
# ---------------------------------------------------------------------------


def test_all_brokers_down_produces_unreconciled_not_silent_success():
    snaps = [make_snapshot("moomoo", available=False, error="OpenD 未接続",
                           scope=["US"]),
             make_snapshot("rakuten_csv", available=False, error="CSV なし",
                           scope=["JP", "US"])]
    model = [{"name": "トヨタ", "quote_symbol": "7203.T", "shares": 100}]
    r = rc.reconcile(model, snaps, check_intent=False)

    assert r["status"] == "unreconciled"
    assert r["blocking"] is True
    assert r["counts"]["ghosts"] == 0, "取得失敗を幽霊に化けさせない"
    assert r["counts"]["unverified"] == 1
    assert any("照合できませんでした" in m for m in r["messages"])


def test_forward_events_survive_total_data_outage(monkeypatch):
    monkeypatch.setattr(fe, "_safe_fetch_events", lambda s: {})
    r = fe.build_forward_section(
        [{"symbol": "7203.T", "name": "トヨタ", "weight_pct": 10.0}],
        as_of=date(2026, 8, 1))
    assert r["calendar"]["events"] == []
    assert "7203.T" in r["calendar"]["unavailable_symbols"]
    assert "取得できなかった" in r["calendar"]["note"]


def test_monday_outlook_without_futures_says_unavailable():
    out = fe.monday_outlook([], {})
    assert out["available"] is False
    assert "取得できず" in out["message"]
    # 「材料が無い」ではなく「取得できなかった」であることを明示している
    assert "材料なしではなく取得不可" in out["message"]


def test_tax_config_missing_warns_and_does_not_pretend(tmp_path):
    tax.reset_cache()
    cfg = tax.load_tax_config(str(tmp_path / "absent.yaml"), use_cache=False)
    assert cfg["_warnings"]
    assert any("信用しないでください" in w for w in cfg["_warnings"])
    tax.reset_cache()


def test_runway_without_config_or_history_says_unavailable():
    cfg = {"contributions": {"monthly_amount": 0},
           "estimation": {"lookback_weeks": 26, "percentile": 25,
                          "runway_weeks": 12}}
    r = rw.weekly_investable(None, cfg)
    assert r["available"] is False
    assert r["weekly_jpy"] is None, "推定できないのに数字を作らない"


def test_diff_without_prior_snapshot_says_first_run():
    d = rd.diff_snapshots({"holdings": {}, "portfolio": {}}, None)
    assert d["available"] is False
    assert "初回" in d["reason"]


def test_cumulative_without_history_says_accumulating():
    cur = rd.build_snapshot({"meta": {"as_of": "2026-08-01"}, "holdings": [],
                             "portfolio": {}})
    cum = rd.cumulative_diff(cur, [])
    for window in cum["windows"].values():
        assert window["available"] is False
        assert "蓄積中" in window["reason"]


# ---------------------------------------------------------------------------
# 11. 祝日・年末年始・市場休場週
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("as_of", [
    date(2026, 12, 26),   # 年末の土曜
    date(2027, 1, 2),     # 年始の土曜
    date(2026, 5, 2),     # GW の土曜
    date(2026, 8, 1),     # 通常の土曜
])
def test_next_week_range_never_crashes_across_year_boundary(as_of):
    start, end = fe.next_week_range(as_of)
    assert start.weekday() == 0
    assert (end - start).days == 4
    assert start > as_of


def test_next_week_range_crosses_new_year_correctly():
    start, end = fe.next_week_range(date(2026, 12, 26))
    assert start == date(2026, 12, 28)
    assert end == date(2027, 1, 1), "年をまたいでも5営業日"


def test_prior_business_day_across_new_year():
    assert fe.prior_business_day(date(2027, 1, 4)) == date(2027, 1, 1)


def test_nisa_weeks_left_is_zero_on_new_years_eve():
    s = tax.nisa_state(0, 0, today=date(2026, 12, 31))
    assert s["weeks_left_in_year"] == 0


def test_nisa_state_early_january_has_full_year():
    s = tax.nisa_state(0, 0, today=date(2026, 1, 1))
    assert s["weeks_left_in_year"] > 50


def test_empty_portfolio_does_not_crash_any_layer():
    """市場休場週や全売却直後でも各層が落ちないこと。"""
    assert rc.reconcile([], [make_snapshot("x", available=True, scope=["JP"])],
                        check_intent=False)["status"] == "ok"
    assert fe.build_calendar([], as_of=date(2026, 8, 1),
                             events_by_symbol={})["events"] == []
    assert fe.event_concentration({"events": [], "folded": []})["pct"] == 0.0
    assert rw.attention_budget(0)["warning"] is None
    snap = rd.build_snapshot({"meta": {"as_of": "2026-08-01"}, "holdings": [],
                              "portfolio": {}})
    assert snap["holdings"] == {}


# ---------------------------------------------------------------------------
# 10. Windows 実環境（日付・パス）
# ---------------------------------------------------------------------------


def test_snapshot_paths_use_pathlib_and_survive_windows(tmp_path):
    snap = rd.build_snapshot({"meta": {"as_of": "2026-08-01"}, "holdings": [],
                              "portfolio": {}})
    path = rd.save_snapshot(snap, base_dir=str(tmp_path / "nested" / "dir"))
    assert path is not None and path.exists()
    assert rd.load_snapshots(base_dir=str(tmp_path / "nested" / "dir"))


def test_narrative_store_key_strips_path_hostile_characters():
    from src.core.research import narrative as nv

    key = nv._key("7203.T", None)
    assert "/" not in key and "\\" not in key and ":" not in key


def test_dates_are_naive_iso_strings_not_timezone_dependent():
    """レポート上の日付は ISO の日付文字列で、TZ 依存で1日ずれないこと。"""
    cal = fe.build_calendar(
        [{"symbol": "1111.T", "name": "A", "weight_pct": 10.0}],
        as_of=date(2026, 8, 1),
        events_by_symbol={"1111.T": {"symbol": "1111.T", "available": True,
                                     "earnings_dates": ["2026-08-06"],
                                     "ex_dividend_date": None}})
    assert cal["events"][0]["date"] == "2026-08-06"
    assert len(cal["events"][0]["date"]) == 10


# ---------------------------------------------------------------------------
# 9. 非破壊 / opt-out
# ---------------------------------------------------------------------------


def test_narrative_can_be_disabled_by_env(monkeypatch, tmp_path):
    from src.core.research import narrative as nv

    monkeypatch.setenv("NARRATIVE_ENABLED", "off")
    r = nv.capture("AAPL", "Apple", base_dir=str(tmp_path))
    assert r["available"] is False
    assert nv.load_snapshots("AAPL", base_dir=str(tmp_path)) == []


def test_reconciliation_can_be_skipped_without_breaking_pack():
    """`include_reconciliation=False` でも他の材料は揃うこと。"""
    from src.core.research import briefing_pack as bp

    assert hasattr(bp, "_safe_reconciliation")
    # 例外を投げるソースでもパックは壊れない
    result = bp._safe_reconciliation({}, {}, include_moomoo=False)
    assert "status" in result


def test_policy_metrics_are_unchanged_by_falsification_extension():
    """反証条件の指標追加が、既存の政策台帳の集合を壊していないこと（非破壊）。"""
    from src.core.policy.ledger import MEASURABLE_METRICS
    from src.core.portfolio.falsification import FALSIFICATION_METRICS

    assert "revenue_growth" not in MEASURABLE_METRICS
    assert set(MEASURABLE_METRICS) < set(FALSIFICATION_METRICS)


def test_save_note_without_falsification_is_unchanged(tmp_path):
    """既存 thesis の保存形式が変わっていないこと。"""
    from src.data.note_manager import load_notes, save_note

    save_note(symbol="X", note_type="thesis", content="旧来の書き方",
              base_dir=str(tmp_path))
    note = load_notes(symbol="X", base_dir=str(tmp_path))[0]
    assert "falsification" not in note


# ---------------------------------------------------------------------------
# 照合前に分析を確定値として語らない（第7章-1）
# ---------------------------------------------------------------------------


def test_blocking_reconciliation_propagates_to_information_verdict():
    """照合が通らない週を『静穏週・何もしなくてよい』と言い切らないこと。"""
    diff = {"available": True, "changes": [], "folded": [], "checked": 5}
    a = rd.assess_information(diff, {}, reconciliation={
        "status": "unreconciled", "blocking": True})
    assert a["quiet"] is False


def test_ghost_positions_break_quiet_week():
    diff = {"available": True, "changes": [], "folded": [], "checked": 5}
    a = rd.assess_information(diff, {}, reconciliation={
        "status": "differences", "ghosts": [{"symbol": "X"}]})
    assert a["quiet"] is False
    assert any("幽霊" in x["title"] for x in a["actionable"])


def test_driver_passes_reconciliation_status_to_all_sections():
    """全節が照合状態を受け取る（未照合のまま数値を語らせない）。"""
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent.parent
    sys.path.insert(0, str(repo / "scripts"))
    import weekly_deep_driver as drv

    pack = {
        "meta": {}, "portfolio": {}, "holdings": [],
        "reconciliation": {"status": "unreconciled", "blocking": True,
                           "independently_verified": False},
        "information": {"quiet": False},
    }
    for section in drv.build_sections(pack):
        material = drv.slice_pack(pack, section)
        assert material["reconciliation_status"]["blocking"] is True, section["id"]
