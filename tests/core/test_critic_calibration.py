"""外部言説の較正重みのテスト (改善5).

## 何を縛っているか

1. **未検証を「外れ」と数えない。** pending を分母に入れると、
   まだ結果が出ていない主張が的中率を押し下げる。
2. **蓄積が足りないとき `available=False` を返す。**
   0.0 を返すと「まだ測れていない」が「当たらない情報源」に化ける。
3. **少数の的中で実力を判定しない。** 2勝0敗を 1.00 と出すと、
   偶然を実力と誤認する。
4. **判定には使わない。** 現時点では表示と引用形式の決定のみ。
"""

from __future__ import annotations

import pytest

from src.core.critic_calibration import (
    DOMAINS,
    MIN_SAMPLES,
    PENDING,
    InvalidThesis,
    add_thesis,
    annotate_claim,
    build_thesis,
    citation_style,
    domain_weight,
    list_critics,
    load_critic,
    profile,
    save_critic,
)


@pytest.fixture
def critics_dir(tmp_path):
    return str(tmp_path / "critics")


def _seed(base_dir: str, source_id: str, domain: str, scores: list[str],
          name: str = "テスト情報源") -> None:
    critic = {"source_id": source_id, "name": name, "theses": []}
    for i, score in enumerate(scores):
        critic["theses"].append(build_thesis(
            claim=f"主張{i}", domain=domain, at=f"2026-0{(i % 9) + 1}-01",
            score=score,
            verified_on=None if score == PENDING else f"2026-0{(i % 9) + 1}-15",
        ))
    save_critic(critic, base_dir)


# ---------------------------------------------------------------------------
# 台帳
# ---------------------------------------------------------------------------


class TestLedger:
    def test_unknown_domain_is_rejected(self):
        with pytest.raises(InvalidThesis, match="ドメイン"):
            build_thesis("需給が崩れる", domain="vibes")

    def test_unknown_score_is_rejected(self):
        with pytest.raises(InvalidThesis, match="採点"):
            build_thesis("需給が崩れる", domain="supply_demand", score="good")

    def test_scoring_requires_a_verification_date(self):
        # 検証日の無い採点は、結果を見てから付けた後知恵と区別できない
        with pytest.raises(InvalidThesis, match="検証日"):
            build_thesis("需給が崩れる", domain="supply_demand", score="hit_exact")

    def test_empty_claim_is_rejected(self):
        with pytest.raises(InvalidThesis, match="本文"):
            build_thesis("   ", domain="supply_demand")

    def test_pending_thesis_needs_no_verification_date(self):
        t = build_thesis("需給が崩れる", domain="supply_demand")
        assert t["score"] == PENDING
        assert t["verified_on"] is None

    def test_missing_critic_returns_empty_ledger_not_error(self):
        critic = load_critic("nobody", base_dir="does/not/exist")
        assert critic["theses"] == []
        assert critic["exists"] is False

    def test_roundtrip(self, critics_dir):
        add_thesis("critic_a", build_thesis("規制の事前織り込みで急落", "regulation"),
                   base_dir=critics_dir)
        assert list_critics(critics_dir) == ["critic_a"]
        assert load_critic("critic_a", critics_dir)["theses"][0]["domain"] == "regulation"


# ---------------------------------------------------------------------------
# 重み
# ---------------------------------------------------------------------------


class TestDomainWeight:
    def test_insufficient_samples_is_unavailable_not_zero(self, critics_dir):
        # 0.0 を返すと「まだ測れていない」が「当たらない情報源」に化ける
        _seed(critics_dir, "critic_a", "supply_demand", ["hit_exact", "hit_exact"])
        w = domain_weight("critic_a", "supply_demand", critics_dir)
        assert w["available"] is False
        assert w["weight"] is None
        assert "測れていない" in w["reason"]

    def test_high_hit_rate_domain(self, critics_dir):
        # 需給9勝1敗
        _seed(critics_dir, "critic_a", "supply_demand",
              ["hit_exact"] * 9 + ["refuted"])
        w = domain_weight("critic_a", "supply_demand", critics_dir)
        assert w["available"] is True
        assert w["weight"] == pytest.approx(0.9)
        assert w["usable"] is True

    def test_low_hit_rate_domain(self, critics_dir):
        # 価格水準の断言 0勝5敗
        _seed(critics_dir, "critic_a", "price_level", ["refuted"] * 5)
        w = domain_weight("critic_a", "price_level", critics_dir)
        assert w["available"] is True
        assert w["weight"] == 0.0
        assert w["usable"] is False

    def test_pending_theses_are_not_counted_as_misses(self, critics_dir):
        _seed(critics_dir, "critic_a", "supply_demand",
              ["hit_exact"] * 5 + [PENDING] * 20)
        w = domain_weight("critic_a", "supply_demand", critics_dir)
        assert w["weight"] == 1.0, "未検証を外れとして数えている"
        assert w["samples"] == 5
        assert w["pending"] == 20

    def test_partial_scores_are_weighted(self, critics_dir):
        _seed(critics_dir, "critic_a", "macro",
              ["hit_direction"] * 5)          # 0.7 × 5
        assert domain_weight("critic_a", "macro", critics_dir)["weight"] == pytest.approx(0.7)

    def test_unknown_critic_is_unavailable(self, critics_dir):
        w = domain_weight("nobody", "macro", critics_dir)
        assert w["available"] is False
        assert w["samples"] == 0

    def test_min_samples_is_configurable(self, critics_dir):
        _seed(critics_dir, "critic_a", "timing", ["hit_exact", "refuted"])
        assert domain_weight("critic_a", "timing", critics_dir,
                             min_samples=2)["available"] is True
        assert MIN_SAMPLES > 2  # 既定はもっと厳しい


