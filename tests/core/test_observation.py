"""観測の第一級化（診断後修理 Phase C）の回帰テスト。

ここが緩むと 2026-08-08 の事故（部分値が全体値の顔で出る）が戻る。
"""
import pytest

from src.core import observation as obs


class TestCoverage:
    def test_complete_coverage_has_no_note(self):
        cov = obs.Coverage(total=3, observed=3)
        assert cov.complete is True
        assert cov.note() == ""  # 全数揃っていれば無害な追記もしない

    def test_partial_coverage_always_notes_denominator(self):
        cov = obs.Coverage(total=10, observed=1, missing=["A"] * 9)
        assert cov.complete is False
        note = cov.note()
        assert "10" in note and "1" in note
        assert "取得不可" in note

    def test_empty_set_is_not_complete(self):
        assert obs.Coverage(total=0, observed=0).complete is False


class TestPartialTotal:
    def test_returns_sum_and_coverage_together(self):
        rows = [{"symbol": "A", "value_jpy": 100.0},
                {"symbol": "B", "value_jpy": None},
                {"symbol": "C", "value_jpy": 50.0}]
        total, cov = obs.partial_total(rows, "value_jpy")
        assert total == 150.0
        assert cov.total == 3 and cov.observed == 2
        assert cov.missing == ["B"]
        assert cov.complete is False

    def test_the_2026_08_08_shape(self):
        """10件中1件しか取れないとき、合計は必ず部分値として返る。"""
        rows = [{"symbol": f"S{i}", "value_jpy": None} for i in range(9)]
        rows.append({"symbol": "SOXL", "value_jpy": 1_376_404.0})
        total, cov = obs.partial_total(rows, "value_jpy")
        assert total == 1_376_404.0
        assert cov.complete is False
        assert cov.note() != ""  # 注記なしで出せない


class TestSafeRatio:
    def test_suppresses_mixed_denominator(self):
        """分子は価格依存・分母は取得単価依存＝母集団が違う。値を返さない。"""
        rows = [{"symbol": "A", "pl_jpy": 1000.0, "cost_jpy": 10000.0}]
        rows += [{"symbol": f"S{i}", "pl_jpy": None, "cost_jpy": 5000.0}
                 for i in range(9)]
        r = obs.safe_ratio(rows, "pl_jpy", "cost_jpy")
        assert r["suppressed"] is True
        assert r["value"] is None
        assert "母集団" in r["reason"]
        # 部分値は捨てずに別キーで残す（計算はする、表示しない）
        assert r["partial_value"] == pytest.approx(10.0)

    def test_returns_value_when_sets_match(self):
        rows = [{"symbol": "A", "pl_jpy": 1000.0, "cost_jpy": 10000.0},
                {"symbol": "B", "pl_jpy": 500.0, "cost_jpy": 10000.0}]
        r = obs.safe_ratio(rows, "pl_jpy", "cost_jpy")
        assert r["suppressed"] is False
        assert r["value"] == pytest.approx(7.5)


class TestCoreFailures:
    def test_lists_only_repairable_failures(self):
        rows = [
            {"symbol": "A", "price_status": obs.UNAVAILABLE,
             "price_unavailable_reason": "possibly delisted"},
            {"symbol": "B", "price_status": obs.ABSENT},        # 取れたが値が無い
            {"symbol": "C", "price_status": obs.NOT_ATTEMPTED},  # 観測対象外
            {"symbol": "D", "price_status": obs.OBSERVED},
        ]
        out = obs.core_failures(rows)
        assert [f["id"] for f in out] == ["A"]
        assert "delisted" in out[0]["reason"]

    def test_missing_reason_is_never_silent(self):
        out = obs.core_failures([{"symbol": "A", "price_status": obs.UNAVAILABLE}])
        assert out[0]["reason"]  # 空文字を返さない


class TestClassify:
    def test_distinguishes_three_kinds_of_none(self):
        assert obs.classify(1.0) == obs.OBSERVED
        assert obs.classify(None, attempted=False) == obs.NOT_ATTEMPTED
        assert obs.classify(None, expected=False) == obs.ABSENT
        assert obs.classify(None, error="timeout") == obs.UNAVAILABLE
