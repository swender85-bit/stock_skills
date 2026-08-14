"""前提の衝突検出のテスト (改善4).

## 何を縛っているか

1. **円高が好機（両替計画）かつ危機（日本株保有）である状態を検出する。**
   同じ変数の同じ方向に opportunity と risk が同居し、opportunity 側が
   行動の前提になっている構造。円高が来た瞬間、両替の原資が目減りする。

2. **単なる二重ロング（同方向の集中）は検出しない。**
   誤検出はこの機能を無価値にする。米国株と日本の輸出企業を同時に持つのは
   「同方向の集中」であり、`exposure.describe_tilt()` が見る別種の問題。

3. **計画の前提が無いとき「衝突なし」と言わない。**
   片側しか台帳に無い状態は、測れていないのであって問題が無いのではない。
"""

from __future__ import annotations

from src.core.risk.assumptions import (
    analyze_assumption_space,
    build_assumption_records,
    detect_conflicting_assumptions,
)
from src.output.constraint_formatter import format_assumption_conflicts


HOLDINGS = [
    {"symbol": "9843.T", "value": 824550},
    {"symbol": "2802.T", "value": 2040800},
    {"symbol": "SOXL", "value": 4965655},
]


def _note(note_type: str, symbol: str, content: str, note_id: str = "n1") -> dict:
    return {"id": note_id, "type": note_type, "symbol": symbol, "content": content}


# ---------------------------------------------------------------------------
# 検出すべきもの
# ---------------------------------------------------------------------------


class TestConflictDetected:
    def test_yen_strength_is_both_opportunity_and_risk(self):
        notes = [
            # 計画: 円高を待って両替する（行動の前提）
            _note("target", "", "USD転換の計画。円高が¥155まで進んだら両替する。", "plan1"),
            # 保有: 円安継続が前提の日本株（保有の前提）
            _note("thesis", "9843.T", "円安メリットで採算が改善する前提。", "th1"),
        ]
        conflicts = detect_conflicting_assumptions(
            build_assumption_records(notes, HOLDINGS))

        assert len(conflicts) == 1
        c = conflicts[0]
        assert c["variable"] == "USDJPY"
        assert c["direction"] == "down"
        assert c["direction_label"] == "円高"
        assert "9843.T" in c["exposed_symbols"]
        assert "原資" in c["message"]

    def test_conflict_reports_what_to_check(self):
        notes = [
            _note("target", "", "円高を待つ。¥155で両替。", "plan1"),
            _note("thesis", "2802.T", "円安継続で輸出採算が支える。", "th1"),
        ]
        conflicts = detect_conflicting_assumptions(
            build_assumption_records(notes, HOLDINGS))
        assert conflicts
        assert "原資が" in conflicts[0]["what_to_check"]

    def test_rate_conflict_is_detected_too(self):
        # 通貨に限らない。金利低下を待つ計画 × 金利低下前提の保有。
        notes = [
            _note("target", "", "利下げが来たら長期債を買う。", "plan1"),
            _note("thesis", "SOXL", "金利上昇局面ではグロースが不利。引き締めに注意。", "th1"),
        ]
        conflicts = detect_conflicting_assumptions(
            build_assumption_records(notes, HOLDINGS))
        assert any(c["variable"] == "RATES" for c in conflicts)


# ---------------------------------------------------------------------------
# 検出してはいけないもの（誤検出防止）
# ---------------------------------------------------------------------------


class TestNoFalsePositives:
    def test_plain_double_long_is_not_a_conflict(self):
        # 米国株と日本の輸出企業を同時に持つ = 同方向の集中。
        # これは「通貨の二重ロング」であって、前提の衝突ではない。
        notes = [
            _note("thesis", "SOXL", "円安継続で円建て評価額が伸びる。", "th1"),
            _note("thesis", "2802.T", "円安メリットで輸出採算が改善。", "th2"),
        ]
        conflicts = detect_conflicting_assumptions(
            build_assumption_records(notes, HOLDINGS))
        assert conflicts == []

    def test_aligned_plan_and_holding_is_not_a_conflict(self):
        # 計画も保有も同じ方向を望んでいる場合は衝突しない
        notes = [
            _note("target", "", "円安が続くうちに米国株を買い増す。", "plan1"),
            _note("thesis", "SOXL", "円安継続が前提。", "th1"),
        ]
        conflicts = detect_conflicting_assumptions(
            build_assumption_records(notes, HOLDINGS))
        assert conflicts == []

    def test_opportunity_without_action_dependency_is_ignored(self):
        records = [
            {"variable": "USDJPY", "direction": "down", "role": "opportunity",
             "action_depends": False, "holdings": []},
            {"variable": "USDJPY", "direction": "down", "role": "risk",
             "action_depends": False, "holdings": ["9843.T"]},
        ]
        assert detect_conflicting_assumptions(records) == []

    def test_holdings_only_produces_no_conflict(self):
        notes = [_note("thesis", "9843.T", "円安継続が前提。", "th1")]
        assert detect_conflicting_assumptions(
            build_assumption_records(notes, HOLDINGS)) == []

    def test_malformed_records_are_skipped(self):
        records = [
            {"variable": None, "direction": "down", "role": "risk"},
            {"variable": "USDJPY", "direction": "sideways", "role": "risk"},
            {"variable": "USDJPY", "direction": "down", "role": "unknown"},
        ]
        assert detect_conflicting_assumptions(records) == []


