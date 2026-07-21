"""Fable5 の health / レポート配線のテスト."""

from datetime import datetime, timezone

import pytest

from src.output.fable5_formatter import (
    format_assumption_concentration,
    format_deviations,
    format_fable5_health_section,
    format_investor_diagnosis,
    format_policy_coverage,
    format_provenance_footer,
    format_provenance_health,
)


AT = datetime(2026, 7, 18, tzinfo=timezone.utc)
HOLDINGS = [
    {"symbol": "SOXL", "value": 6_800_000},
    {"symbol": "2802.T", "value": 2_100_000},
]


class TestPolicyCoverage:
    def test_uncovered_holdings_are_named(self, monkeypatch):
        monkeypatch.setattr(
            "src.core.policy.coverage_rate",
            lambda holdings, **kw: {
                "holdings_count": 2,
                "covered_count": 1,
                "count_rate": 0.5,
                "value_rate": 0.76,
                "uncovered_symbols": ["2802.T"],
            },
        )
        out = format_policy_coverage(HOLDINGS)
        assert "2802.T" in out
        assert "白紙" in out

    def test_full_coverage_is_reported(self, monkeypatch):
        monkeypatch.setattr(
            "src.core.policy.coverage_rate",
            lambda holdings, **kw: {
                "holdings_count": 2,
                "covered_count": 2,
                "count_rate": 1.0,
                "value_rate": 1.0,
                "uncovered_symbols": [],
            },
        )
        assert "✅" in format_policy_coverage(HOLDINGS)

    def test_empty_portfolio_yields_nothing(self, monkeypatch):
        monkeypatch.setattr(
            "src.core.policy.coverage_rate",
            lambda holdings, **kw: {"holdings_count": 0, "covered_count": 0,
                                    "count_rate": 0.0, "value_rate": 0.0,
                                    "uncovered_symbols": []},
        )
        assert format_policy_coverage([]) == ""

    def test_failure_degrades_to_empty(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("nope")

        monkeypatch.setattr("src.core.policy.coverage_rate", boom)
        assert format_policy_coverage(HOLDINGS) == ""


class TestDeviations:
    def test_no_trades_yields_nothing(self):
        assert format_deviations(None) == ""

    def test_deviation_is_rendered(self, monkeypatch):
        monkeypatch.setattr(
            "src.core.policy.detect_deviations",
            lambda trades, **kw: [
                {"symbol": "SOXL", "detail": "トリガー未成立だが売却を実行"}
            ],
        )
        out = format_deviations([{"symbol": "SOXL", "action": "sell"}])
        assert "SOXL" in out
        assert "origin=process" in out

    def test_no_deviations_yields_nothing(self, monkeypatch):
        monkeypatch.setattr("src.core.policy.detect_deviations", lambda trades, **kw: [])
        assert format_deviations([{"symbol": "SOXL", "action": "sell"}]) == ""


class TestInvestorDiagnosis:
    def test_renders_insight(self, monkeypatch):
        monkeypatch.setattr(
            "src.data.question_log.investor_diagnosis",
            lambda **kw: {"total": 9, "insight": "防御的な質問が6割を超えている。"},
        )
        out = format_investor_diagnosis()
        assert "防御的な質問" in out
        assert "観測者も記憶する" in out

    def test_no_questions_yields_nothing(self, monkeypatch):
        monkeypatch.setattr(
            "src.data.question_log.investor_diagnosis", lambda **kw: {"total": 0}
        )
        assert format_investor_diagnosis() == ""


class TestAssumptionConcentration:
    def test_renders_top_assumptions(self, monkeypatch):
        monkeypatch.setattr(
            "src.core.risk.assumptions.analyze_assumption_space",
            lambda **kw: {
                "assumption_map": {"AI設備投資拡大": ["SOXL"]},
                "concentration": {
                    "message": "前提HHI 0.60 — 危険水準。",
                    "exposure": {"AI設備投資拡大": 0.76},
                },
                "scenarios": [],
            },
        )
        out = format_assumption_concentration(HOLDINGS)
        assert "AI設備投資拡大" in out
        assert "76.0%" in out

    def test_failure_degrades_to_empty(self, monkeypatch):
        def boom(**k):
            raise RuntimeError("nope")

        monkeypatch.setattr("src.core.risk.assumptions.analyze_assumption_space", boom)
        assert format_assumption_concentration(HOLDINGS) == ""


class TestProvenanceHealth:
    def _claims(self, depth):
        from src.core.provenance import PRIMARY, SELF, build_claim

        root = build_claim("開示原文", PRIMARY, symbol="B", at=AT)
        chain = [root]
        current = root
        for i in range(depth):
            current = build_claim(f"推論{i}", SELF, symbol="B",
                                  derived_from=[current], at=AT)
            chain.append(current)
        return chain

    def test_contaminated_knowledge_is_flagged(self):
        out = format_provenance_health(self._claims(4))
        assert "独立根拠が薄い" in out
        assert "再接地が必要な主張" in out

    def test_clean_knowledge_has_no_warning(self):
        out = format_provenance_health(self._claims(1))
        assert "本解釈の根拠" in out
        assert "独立根拠が薄い" not in out

    def test_no_claims_yields_nothing(self):
        assert format_provenance_health([]) == ""

    def test_footer_is_report_ready(self):
        footer = format_provenance_footer(self._claims(1))
        assert "本解釈の根拠" in footer
        assert footer.startswith("\n---")

    def test_footer_without_claims_is_empty(self):
        assert format_provenance_footer([]) == ""
        assert format_provenance_footer(None) == ""


class TestCombinedSection:
    def test_section_is_empty_when_nothing_to_report(self, monkeypatch):
        """出せる節が無いなら見出しだけ浮かせない。"""
        monkeypatch.setattr(
            "src.core.policy.coverage_rate",
            lambda holdings, **kw: {"holdings_count": 0, "covered_count": 0,
                                    "count_rate": 0.0, "value_rate": 0.0,
                                    "uncovered_symbols": []},
        )
        monkeypatch.setattr(
            "src.data.question_log.investor_diagnosis", lambda **kw: {"total": 0}
        )
        monkeypatch.setattr(
            "src.core.risk.assumptions.analyze_assumption_space",
            lambda **kw: {"concentration": {}},
        )
        monkeypatch.setattr("src.core.provenance.load_claims", lambda **kw: [])
        assert format_fable5_health_section([]) == ""

    def test_section_has_heading_when_content_exists(self, monkeypatch):
        monkeypatch.setattr(
            "src.core.policy.coverage_rate",
            lambda holdings, **kw: {"holdings_count": 2, "covered_count": 0,
                                    "count_rate": 0.0, "value_rate": 0.0,
                                    "uncovered_symbols": ["SOXL", "2802.T"]},
        )
        monkeypatch.setattr(
            "src.data.question_log.investor_diagnosis", lambda **kw: {"total": 0}
        )
        monkeypatch.setattr(
            "src.core.risk.assumptions.analyze_assumption_space",
            lambda **kw: {"concentration": {}},
        )
        monkeypatch.setattr("src.core.provenance.load_claims", lambda **kw: [])
        out = format_fable5_health_section(HOLDINGS)
        assert "## 構造診断 (Fable5)" in out
        assert "SOXL" in out
