"""判断パッケージと過程再審のテスト -- Fable5 第2弾 案B.

受け入れ基準:
  P1 封印後の改変がハッシュ検証で検出される
  P3 再審記録の時刻が結果参照より先行することを保証
  P5 開示時刻と判断時刻の前後判定が市場タイムゾーン込みで正しい
"""

from datetime import datetime, timezone

import pytest

from src.core.decision import (
    InformationBoundary,
    ReviewOrderViolation,
    attach_outcome,
    build_package,
    classify_by_disclosure_time,
    list_packages,
    load_package,
    process_review,
    save_package,
    verify_package,
)
from src.core.decision.review import luck_skill_stats, sealed_body_intact
from src.core.temporal import compare_instants


DECIDED_AT = datetime(2026, 7, 18, 0, 0, tzinfo=timezone.utc)


def _boundary():
    return InformationBoundary(
        used=[{"label": "決算短信"}],
        available_unused=[{"label": "在庫増加の開示"}],
        unknowable=[{"label": "為替急変"}],
    )


class TestBuildPackage:
    def test_requires_symbol_and_decision(self):
        with pytest.raises(ValueError):
            build_package("", "buy")
        with pytest.raises(ValueError):
            build_package("7203.T", "")

    def test_confidence_must_be_a_probability(self):
        with pytest.raises(ValueError):
            build_package("7203.T", "buy", confidence=1.5)

    def test_package_carries_market_and_timestamp(self):
        pkg = build_package("7203.T", "buy", decided_at=DECIDED_AT)
        assert pkg["market"] == "JP"
        assert pkg["decided_at"]["market_tz"] == "Asia/Tokyo"
        assert pkg["decided_at"]["utc"].startswith("2026-07-18")

    def test_starts_unsealed_and_without_outcome(self):
        pkg = build_package("7203.T", "buy", decided_at=DECIDED_AT)
        assert pkg["sealed"] is False
        assert pkg["outcome"] is None
        assert pkg["process_review"] is None
        assert verify_package(pkg) is False

    def test_boundary_kinds_must_be_exclusive(self):
        overlapping = InformationBoundary(
            used=[{"label": "決算短信"}],
            available_unused=[{"label": "決算短信"}],
        )
        with pytest.raises(ValueError, match="exclusive"):
            build_package("7203.T", "buy", boundary=overlapping)

    def test_accepts_boundary_as_plain_dict(self):
        pkg = build_package(
            "7203.T", "buy", boundary={"used": [{"label": "IR"}]}, decided_at=DECIDED_AT
        )
        assert pkg["information_boundary"]["used"] == [{"label": "IR"}]


class TestDisclosureClassification:
    """案B P5: 開示時刻による可知境界の機械判定。"""

    def test_disclosed_before_and_used_is_used(self):
        items = [{"label": "決算", "disclosed_at": "2026-07-15T15:00:00+09:00"}]
        b = classify_by_disclosure_time(items, DECIDED_AT, used_labels=["決算"])
        assert [i["label"] for i in b.used] == ["決算"]
        assert b.available_unused == []

    def test_disclosed_before_but_unused_is_a_process_miss(self):
        items = [{"label": "在庫増加", "disclosed_at": "2026-07-15T15:00:00+09:00"}]
        b = classify_by_disclosure_time(items, DECIDED_AT, used_labels=[])
        assert [i["label"] for i in b.available_unused] == ["在庫増加"]

    def test_disclosed_after_decision_is_luck(self):
        items = [{"label": "為替急変", "disclosed_at": "2026-07-20T15:00:00+09:00"}]
        b = classify_by_disclosure_time(items, DECIDED_AT, used_labels=[])
        assert [i["label"] for i in b.unknowable] == ["為替急変"]

    def test_timezone_is_respected_not_wall_clock(self):
        """判断は 2026-07-18 00:00 UTC (= 東京 09:00)。

        東京 08:00 の開示は判断より前、東京 10:00 の開示は判断より後。
        ローカル時刻の日付だけ見ると両方「同日」で区別がつかない。
        """
        items = [
            {"label": "朝の開示", "disclosed_at": "2026-07-18T08:00:00+09:00"},
            {"label": "昼の開示", "disclosed_at": "2026-07-18T10:00:00+09:00"},
        ]
        b = classify_by_disclosure_time(items, DECIDED_AT, used_labels=[])
        assert [i["label"] for i in b.available_unused] == ["朝の開示"]
        assert [i["label"] for i in b.unknowable] == ["昼の開示"]

    def test_unknown_disclosure_time_falls_back_to_unknowable(self):
        """開示時刻不明を『見落とし』にすると誤ったlessonを生む。運側に倒す。"""
        b = classify_by_disclosure_time([{"label": "噂"}], DECIDED_AT)
        assert [i["label"] for i in b.unknowable] == ["噂"]
        assert b.unknowable[0]["unclassified_reason"] == "disclosure time unknown"