# ---------------------------------------------------------------------------
# レコード生成
# ---------------------------------------------------------------------------


class TestRecordBuilding:
    def test_thesis_of_unheld_symbol_is_excluded(self):
        # 保有していない銘柄の前提を混ぜると HHI も衝突も歪む
        notes = [_note("thesis", "AAPL", "円安継続が前提。", "th1")]
        records = build_assumption_records(notes, HOLDINGS)
        assert records == []

    def test_plan_note_is_kept_even_without_holding(self):
        # 計画はまだ保有していないからこそ計画なので落とさない
        notes = [_note("target", "", "円高¥155で両替する。", "plan1")]
        records = build_assumption_records(notes, HOLDINGS)
        assert records and records[0]["role"] == "opportunity"
        assert records[0]["action_depends"] is True

    def test_other_note_types_are_ignored(self):
        notes = [_note("lesson", "9843.T", "円安継続が前提。", "l1"),
                 _note("concern", "9843.T", "円高が怖い。", "c1")]
        assert build_assumption_records(notes, HOLDINGS) == []


# ---------------------------------------------------------------------------
# 出力
# ---------------------------------------------------------------------------


class TestFormatting:
    def test_conflict_section_names_the_structure(self):
        notes = [
            _note("target", "", "円高¥155で両替する。", "plan1"),
            _note("thesis", "9843.T", "円安継続が前提。", "th1"),
        ]
        result = analyze_assumption_space(notes=notes, holdings=HOLDINGS)
        text = format_assumption_conflicts(result)
        assert "前提の衝突" in text
        assert "円高" in text
        assert "9843.T" in text

    def test_missing_plan_notes_is_reported_as_undetectable(self):
        notes = [_note("thesis", "9843.T", "円安継続が前提。", "th1")]
        result = analyze_assumption_space(notes=notes, holdings=HOLDINGS)
        assert result["conflict_detectable"] is False
        text = format_assumption_conflicts(result)
        # 「衝突なし」ではなく「判定できません」
        assert "判定できません" in text
        assert "『衝突なし』ではありません" in text

    def test_no_section_when_plans_exist_but_nothing_conflicts(self):
        notes = [
            _note("target", "", "円安が続くうちに米国株を買い増す。", "plan1"),
            _note("thesis", "SOXL", "円安継続が前提。", "th1"),
        ]
        result = analyze_assumption_space(notes=notes, holdings=HOLDINGS)
        assert result["conflict_detectable"] is True
        assert format_assumption_conflicts(result) == ""

    def test_handles_empty_input(self):
        assert format_assumption_conflicts(None) == ""
        assert format_assumption_conflicts({}) == ""


class TestAnalyzeIntegration:
    def test_hhi_ignores_plan_notes(self):
        # 計画の前提を HHI に混ぜると、保有していない銘柄の前提が exposure 0 で
        # 紛れ込み、集中度が不当に下がる
        holdings_only = [_note("thesis", "9843.T", "円安継続が前提。", "th1")]
        with_plan = holdings_only + [_note("target", "", "円高で両替。", "p1")]

        a = analyze_assumption_space(notes=holdings_only, holdings=HOLDINGS)
        b = analyze_assumption_space(notes=with_plan, holdings=HOLDINGS)
        assert a["concentration"]["hhi"] == b["concentration"]["hhi"]
        assert a["assumption_map"] == b["assumption_map"]

    def test_records_and_conflicts_are_exposed(self):
        notes = [
            _note("target", "", "円高¥155で両替する。", "plan1"),
            _note("thesis", "9843.T", "円安継続が前提。", "th1"),
        ]
        result = analyze_assumption_space(notes=notes, holdings=HOLDINGS)
        assert result["records"]
        assert result["conflicts"]
