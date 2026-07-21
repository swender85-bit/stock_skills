"""質問ストリーム記録と投資家診断のテスト -- Fable5 第1弾 案1."""

from datetime import date, datetime, timezone

import pytest

from src.data.question_log import (
    ACQUISITIVE,
    DEFENSIVE,
    NEUTRAL,
    classify_sentiment,
    investor_diagnosis,
    load_questions,
    record_question,
)


AT = datetime(2026, 7, 18, tzinfo=timezone.utc)


class TestClassifySentiment:
    def test_worry_is_defensive(self):
        assert classify_sentiment("PF大丈夫かな") == DEFENSIVE
        assert classify_sentiment("そろそろ売るべき？") == DEFENSIVE
        assert classify_sentiment("暴落したけどどうしよう") == DEFENSIVE

    def test_seeking_is_acquisitive(self):
        assert classify_sentiment("いい日本株ある？") == ACQUISITIVE
        assert classify_sentiment("買い増しすべき？") == ACQUISITIVE

    def test_lookup_is_neutral(self):
        assert classify_sentiment("PF見せて") == NEUTRAL
        assert classify_sentiment("トヨタのPERは？") == NEUTRAL

    def test_empty_is_neutral(self):
        assert classify_sentiment("") == NEUTRAL

    def test_mixed_markers_resolve_by_majority(self):
        assert classify_sentiment("暴落してるけど買い増しのチャンス？狙い目？") == ACQUISITIVE


class TestRecordQuestion:
    def test_records_with_market_state(self, tmp_path):
        record = record_question(
            "PF大丈夫かな",
            intent="stock-portfolio health",
            symbols=["2802.T"],
            market_state={"index_change_pct": -4.2},
            at=AT,
            base_dir=str(tmp_path),
        )
        assert record["sentiment"] == DEFENSIVE
        assert record["market_state"]["index_change_pct"] == -4.2
        assert record["asked_at"]["market_tz"] == "Asia/Tokyo"

    def test_appends_to_daily_file(self, tmp_path):
        for _ in range(3):
            record_question("PF大丈夫かな", at=AT, base_dir=str(tmp_path))
        assert len(load_questions(base_dir=str(tmp_path))) == 3

    def test_missing_market_state_still_records(self, tmp_path):
        """市況が取れなくても質問の時刻と種類は残す（後から遡れないため）。"""
        record = record_question("売るべき？", at=AT, base_dir=str(tmp_path))
        assert record is not None
        assert record["market_state"] == {}

    def test_failure_returns_none_without_raising(self, monkeypatch, tmp_path):
        """記録が失敗してもユーザーの質問処理は止めない。"""
        monkeypatch.setattr(
            "src.data.question_log._questions_dir",
            lambda base_dir=None: (_ for _ in ()).throw(OSError("disk full")),
        )
        assert record_question("PF大丈夫かな", base_dir=str(tmp_path)) is None


class TestLoadQuestions:
    def test_missing_dir_returns_empty(self, tmp_path):
        assert load_questions(base_dir=str(tmp_path / "nope")) == []

    def test_filters_by_sentiment(self, tmp_path):
        record_question("PF大丈夫かな", at=AT, base_dir=str(tmp_path))
        record_question("いい株ある？", at=AT, base_dir=str(tmp_path))
        defensive = load_questions(sentiment=DEFENSIVE, base_dir=str(tmp_path))
        assert len(defensive) == 1

    def test_corrupt_lines_are_skipped(self, tmp_path):
        record_question("PF大丈夫かな", at=AT, base_dir=str(tmp_path))
        (tmp_path / "2026-07-18.jsonl").open("a", encoding="utf-8").write("{broken\n")
        assert len(load_questions(base_dir=str(tmp_path))) == 1


class TestInvestorDiagnosis:
    def _ask(self, tmp_path, text, index_change=None, n=1):
        for _ in range(n):
            record_question(
                text,
                market_state={"index_change_pct": index_change} if index_change is not None else None,
                at=AT,
                base_dir=str(tmp_path),
            )

    def test_small_sample_does_not_claim_a_pattern(self, tmp_path):
        self._ask(tmp_path, "PF大丈夫かな", n=2)
        result = investor_diagnosis(base_dir=str(tmp_path))
        assert "まだ足りない" in result["insight"]

    def test_counts_by_sentiment(self, tmp_path):
        self._ask(tmp_path, "PF大丈夫かな", n=4)
        self._ask(tmp_path, "いい株ある？", n=2)
        result = investor_diagnosis(base_dir=str(tmp_path))
        assert result["total"] == 6
        assert result["defensive"] == 4
        assert result["acquisitive"] == 2

    def test_detects_fear_as_a_lagging_indicator(self, tmp_path):
        """防御的質問が下落後に集中しているなら、不安は遅行指標。"""
        self._ask(tmp_path, "PF大丈夫かな", index_change=-4.5, n=5)
        result = investor_diagnosis(base_dir=str(tmp_path))
        assert "遅行指標" in result["insight"]

    def test_warns_on_defensive_dominance(self, tmp_path):
        self._ask(tmp_path, "PF大丈夫かな", n=8)
        result = investor_diagnosis(base_dir=str(tmp_path))
        assert result["defensive_ratio"] > 0.6
        assert "狼狽売り" in result["insight"]

    def test_flags_acquisitive_dominance(self, tmp_path):
        self._ask(tmp_path, "いい株ある？おすすめは？", n=8)
        result = investor_diagnosis(base_dir=str(tmp_path))
        assert "高値掴み" in result["insight"]

    def test_empty_history_is_safe(self, tmp_path):
        result = investor_diagnosis(base_dir=str(tmp_path))
        assert result["total"] == 0
        assert result["defensive_ratio"] == 0.0