class TestSealAndTamperDetection:
    """案B 受け入れ基準P1。"""

    def test_review_seals_the_package(self):
        pkg = build_package("7203.T", "buy", boundary=_boundary(), decided_at=DECIDED_AT)
        process_review(pkg)
        assert pkg["sealed"] is True
        assert verify_package(pkg) is True

    def test_post_seal_tampering_is_detected(self):
        pkg = build_package("7203.T", "buy", boundary=_boundary(), decided_at=DECIDED_AT)
        process_review(pkg)
        pkg["decision"] = "sell"
        assert verify_package(pkg) is False

    def test_tampering_with_the_boundary_is_detected(self):
        """後から『実は知り得なかった』と書き換える後知恵を封じる。"""
        pkg = build_package("7203.T", "buy", boundary=_boundary(), decided_at=DECIDED_AT)
        process_review(pkg)
        pkg["information_boundary"]["available_unused"] = []
        assert verify_package(pkg) is False

    def test_outcome_attachment_keeps_sealed_body_intact(self):
        pkg = build_package("7203.T", "buy", boundary=_boundary(), decided_at=DECIDED_AT)
        process_review(pkg)
        attach_outcome(pkg, return_pct=-12.0)
        assert sealed_body_intact(pkg) is True


class TestProcessReview:
    def test_unused_available_info_becomes_a_process_lesson(self):
        pkg = build_package("7203.T", "buy", boundary=_boundary(), decided_at=DECIDED_AT)
        process_review(pkg)
        lessons = pkg["process_review"]["lessons"]
        assert len(lessons) == 1
        assert lessons[0]["origin"] == "process"
        assert "在庫増加の開示" in lessons[0]["trigger"]

    def test_unknowable_info_generates_no_lesson(self):
        """運はlessonにしない -- これが案Bの核心。"""
        boundary = InformationBoundary(
            used=[{"label": "決算短信"}], unknowable=[{"label": "為替急変"}]
        )
        pkg = build_package("7203.T", "buy", boundary=boundary, decided_at=DECIDED_AT)
        process_review(pkg)
        assert pkg["process_review"]["lessons"] == []
        assert pkg["process_review"]["process_sound"] is True
        assert pkg["process_review"]["unknowable_count"] == 1

    def test_manual_findings_are_added_as_process_lessons(self):
        pkg = build_package("7203.T", "buy", decided_at=DECIDED_AT)
        process_review(pkg, findings=[{"label": "代替案を検討していない"}])
        assert len(pkg["process_review"]["lessons"]) == 1
        assert pkg["process_review"]["process_sound"] is False

    def test_double_review_is_rejected(self):
        pkg = build_package("7203.T", "buy", decided_at=DECIDED_AT)
        process_review(pkg)
        with pytest.raises(ReviewOrderViolation, match="封印済み"):
            process_review(pkg)


