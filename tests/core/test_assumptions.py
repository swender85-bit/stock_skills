"""前提空間ダイバーシフィケーションのテスト -- Fable5 第1弾 案2."""

import pytest

from src.core.risk.assumptions import (
    HHI_DANGER,
    HHI_WARNING,
    analyze_assumption_space,
    assumption_exposure,
    assumption_hhi,
    build_assumption_map,
    build_personal_scenarios,
    extract_assumptions,
    generate_inversion_scenario,
)


class TestExtractAssumptions:
    def test_detects_fx_assumption(self):
        assert "円安継続" in extract_assumptions("円安が続く前提で米国株を厚めに持つ")

    def test_detects_ai_capex(self):
        assert "AI設備投資拡大" in extract_assumptions("データセンター投資が続く限り半導体は強い")

    def test_detects_multiple_assumptions(self):
        found = extract_assumptions("AI需要が続き、利下げも進むなら グロースに追い風")
        assert "AI設備投資拡大" in found
        assert "金利低下" in found

    def test_vague_words_do_not_trigger(self):
        """『成長』『好調』のような曖昧語で前提を捏造しない。"""
        assert extract_assumptions("業績が好調で成長している") == []

    def test_empty_text(self):
        assert extract_assumptions("") == []
        assert extract_assumptions(None) == []


class TestBuildAssumptionMap:
    def test_groups_symbols_by_shared_assumption(self):
        notes = [
            {"symbol": "SOXL", "content": "AI需要が続く"},
            {"symbol": "TECL", "content": "データセンター投資は継続する"},
            {"symbol": "2802.T", "content": "価格転嫁が進む"},
        ]
        result = build_assumption_map(notes)
        assert result["AI設備投資拡大"] == ["SOXL", "TECL"]
        assert result["コスト転嫁継続"] == ["2802.T"]

    def test_notes_without_symbol_are_skipped(self):
        assert build_assumption_map([{"content": "円安が続く"}]) == {}

    def test_duplicate_symbols_are_deduped(self):
        notes = [
            {"symbol": "SOXL", "content": "AI需要"},
            {"symbol": "SOXL", "content": "生成AIの拡大"},
        ]
        assert build_assumption_map(notes)["AI設備投資拡大"] == ["SOXL"]

    def test_trigger_field_is_also_scanned(self):
        notes = [{"symbol": "X", "content": "", "trigger": "円安が続く限り"}]
        assert "円安継続" in build_assumption_map(notes)

    def test_empty_input(self):
        assert build_assumption_map([]) == {}


class TestAssumptionExposure:
    def test_weights_by_portfolio_value(self):
        mapping = {"AI設備投資拡大": ["SOXL", "TECL"]}
        holdings = [
            {"symbol": "SOXL", "value": 600},
            {"symbol": "TECL", "value": 200},
            {"symbol": "2802.T", "value": 200},
        ]
        exposure = assumption_exposure(mapping, holdings)
        assert exposure["AI設備投資拡大"] == pytest.approx(0.8)

    def test_overlapping_assumptions_double_count_on_purpose(self):
        """1銘柄が2前提に依存するなら、両方が『壊れたら効く』。合計は100%を超えてよい。"""
        mapping = {"AI設備投資拡大": ["SOXL"], "金利低下": ["SOXL"]}
        holdings = [{"symbol": "SOXL", "value": 1000}]
        exposure = assumption_exposure(mapping, holdings)
        assert exposure["AI設備投資拡大"] == pytest.approx(1.0)
        assert exposure["金利低下"] == pytest.approx(1.0)

    def test_falls_back_to_counts_without_holdings(self):
        mapping = {"円安継続": ["A", "B"]}
        assert assumption_exposure(mapping)["円安継続"] == 2.0