# ---------------------------------------------------------------------------
# 得手不得手の構造
# ---------------------------------------------------------------------------


class TestProfile:
    def test_same_critic_can_be_strong_and_weak(self, critics_dir):
        """一人の批評家が需給に強く価格水準に弱い、という実態を潰さない。

        情報源を「信頼できる／できない」で二値化すると、この構造が消える。
        """
        critic = {"source_id": "critic_a", "name": "批評家A", "theses": []}
        for i in range(9):
            critic["theses"].append(build_thesis(
                f"需給{i}", "supply_demand", at="2026-07-16",
                score="hit_exact", verified_on="2026-07-16"))
        critic["theses"].append(build_thesis(
            "需給10", "supply_demand", at="2026-07-16",
            score="refuted", verified_on="2026-07-16"))
        for i in range(5):
            critic["theses"].append(build_thesis(
                f"価格{i}", "price_level", at="2026-07-16",
                score="refuted", verified_on="2026-07-16"))
        save_critic(critic, critics_dir)

        p = profile("critic_a", critics_dir)
        assert p["domains"]["supply_demand"]["usable"] is True
        assert p["domains"]["price_level"]["usable"] is False
        assert p["any_usable"] is True

    def test_profile_of_unknown_critic(self, critics_dir):
        p = profile("nobody", critics_dir)
        assert p["exists"] is False
        assert p["total"] == 0
        assert p["any_usable"] is False


# ---------------------------------------------------------------------------
# 引用形式（provenance.md の規約）
# ---------------------------------------------------------------------------


class TestCitationStyle:
    def test_high_weight_can_be_used_as_evidence(self, critics_dir):
        _seed(critics_dir, "critic_a", "supply_demand", ["hit_exact"] * 9 + ["refuted"])
        style = citation_style("critic_a", "supply_demand", critics_dir)
        assert style["style"] == "evidence"
        assert style["usable_as_evidence"] is True

    def test_low_weight_becomes_a_quotation(self, critics_dir):
        _seed(critics_dir, "critic_a", "price_level", ["refuted"] * 5)
        style = citation_style("critic_a", "price_level", critics_dir)
        assert style["style"] == "quotation"
        assert style["usable_as_evidence"] is False
        assert "過去的中率" in style["prefix"]

    def test_unmeasured_is_labelled_unverified_not_inaccurate(self, critics_dir):
        style = citation_style("critic_a", "technology", critics_dir)
        assert style["style"] == "unverified"
        assert "未測定" in style["prefix"]
        assert "低いという意味ではない" in style["note"]


# ---------------------------------------------------------------------------
# provenance との接続
# ---------------------------------------------------------------------------


class TestProvenanceWiring:
    def test_external_claim_is_annotated(self, critics_dir):
        from src.core.provenance import EXTERNAL, build_claim

        _seed(critics_dir, "critic_a", "supply_demand", ["hit_exact"] * 9 + ["refuted"])
        claim = build_claim("需給で急落する", EXTERNAL, symbol="QCOM",
                            source="https://example.com/post")
        annotated = annotate_claim(claim, "critic_a", "supply_demand", critics_dir)

        assert annotated["source_id"] == "critic_a"
        assert annotated["domain_weight"] == pytest.approx(0.9)
        assert annotated["citation"]["usable_as_evidence"] is True

    def test_non_external_claims_are_untouched(self, critics_dir):
        from src.core.provenance import PRIMARY, build_claim

        claim = build_claim("終値 175.63", PRIMARY, symbol="QCOM")
        annotated = annotate_claim(claim, "critic_a", "supply_demand", critics_dir)
        assert "source_id" not in annotated
        assert annotated["depth"] == 0

    def test_annotation_does_not_change_provenance_or_depth(self, critics_dir):
        from src.core.provenance import EXTERNAL, build_claim

        claim = build_claim("需給で急落する", EXTERNAL, symbol="QCOM")
        before = (claim["provenance"], claim["depth"])
        annotate_claim(claim, "critic_a", "supply_demand", critics_dir)
        # 蓄積が足りないうちに判定へ流さない
        assert (claim["provenance"], claim["depth"]) == before


def test_all_domains_have_labels():
    for domain in DOMAINS:
        assert DOMAINS[domain]