class TestOrderingEnforcement:
    """案B 受け入れ基準P3: 再審は結果参照より必ず先行する。"""

    def test_review_after_seeing_outcome_is_rejected(self):
        pkg = build_package("7203.T", "buy", boundary=_boundary(), decided_at=DECIDED_AT)
        pkg["outcome"] = {"return_pct": -12.0}  # 順序違反を模す
        with pytest.raises(ReviewOrderViolation, match="後知恵"):
            process_review(pkg)

    def test_outcome_before_review_is_rejected(self):
        pkg = build_package("7203.T", "buy", decided_at=DECIDED_AT)
        with pytest.raises(ReviewOrderViolation, match="過程再審の前"):
            attach_outcome(pkg, return_pct=-12.0)

    def test_review_timestamp_precedes_outcome_timestamp(self):
        pkg = build_package("7203.T", "buy", boundary=_boundary(), decided_at=DECIDED_AT)
        process_review(pkg, at=datetime(2026, 8, 1, tzinfo=timezone.utc))
        attach_outcome(pkg, return_pct=-12.0, at=datetime(2026, 9, 1, tzinfo=timezone.utc))
        order = compare_instants(
            pkg["process_review"]["reviewed_at"], pkg["outcome"]["recorded_at"]
        )
        assert order == -1

    def test_double_outcome_is_rejected(self):
        pkg = build_package("7203.T", "buy", decided_at=DECIDED_AT)
        process_review(pkg)
        attach_outcome(pkg, return_pct=1.0)
        with pytest.raises(ReviewOrderViolation, match="既に記録済み"):
            attach_outcome(pkg, return_pct=2.0)

    def test_outcome_on_tampered_package_is_rejected(self):
        pkg = build_package("7203.T", "buy", decided_at=DECIDED_AT)
        process_review(pkg)
        pkg["rationale"] = "後から書き換えた理由"
        with pytest.raises(ValueError, match="封印"):
            attach_outcome(pkg, return_pct=-5.0)

    def test_outcome_is_tagged_as_outcome_origin(self):
        pkg = build_package("7203.T", "buy", decided_at=DECIDED_AT)
        process_review(pkg)
        attach_outcome(pkg, return_pct=-8.0, note="決算後に下落")
        assert pkg["outcome"]["origin"] == "outcome"
        assert pkg["outcome"]["process_sound"] is True


class TestPersistence:
    def test_save_and_load_roundtrip(self, tmp_path):
        pkg = build_package("7203.T", "buy", boundary=_boundary(), decided_at=DECIDED_AT)
        process_review(pkg)
        save_package(pkg, base_dir=str(tmp_path))
        loaded = load_package(pkg["id"], base_dir=str(tmp_path))
        assert loaded is not None
        assert verify_package(loaded) is True

    def test_load_missing_returns_none(self, tmp_path):
        assert load_package("dp_nope", base_dir=str(tmp_path)) is None

    def test_list_filters_by_symbol_and_sorts_desc(self, tmp_path):
        old = build_package("7203.T", "buy", decided_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        new = build_package("7203.T", "sell", decided_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
        other = build_package("AAPL", "buy", decided_at=DECIDED_AT)
        for p in (old, new, other):
            save_package(p, base_dir=str(tmp_path))

        toyota = list_packages("7203.T", base_dir=str(tmp_path))
        assert [p["decision"] for p in toyota] == ["sell", "buy"]

    def test_list_on_missing_dir_returns_empty(self, tmp_path):
        assert list_packages(base_dir=str(tmp_path / "nope")) == []


class TestLuckSkillStats:
    def _pkg(self, sound, ret):
        pkg = build_package("7203.T", "buy", decided_at=DECIDED_AT)
        findings = [] if sound else [{"label": "見落とし"}]
        process_review(pkg, findings=findings)
        attach_outcome(pkg, return_pct=ret)
        return pkg

    def test_cross_tabulates_process_and_outcome(self):
        stats = luck_skill_stats([self._pkg(True, 5.0), self._pkg(False, -3.0)])
        assert stats["sound_good"] == 1
        assert stats["flawed_bad"] == 1
        assert stats["scored"] == 2

    def test_warns_when_sound_process_keeps_losing(self):
        """過程健全なのに結果不良が偏在 = 可知集合の外に構造要因がある疑い。"""
        pkgs = [self._pkg(True, -5.0) for _ in range(8)]
        assert luck_skill_stats(pkgs)["structural_factor_warning"] is True

    def test_no_warning_on_small_sample(self):
        assert luck_skill_stats([self._pkg(True, -5.0)])["structural_factor_warning"] is False

    def test_packages_without_outcome_are_skipped(self):
        pkg = build_package("7203.T", "buy", decided_at=DECIDED_AT)
        process_review(pkg)
        assert luck_skill_stats([pkg])["scored"] == 0
