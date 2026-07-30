"""週次レポートの固定骨格テスト (土曜設計書 第3章 / 第7章 最終検証項目)。

守るべき性質:
1. セクション順序が意思決定論的に固定されている（照合が最初、機会は制約の後）
2. 照合を通らずに分析セクションが数値を確定値として語らない（照合状態が全節に渡る）
3. 変化のない週にレポートが実際に短くなる（静穏週で銘柄別を展開しない）
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import weekly_deep_driver as drv  # noqa: E402


def _pack(quiet=False, holdings=None, rec_status="ok"):
    holdings = holdings if holdings is not None else [
        {"symbol": "2802.T", "name": "味の素", "weight_pct": 11.4},
        {"symbol": "SOXL", "name": "SOXL", "weight_pct": 31.0},
    ]
    return {
        "meta": {"as_of": "2026-08-01", "warnings": []},
        "portfolio": {"total_jpy": 23_000_000, "total_pl_jpy": 5_000_000},
        "holdings": holdings,
        "reconciliation": {
            "status": rec_status, "blocking": rec_status != "ok",
            "independently_verified": rec_status == "ok",
            "counts": {"model": 2, "broker": 2, "orphans": 1},
            "messages": ["テスト"],
            "orphans": [{"symbol": "SOXL"}],
        },
        "falsification": {"falsified": [], "near": [], "unchecked": [],
                          "missing": [], "intact": 2, "checked": 2},
        "forward": {"calendar": {"events": [], "folded": [],
                                 "unavailable_symbols": []},
                    "actionable": [], "errors": []},
        "constraints": {"tax_state": {}, "runway_bundle": {}, "attention": {},
                        "loss_harvest": []},
        "week_diff": {"available": True, "changes": [], "folded": [], "checked": 9},
        "cumulative_diff": {"windows": {}, "slow_drift": []},
        "information": {"verdict": "静穏週" if quiet else "要対応週",
                        "quiet": quiet, "actionable": [],
                        "actionable_count": 0 if quiet else 3,
                        "checked_count": 9, "folded_count": 0},
        "narrative": {"crowding": {}},
        "indices": [], "moomoo": {}, "holding_news": {},
        "forward_schedule": [], "prior_context": "",
    }


# ---------------------------------------------------------------------------
# 骨格の順序（受け入れ基準1）
# ---------------------------------------------------------------------------


def test_section_order_is_decision_theoretic():
    """照合 → 信念 → 前方 → 制約 → 機会 → 事前決定 → 監査。

    機会を先に出すのは、自宅が燃えているかを確認する前に買い物に行くのと同じ。
    """
    secs = sorted(drv.build_sections(_pack()), key=lambda s: s["order"])
    ids = [s["id"] for s in secs]

    assert ids[0] == "verdict", "判定は最初に置く（最後に書く）"
    for earlier, later in (
        ("reconcile", "belief"),
        ("belief", "forward"),
        ("forward", "constraints"),
        ("constraints", "heat"),
        ("heat", "decide"),
        ("decide", "audit"),
        ("audit", "limits"),
    ):
        assert ids.index(earlier) < ids.index(later), \
            f"{earlier} は {later} より前でなければならない"


def test_opportunity_comes_after_constraints():
    """制約（行動可能な空間）を確定させてから機会を提示する。"""
    secs = sorted(drv.build_sections(_pack()), key=lambda s: s["order"])
    constraints_order = next(s["order"] for s in secs if s["id"] == "constraints")
    holdings = [s for s in secs if s["kind"] == "holding"]
    assert holdings, "要対応週なら銘柄別の節がある"
    assert all(h["order"] > constraints_order for h in holdings)


def test_verdict_is_written_last_but_placed_first():
    secs = drv.build_sections(_pack())
    verdict = next(s for s in secs if s["id"] == "verdict")
    assert verdict["order"] == 10, "先頭に置く"
    assert secs.index(verdict) == len(secs) - 1, "最後に書く"
    assert verdict.get("needs_body") is True, "全節を読んでから書く"


def test_decide_section_exists_and_needs_body():
    """事前決定は土曜の唯一の『行動』。本文を読んでから書く。"""
    secs = drv.build_sections(_pack())
    decide = next(s for s in secs if s["id"] == "decide")
    assert decide["needs_body"] is True
    assert "条件付き政策" in decide["heading"]


def test_every_section_has_heading_and_spec():
    for s in drv.build_sections(_pack()):
        assert s.get("heading"), s["id"]
        assert s.get("spec"), s["id"]
        assert s.get("kind"), s["id"]


def test_section_ids_are_unique():
    ids = [s["id"] for s in drv.build_sections(_pack())]
    assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# 静穏週で短くなる（受け入れ基準3）
# ---------------------------------------------------------------------------


def test_quiet_week_does_not_expand_per_holding_sections():
    """変化のない週にレポートが実際に短くなること。"""
    busy = drv.build_sections(_pack(quiet=False))
    quiet = drv.build_sections(_pack(quiet=True))
    assert len(quiet) < len(busy)
    assert not any(s["kind"] == "holding" for s in quiet)
    assert any(s["kind"] == "holding" for s in busy)


def test_quiet_week_still_keeps_all_seven_skeleton_sections():
    """短くしても骨格は落とさない（折り畳みは削除ではない）。"""
    quiet = drv.build_sections(_pack(quiet=True))
    ids = {s["id"] for s in quiet}
    for required in ("verdict", "reconcile", "belief", "forward",
                     "constraints", "decide", "audit", "limits"):
        assert required in ids, required


def test_quiet_week_opportunity_section_is_capped():
    quiet = drv.build_sections(_pack(quiet=True))
    opp = next(s for s in quiet if s["id"] == "opportunity")
    assert "10行以内" in opp["spec"]
    assert "折り畳み" in opp["spec"]


def test_is_quiet_week_reads_information():
    assert drv.is_quiet_week(_pack(quiet=True)) is True
    assert drv.is_quiet_week(_pack(quiet=False)) is False
    assert drv.is_quiet_week({}) is False


def test_more_holdings_produce_more_sections_when_busy():
    few = drv.build_sections(_pack(holdings=[
        {"symbol": "A.T", "name": "A", "weight_pct": 50.0}]))
    many = drv.build_sections(_pack(holdings=[
        {"symbol": f"{i}.T", "name": str(i), "weight_pct": 10.0}
        for i in range(1, 6)]))
    assert len(many) > len(few)


# ---------------------------------------------------------------------------
# 照合状態の伝播（受け入れ基準2）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["verdict", "reconcile", "belief", "forward",
                                  "constraints", "decide", "audit", "heat",
                                  "limits", "holding"])
def test_reconciliation_status_reaches_every_section(kind):
    """どの節も照合状態を持つ。未照合のまま確定値として語らせないため。"""
    pack = _pack(rec_status="unreconciled")
    section = {"kind": kind, "key": "sym:2802", "symbol": "2802.T"}
    material = drv.slice_pack(pack, section)
    assert "reconciliation_status" in material
    assert material["reconciliation_status"]["blocking"] is True
    assert material["reconciliation_status"]["status"] == "unreconciled"


@pytest.mark.parametrize("kind", ["verdict", "reconcile", "belief", "forward",
                                  "constraints", "decide", "audit", "heat"])
def test_information_verdict_reaches_every_section(kind):
    material = drv.slice_pack(_pack(quiet=True), {"kind": kind})
    assert material["information"]["quiet"] is True


def test_rec_status_extracts_independence_flag():
    st = drv._rec_status(_pack(rec_status="circular"))
    assert st["independently_verified"] is False


def test_rec_status_on_empty_pack():
    st = drv._rec_status({})
    assert st["status"] is None
    assert st["blocking"] is None


# ---------------------------------------------------------------------------
# スライス内容
# ---------------------------------------------------------------------------


def test_reconcile_slice_carries_full_reconciliation():
    m = drv.slice_pack(_pack(), {"kind": "reconcile"})
    assert m["reconciliation"]["counts"]["orphans"] == 1


def test_belief_slice_carries_falsification():
    m = drv.slice_pack(_pack(), {"kind": "belief"})
    assert m["falsification"]["intact"] == 2
    assert "holdings_overview" in m


def test_forward_slice_carries_calendar():
    m = drv.slice_pack(_pack(), {"kind": "forward"})
    assert "calendar" in m["forward"]


def test_constraints_slice_carries_tax_and_runway():
    m = drv.slice_pack(_pack(), {"kind": "constraints"})
    assert "tax_state" in m["constraints"]
    assert "runway_bundle" in m["constraints"]


def test_holding_slice_carries_next_week_events_and_crowding():
    pack = _pack()
    pack["forward"]["calendar"]["events"] = [
        {"symbol": "2802.T", "kind": "earnings", "date": "2026-08-06"},
        {"symbol": "SOXL", "kind": "earnings", "date": "2026-08-06"},
    ]
    pack["narrative"]["crowding"] = {"2802.T": {"available": True, "ratio": 3.0}}
    m = drv.slice_pack(pack, {"kind": "holding", "key": "sym:2802",
                              "symbol": "2802.T"})
    assert [e["symbol"] for e in m["next_week_events"]] == ["2802.T"]
    assert m["crowding"]["ratio"] == 3.0


def test_audit_slice_carries_cumulative_only():
    m = drv.slice_pack(_pack(), {"kind": "audit"})
    assert "cumulative_diff" in m
    assert m["week_diff"] == {"folded_count": 0}, "監査節に生の差分全量は渡さない"


# ---------------------------------------------------------------------------
# 組み立て
# ---------------------------------------------------------------------------


def _write_sections(tmp_path: Path, state: dict) -> None:
    for s in state["sections"]:
        if s.get("file"):
            p = tmp_path / s["file"]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"{s['heading']}\n\n本文\n", encoding="utf-8")


def _state_from(pack: dict, day: str = "20260801") -> dict:
    secs = drv.build_sections(pack)
    return {"date": day, "sections": [
        {**s, "status": "done", "file": f"{day}/{s['order']:02d}_{s['id']}.md"}
        for s in secs]}


def test_assembled_report_puts_verdict_first(tmp_path):
    pack = _pack()
    state = _state_from(pack)
    _write_sections(tmp_path, state)
    text = drv.assemble(state, tmp_path, pack)
    order = [text.index(s["heading"]) for s in
             sorted(state["sections"], key=lambda x: x["order"])]
    assert order == sorted(order), "見出しは order 通りに並ぶ"
    assert text.index("## 0. 今週の判定") < text.index("## 1. 照合")


def test_assembled_report_adds_parent_heading_for_holdings(tmp_path):
    pack = _pack()
    state = _state_from(pack)
    _write_sections(tmp_path, state)
    text = drv.assemble(state, tmp_path, pack)
    assert text.count("## 5. 機会 — 保有の立ち位置（保有比率順）") == 1


def test_quiet_report_has_no_duplicate_opportunity_heading(tmp_path):
    pack = _pack(quiet=True)
    state = _state_from(pack)
    _write_sections(tmp_path, state)
    text = drv.assemble(state, tmp_path, pack)
    assert "## 5. 機会 — 保有の立ち位置（保有比率順）" not in text
    assert text.count("## 5. 機会") == 1


def test_header_shows_reconciliation_and_verdict():
    header = drv.build_header(_pack(rec_status="circular"))
    assert "三点照合" in header
    assert "独立検証なし" in header
    assert "今週の判定" in header


def test_header_survives_missing_sections():
    header = drv.build_header({"meta": {}, "portfolio": {}})
    assert "週次ポートフォリオ" in header


# ---------------------------------------------------------------------------
# 執筆仕様
# ---------------------------------------------------------------------------


def test_spec_file_encodes_the_saturday_asymmetry():
    """第0原則（土曜は買う銘柄を答える日ではない）が仕様に書かれていること。"""
    spec = (REPO / ".claude" / "prompts" / "weekly_deep.md").read_text(
        encoding="utf-8")
    assert "いま買うべき銘柄」を出力してはならない" in spec
    assert "条件付き政策" in spec
    assert "信念の変化" in spec


def test_spec_file_lists_all_skeleton_sections():
    spec = (REPO / ".claude" / "prompts" / "weekly_deep.md").read_text(
        encoding="utf-8")
    for heading in ("### 1. 照合", "### 2. 信念の変化", "### 3. 前方イベント",
                    "### 4. 制約", "### 5. 機会", "### 6. 事前決定",
                    "### 7. 監査"):
        assert heading in spec, heading
