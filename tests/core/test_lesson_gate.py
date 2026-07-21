"""lesson ゲートのテスト -- Fable5 第1弾 案3 + 第2弾 案B P4."""

from datetime import date

import pytest

from src.core.lesson_gate import (
    VERDICT_APPLY,
    VERDICT_SHELVE,
    VERDICT_WARN,
    build_envelope,
    evaluate_lesson,
    gate_lessons,
    is_constraint_eligible,
    lesson_origin,
)


TODAY = date(2026, 7, 18)


class TestOrigin:
    def test_missing_origin_is_legacy(self):
        assert lesson_origin({"content": "x"}) == "legacy"

    def test_explicit_origins_are_preserved(self):
        assert lesson_origin({"origin": "process"}) == "process"
        assert lesson_origin({"origin": "outcome"}) == "outcome"

    def test_unknown_origin_falls_back_to_legacy(self):
        assert lesson_origin({"origin": "vibes"}) == "legacy"

    def test_outcome_lessons_are_not_constraint_eligible(self):
        assert is_constraint_eligible({"origin": "outcome"}) is False

    def test_process_and_legacy_are_eligible(self):
        assert is_constraint_eligible({"origin": "process"}) is True
        assert is_constraint_eligible({"content": "old lesson"}) is True


class TestGateVerdicts:
    def test_outcome_lesson_is_shelved(self):
        """案B P4: 運を教師にしたlessonは制約にならない。"""
        result = evaluate_lesson({"origin": "outcome"}, today=TODAY)
        assert result["verdict"] == VERDICT_SHELVE
        assert "結果由来" in result["reasons"][0]

    def test_expired_envelope_is_shelved(self):
        lesson = {"origin": "process", "validity": build_envelope(valid_until="2026-01-01")}
        result = evaluate_lesson(lesson, today=TODAY)
        assert result["verdict"] == VERDICT_SHELVE
        assert "有効期限" in result["reasons"][0]

    def test_unexpired_envelope_applies(self):
        lesson = {"origin": "process", "validity": build_envelope(valid_until="2027-01-01")}
        assert evaluate_lesson(lesson, today=TODAY)["verdict"] == VERDICT_APPLY

    def test_regime_mismatch_downgrades_to_warning(self):
        """低金利期に学んだ教訓を高金利期に無条件適用させない。"""
        lesson = {"origin": "process", "validity": build_envelope(regime=["low-vol"])}
        result = evaluate_lesson(lesson, current_regime=["high-vol"], today=TODAY)
        assert result["verdict"] == VERDICT_WARN
        assert "一致しない" in result["reasons"][0]

    def test_regime_match_applies(self):
        lesson = {"origin": "process", "validity": build_envelope(regime=["high-vol"])}
        result = evaluate_lesson(lesson, current_regime=["high-vol", "fear"], today=TODAY)
        assert result["verdict"] == VERDICT_APPLY

    def test_unknown_current_regime_does_not_block(self):
        lesson = {"origin": "process", "validity": build_envelope(regime=["low-vol"])}
        result = evaluate_lesson(lesson, current_regime=[], today=TODAY)
        assert result["verdict"] == VERDICT_APPLY
        assert any("照合できず" in r for r in result["reasons"])

    def test_legacy_lesson_without_envelope_still_applies(self):
        """非破壊: 既存lessonは従来どおり効く。"""
        assert evaluate_lesson({"content": "古い教訓"}, today=TODAY)["verdict"] == VERDICT_APPLY

    def test_expiry_beats_regime_match(self):
        lesson = {
            "origin": "process",
            "validity": build_envelope(regime=["high-vol"], valid_until="2026-01-01"),
        }
        result = evaluate_lesson(lesson, current_regime=["high-vol"], today=TODAY)
        assert result["verdict"] == VERDICT_SHELVE


class TestGateLessons:
    def test_shelved_lessons_are_separated(self):
        lessons = [
            {"id": "a", "origin": "process"},
            {"id": "b", "origin": "outcome"},
        ]
        passed, shelved = gate_lessons(lessons, today=TODAY)
        assert [x["id"] for x in passed] == ["a"]
        assert [x["id"] for x in shelved] == ["b"]

    def test_gate_annotation_is_attached(self):
        passed, _ = gate_lessons([{"id": "a", "origin": "process"}], today=TODAY)
        assert passed[0]["_gate"]["verdict"] == VERDICT_APPLY
        assert passed[0]["_gate"]["label"] == "適用"

    def test_drop_shelved_false_keeps_everything(self):
        lessons = [{"id": "b", "origin": "outcome"}]
        passed, shelved = gate_lessons(lessons, today=TODAY, drop_shelved=False)
        assert len(passed) == 1
        assert shelved == []

    def test_original_lessons_are_not_mutated(self):
        original = {"id": "a", "origin": "process"}
        gate_lessons([original], today=TODAY)
        assert "_gate" not in original


class TestConstraintExtractorIntegration:
    """案B 受け入れ基準P4: outcome由来lessonがplan-check制約に読まれない。"""

    def test_outcome_lessons_are_excluded_from_constraints(self, monkeypatch):
        from src.data.context import constraint_extractor as ce

        lessons = [
            {
                "id": "process_one",
                "origin": "process",
                "trigger": "損切りが遅れた",
                "expected_action": "閾値で機械的に損切りする",
                "content": "損切りが遅れた",
                "date": "2026-07-01",
            },
            {
                "id": "outcome_one",
                "origin": "outcome",
                "trigger": "損切りしたら反発した",
                "expected_action": "損切りしない",
                "content": "損切りしたら反発した",
                "date": "2026-07-02",
            },
        ]
        monkeypatch.setattr(
            "src.data.note_manager.load_notes", lambda note_type=None, **kw: lessons
        )
        monkeypatch.setattr(ce, "_build_lot_size_constraints", lambda symbols: [])

        result = ce.extract_constraints("損切りすべき？")
        ids = [c["id"] for c in result["constraints"]]
        assert "outcome_one" not in ids

    def test_constraints_carry_their_gate_verdict(self, monkeypatch):
        from src.data.context import constraint_extractor as ce

        lessons = [
            {
                "id": "process_one",
                "origin": "process",
                "trigger": "損切りが遅れた",
                "expected_action": "閾値で機械的に損切りする",
                "content": "損切りが遅れた",
                "date": "2026-07-01",
            }
        ]
        monkeypatch.setattr(
            "src.data.note_manager.load_notes", lambda note_type=None, **kw: lessons
        )
        monkeypatch.setattr(ce, "_build_lot_size_constraints", lambda symbols: [])

        result = ce.extract_constraints("損切りすべき？")
        matched = [c for c in result["constraints"] if c["id"] == "process_one"]
        assert matched, "process lesson should survive the gate"
        assert matched[0]["origin"] == "process"
        assert matched[0]["gate_verdict"] == VERDICT_APPLY
