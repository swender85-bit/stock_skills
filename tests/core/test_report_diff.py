"""情報量比例レポートのテスト (土曜設計書 提案8-⑨ 受け入れ基準)。

1. 前週レポートとの差分が全項目で計算される
2. 変化のない週に、レポートが実際に短くなる
3. 4週・13週の累積差分が別途監視され、緩慢な変化を検出する
4. thesis の反証条件が点検され、成立時に最上位に表示される
5. 折り畳まれた項目が失われず、全て展開可能である
"""

from __future__ import annotations

import pytest

from src.core.portfolio import report_diff as rd


def _pack(price=100.0, weight=10.0, per=20.0, day="2026-08-01",
          orphans=0, rec_status="ok"):
    return {
        "meta": {"as_of": day},
        "portfolio": {"total_jpy": 1_000_000, "cash_jpy": 0, "pl_pct": 5.0},
        "holdings": [{
            "name": "テスト", "symbol": "1111.T", "price": price,
            "weight_pct": weight, "pl_pct": 5.0,
            "fundamentals": {"per": per, "pbr": 1.0},
            "technicals": {"rsi": 50.0},
        }],
        "reconciliation": {"status": rec_status,
                           "counts": {"orphans": orphans, "ghosts": 0,
                                      "unrecorded": 0}},
    }


# ---------------------------------------------------------------------------
# スナップショット
# ---------------------------------------------------------------------------


def test_snapshot_aggregates_weight_across_accounts():
    pack = _pack()
    pack["holdings"].append({**pack["holdings"][0], "weight_pct": 5.0})
    snap = rd.build_snapshot(pack)
    assert snap["holdings"]["sym:1111"]["weight_pct"] == 15.0


def test_snapshot_roundtrip(tmp_path):
    snap = rd.build_snapshot(_pack())
    rd.save_snapshot(snap, base_dir=str(tmp_path))
    loaded = rd.load_snapshots(base_dir=str(tmp_path))
    assert len(loaded) == 1
    assert loaded[0]["holdings"]["sym:1111"]["price"] == 100.0


def test_load_snapshots_skips_corrupt_files(tmp_path):
    (tmp_path / "20260101.json").write_text("not json", encoding="utf-8")
    rd.save_snapshot(rd.build_snapshot(_pack()), base_dir=str(tmp_path))
    assert len(rd.load_snapshots(base_dir=str(tmp_path))) == 1


def test_prior_snapshot_returns_none_rather_than_wrong_week():
    """4週前が無いときに2週前で代用すると、緩慢な変化の検出が壊れる。"""
    snaps = [rd.build_snapshot(_pack(day=f"2026-08-0{i}")) for i in (1, 2)]
    assert rd.prior_snapshot(snaps, weeks_back=4, today="2026-08-03") is None
    assert rd.prior_snapshot(snaps, weeks_back=1, today="2026-08-03")["date"] == "2026-08-02"


# ---------------------------------------------------------------------------
# 差分（受け入れ基準1・5）
# ---------------------------------------------------------------------------


def test_diff_reports_first_run_without_crashing():
    d = rd.diff_snapshots(rd.build_snapshot(_pack()), None)
    assert d["available"] is False
    assert "初回" in d["reason"]


def test_significant_price_change_is_surfaced():
    prev = rd.build_snapshot(_pack(price=100.0, day="2026-07-25"))
    cur = rd.build_snapshot(_pack(price=120.0))
    d = rd.diff_snapshots(cur, prev)
    labels = [c["label"] for c in d["changes"]]
    assert any("株価" in x for x in labels)


def test_small_change_is_folded_not_deleted():
    """折り畳みは削除ではない。展開できる形で残す。"""
    prev = rd.build_snapshot(_pack(price=100.0, day="2026-07-25"))
    cur = rd.build_snapshot(_pack(price=101.0))
    d = rd.diff_snapshots(cur, prev)
    assert not any("株価" in c["label"] for c in d["changes"])
    assert any("株価" in c["label"] for c in d["folded"])
    assert d["checked"] == len(d["changes"]) + len(d["folded"])


def test_new_and_gone_holdings_are_always_significant():
    prev = rd.build_snapshot(_pack(day="2026-07-25"))
    cur = rd.build_snapshot(_pack())
    cur["holdings"]["sym:2222"] = {"name": "新規", "symbol": "2222.T",
                                   "weight_pct": 3.0}
    del cur["holdings"]["sym:1111"]
    d = rd.diff_snapshots(cur, prev)
    kinds = {c["kind"] for c in d["changes"]}
    assert "holding_new" in kinds and "holding_gone" in kinds


def test_reconciliation_count_change_is_never_folded():
    """孤児が1件増えたことは、閾値で潰してはいけない。"""
    prev = rd.build_snapshot(_pack(orphans=0, day="2026-07-25"))
    cur = rd.build_snapshot(_pack(orphans=1))
    d = rd.diff_snapshots(cur, prev)
    assert any(c["key"] == "reconciliation.orphans" and c["significant"]
               for c in d["changes"])


def test_missing_values_do_not_create_fake_changes():
    prev = rd.build_snapshot(_pack(per=None, day="2026-07-25"))
    cur = rd.build_snapshot(_pack(per=None))
    d = rd.diff_snapshots(cur, prev)
    assert not any("PER" in c["label"] for c in d["changes"] + d["folded"])