class TestAssumptionHHI:
    def test_single_shared_assumption_is_dangerous(self):
        """セクターが分散していても、全銘柄が同じ物語なら分散していない。"""
        mapping = {"AI設備投資拡大": ["SOXL", "TECL", "TQQQ"]}
        holdings = [
            {"symbol": s, "value": 1000} for s in ("SOXL", "TECL", "TQQQ")
        ]
        result = assumption_hhi(mapping, holdings)
        assert result["hhi"] >= HHI_DANGER
        assert result["level"] == "danger"
        assert result["top_assumption"] == "AI設備投資拡大"

    def test_spread_assumptions_are_ok(self):
        mapping = {
            "AI設備投資拡大": ["SOXL"],
            "コスト転嫁継続": ["2802.T"],
            "個人消費堅調": ["9843.T"],
            "円安継続": ["TECL"],
        }
        holdings = [
            {"symbol": s, "value": 1000}
            for s in ("SOXL", "2802.T", "9843.T", "TECL")
        ]
        result = assumption_hhi(mapping, holdings)
        assert result["level"] == "ok"

    def test_empty_map_is_unknown_not_safe(self):
        """前提が抽出できないことを『分散している』と誤読させない。"""
        result = assumption_hhi({})
        assert result["level"] == "unknown"
        assert "抽出できませんでした" in result["message"]

    def test_message_names_the_dominant_assumption(self):
        mapping = {"円安継続": ["A", "B", "C"]}
        holdings = [{"symbol": s, "value": 100} for s in ("A", "B", "C")]
        assert "円安継続" in assumption_hhi(mapping, holdings)["message"]

    def test_hhi_is_bounded(self):
        mapping = {"円安継続": ["A"], "金利低下": ["B"]}
        holdings = [{"symbol": "A", "value": 50}, {"symbol": "B", "value": 50}]
        assert 0.0 <= assumption_hhi(mapping, holdings)["hhi"] <= 1.0


class TestInversionScenario:
    def test_generates_scenario_in_existing_shape(self):
        """既存の SCENARIOS と同じ形なので、既存分析にそのまま流せる。"""
        scenario = generate_inversion_scenario("AI設備投資拡大", ["SOXL"])
        assert scenario["base_shock"] < 0
        assert "primary" in scenario["effects"]
        assert scenario["effects"]["primary"][0]["target"]
        assert scenario["source"] == "assumption_inversion"

    def test_unknown_assumption_returns_none(self):
        """影響度を推測で捏造しない。"""
        assert generate_inversion_scenario("よく分からない前提") is None

    def test_scenario_names_the_assumption(self):
        scenario = generate_inversion_scenario("円安継続")
        assert "円安継続" in scenario["name"]
        assert scenario["assumption"] == "円安継続"

    def test_exposed_symbols_are_carried(self):
        scenario = generate_inversion_scenario("金利低下", ["SOXL", "TECL"])
        assert scenario["exposed_symbols"] == ["SOXL", "TECL"]


class TestBuildPersonalScenarios:
    def test_orders_by_exposure(self):
        mapping = {
            "AI設備投資拡大": ["SOXL", "TECL", "TQQQ"],
            "コスト転嫁継続": ["2802.T"],
        }
        holdings = [
            {"symbol": s, "value": 1000}
            for s in ("SOXL", "TECL", "TQQQ", "2802.T")
        ]
        scenarios = build_personal_scenarios(mapping, holdings)
        assert scenarios[0]["assumption"] == "AI設備投資拡大"

    def test_respects_limit(self):
        mapping = {
            "AI設備投資拡大": ["A"],
            "円安継続": ["B"],
            "金利低下": ["C"],
            "個人消費堅調": ["D"],
        }
        assert len(build_personal_scenarios(mapping, limit=2)) == 2

    def test_undefined_assumptions_are_skipped_not_faked(self):
        mapping = {"地政学安定": ["A"]}  # INVERSION_EFFECTS に定義なし
        assert build_personal_scenarios(mapping) == []

    def test_empty_map_yields_no_scenarios(self):
        assert build_personal_scenarios({}) == []


class TestAnalyzeAssumptionSpace:
    def test_full_analysis(self):
        notes = [
            {"symbol": "SOXL", "content": "AI需要が続く"},
            {"symbol": "TECL", "content": "データセンター投資は継続"},
        ]
        holdings = [{"symbol": "SOXL", "value": 1000}, {"symbol": "TECL", "value": 1000}]
        result = analyze_assumption_space(notes=notes, holdings=holdings)
        assert result["concentration"]["top_assumption"] == "AI設備投資拡大"
        assert len(result["scenarios"]) >= 1

    def test_no_notes_degrades_gracefully(self):
        result = analyze_assumption_space(notes=[])
        assert result["assumption_map"] == {}
        assert result["scenarios"] == []
        assert result["concentration"]["level"] == "unknown"