# ---------------------------------------------------------------------------
# 累積差分（受け入れ基準3）
# ---------------------------------------------------------------------------


def test_cumulative_reports_missing_history_instead_of_guessing():
    snaps = [rd.build_snapshot(_pack(day="2026-07-25"))]
    cum = rd.cumulative_diff(rd.build_snapshot(_pack()), snaps)
    assert cum["windows"]["4w"]["available"] is False
    assert "蓄積中" in cum["windows"]["4w"]["reason"]


def test_cumulative_detects_slow_drift_invisible_week_to_week():
    """毎週2%ずつ下げると週次閾値(5%)には一度も引っかからないが、
    4週で8%になる。ここを見逃すのが差分レポートの最大の欠陥。"""
    snaps = []
    for i, price in enumerate([100.0, 98.0, 96.0, 94.0], start=1):
        snaps.append(rd.build_snapshot(_pack(price=price, day=f"2026-07-0{i}")))
    cur = rd.build_snapshot(_pack(price=92.0, day="2026-07-05"))

    weekly = rd.diff_snapshots(cur, snaps[-1])
    assert not any("株価" in c["label"] for c in weekly["changes"]), \
        "週次では引っかからない前提のテスト"

    cum = rd.cumulative_diff(cur, snaps)
    assert cum["windows"]["4w"]["available"] is True
    assert any("株価" in d["label"] for d in cum["slow_drift"])


# ---------------------------------------------------------------------------
# 情報量判定（受け入れ基準2・4）
# ---------------------------------------------------------------------------


def test_quiet_week_is_a_valid_output():
    d = {"available": True, "changes": [], "folded": [], "checked": 12}
    a = rd.assess_information(d, {}, reconciliation={"status": "ok"})
    assert a["quiet"] is True
    assert a["verdict"] == "静穏週"
    assert "何もしないことが正しい" in a["guidance"]


def test_falsified_thesis_ranks_first():
    d = {"available": True, "changes": [], "folded": [], "checked": 5}
    a = rd.assess_information(
        d, {},
        falsified=[{"symbol": "1111.T", "message": "営業利益率が7.2%へ低下"}],
        reconciliation={"status": "ok", "orphans": [{"symbol": "2222.T"}]})
    assert a["quiet"] is False
    assert a["actionable"][0]["kind"] == "belief"


def test_unreconciled_is_never_quiet():
    """照合できていない週を『何もしなくてよい』と言い切ってはいけない。"""
    d = {"available": True, "changes": [], "folded": [], "checked": 3}
    a = rd.assess_information(d, {}, reconciliation={"status": "unreconciled"})
    assert a["quiet"] is False


def test_circular_reconciliation_is_not_quiet():
    d = {"available": True, "changes": [], "folded": [], "checked": 3}
    a = rd.assess_information(
        d, {}, reconciliation={"status": "circular",
                               "independently_verified": False})
    assert a["quiet"] is False
    assert any("独立検証" in x["title"] for x in a["actionable"])


def test_orphans_break_quiet_week():
    d = {"available": True, "changes": [], "folded": [], "checked": 3}
    a = rd.assess_information(
        d, {}, reconciliation={"status": "ok", "orphans": [{"symbol": "1111.T"}],
                               "orphan_burden_pct": 77.6})
    assert a["quiet"] is False
    assert "77.6%" in a["actionable"][0]["title"]


def test_missing_falsification_breaks_quiet_week():
    """反証条件の無いテーゼを静穏扱いすると、未点検が『問題なし』に化ける。"""
    d = {"available": True, "changes": [], "folded": [], "checked": 3}
    a = rd.assess_information(
        d, {}, falsification={"missing": [{"symbol": "1111.T"}]},
        reconciliation={"status": "ok"})
    assert a["quiet"] is False
    assert any("反証条件が未定義" in x["title"] for x in a["actionable"])


def test_unchecked_thesis_is_reported_as_unchecked_not_ok():
    d = {"available": True, "changes": [], "folded": [], "checked": 3}
    a = rd.assess_information(
        d, {}, falsification={"unchecked": [{"symbol": "1111.T"}]},
        reconciliation={"status": "ok"})
    assert any("未点検" in (x.get("detail") or "") for x in a["actionable"])


# ---------------------------------------------------------------------------
# 出力
# ---------------------------------------------------------------------------


def test_quiet_report_is_short():
    from src.output.weekly_diff_formatter import format_verdict

    d = {"available": True, "changes": [], "folded": [], "checked": 12,
         "previous_date": "2026-07-25"}
    a = rd.assess_information(d, {}, reconciliation={"status": "ok"})
    text = format_verdict(a, d, {})
    assert "静穏週" in text
    assert len(text.splitlines()) < 20, "静穏週は実際に短くなければならない"
    assert "12項目を点検" in text, "動作していることは必ず示す"


def test_busy_report_lists_actions():
    from src.output.weekly_diff_formatter import format_verdict

    d = {"available": True, "changes": [], "folded": [], "checked": 5,
         "previous_date": "2026-07-25"}
    a = rd.assess_information(
        d, {}, falsified=[{"symbol": "1111.T", "message": "反証成立"}],
        reconciliation={"status": "ok"})
    text = format_verdict(a, d, {})
    assert "要対応週" in text
    assert "【信念】" in text
