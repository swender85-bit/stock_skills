"""V4（概念層）・V5（系譜の根）の回帰テスト。"""
import pytest

from src.core.reading import concepts as cpt
from src.core.reading import genealogy, schema, vault


@pytest.fixture()
def tmp_vault(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_SKILLS_VAULT", str(tmp_path))
    vault.ensure_structure()
    return tmp_path


def _concept(**kw):
    base = dict(name="レバレッジETFの逓減", category=cpt.INSTRUMENT,
                body="日次リバランスにより、往復すると原資産より戻らない。",
                counterexample="2024年の一方向上昇局面では逓減が観測されなかった。")
    base.update(kw)
    return cpt.build(**base)


class TestConceptCreation:
    def test_counterexample_is_mandatory(self):
        """反証事例を書けない概念は反証不能な信念であり、概念層に置かない。"""
        with pytest.raises(cpt.ConceptError) as e:
            _concept(counterexample="")
        assert "反証不能な信念" in str(e.value)

    def test_body_is_mandatory(self):
        with pytest.raises(cpt.ConceptError) as e:
            _concept(body="   ")
        assert "自分の言葉" in str(e.value)

    def test_bad_category_rejected(self):
        with pytest.raises(cpt.ConceptError):
            _concept(category="バリュー投資")

    def test_new_concept_starts_as_hypothesis(self):
        c = _concept()
        assert c["frontmatter"]["confidence"] == cpt.HYPOTHESIS
        assert c["frontmatter"]["application_count"] == 0

    def test_saved_file_is_parseable(self, tmp_vault):
        rel = cpt.save(_concept())
        fm, body = schema.parse_markdown((tmp_vault / rel).read_text(encoding="utf-8"))
        assert fm["type"] == "concept"
        assert "## 反証事例" in body
        assert "## 未解決の問い" in body


class TestConfidenceLifecycle:
    def test_promotes_on_evidence(self):
        t = cpt.next_confidence({"confidence": cpt.HYPOTHESIS,
                                 "application_count": 3, "success_count": 2})
        assert t["to"] == cpt.VERIFIED and t["changed"]

    def test_does_not_promote_without_enough_applications(self):
        t = cpt.next_confidence({"confidence": cpt.HYPOTHESIS,
                                 "application_count": 1, "success_count": 1})
        assert not t["changed"]

    def test_demotes_when_failing(self):
        t = cpt.next_confidence({"confidence": cpt.VERIFIED,
                                 "application_count": 5, "success_count": 1})
        assert t["to"] == cpt.DOUBTFUL

    def test_retirement_is_never_automatic(self):
        """概念の廃止は知識の放棄であり、統計が悪いだけで捨てるのは早計。"""
        t = cpt.next_confidence({"confidence": cpt.VERIFIED,
                                 "application_count": 99, "success_count": 0})
        assert t["to"] != cpt.RETIRED

    def test_retired_is_not_auto_restored(self):
        t = cpt.next_confidence({"confidence": cpt.RETIRED,
                                 "application_count": 9, "success_count": 9})
        assert not t["changed"]

    def test_no_applications_means_no_judgement(self):
        t = cpt.next_confidence({"confidence": cpt.HYPOTHESIS, "application_count": 0})
        assert not t["changed"]
        assert "判定しない" in t["reason"]


class TestConstraintEligibility:
    def test_hypothesis_cannot_be_a_constraint(self):
        assert cpt.usable_as_constraint(
            {"confidence": cpt.HYPOTHESIS, "sources": ["a", "b"]})["usable"] is False

    def test_doubtful_cannot_be_a_constraint(self):
        assert cpt.usable_as_constraint(
            {"confidence": cpt.DOUBTFUL, "sources": ["a", "b"]})["usable"] is False

    def test_single_source_concept_is_held_back(self):
        """外部から注入された知識をそのまま制約にしない（緩慢な汚染への防御）。"""
        r = cpt.usable_as_constraint({"confidence": cpt.VERIFIED, "sources": ["one"],
                                      "application_count": 0})
        assert r["usable"] is False
        assert "注入" in r["reason"]

    def test_verified_with_multiple_sources_is_usable(self):
        assert cpt.usable_as_constraint(
            {"confidence": cpt.VERIFIED, "sources": ["a", "b"],
             "application_count": 3})["usable"] is True


class TestDuplicatePrevention:
    def test_exact_name_is_found(self):
        existing = [{"name": "半導体サイクル", "aliases": []}]
        hits = cpt.find_similar("半導体サイクル", existing)
        assert hits and hits[0]["reason"] == "完全一致"

    def test_substring_is_found(self):
        existing = [{"name": "半導体サイクル", "aliases": []}]
        hits = cpt.find_similar("半導体サイクルの在庫調整", existing)
        assert hits

    def test_alias_is_found(self):
        existing = [{"name": "レバレッジETFの逓減",
                     "aliases": ["ボラティリティドラッグ"]}]
        assert cpt.find_similar("ボラティリティドラッグ", existing)

    def test_count_guidance_warns_above_fifty(self):
        assert cpt.count_guidance(60)["warn"] is True
        assert cpt.count_guidance(20)["label"] == "健全"


class TestGenealogy:
    SOURCES = {
        "src_a": {"id": "src_a", "depth": 0, "provenance": schema.PRIMARY, "title": "10-Q"},
        "src_b": {"id": "src_b", "depth": 2, "provenance": schema.PERSONAL, "title": "解説"},
    }

    def test_no_source_is_detected(self):
        r = genealogy.classify_thesis({"symbol": "QCOM"}, self.SOURCES)
        assert r["state"] == genealogy.NO_SOURCE
        assert "遡れません" in r["label"]

    def test_primary_backing_is_grounded(self):
        r = genealogy.classify_thesis({"symbol": "QCOM", "sources": ["src_a"]}, self.SOURCES)
        assert r["state"] == genealogy.GROUNDED

    def test_personal_only_is_flagged(self):
        r = genealogy.classify_thesis({"symbol": "QCOM", "sources": ["src_b"]}, self.SOURCES)
        assert r["state"] == genealogy.PERSONAL_ONLY

    def test_missing_ids_are_reported(self):
        r = genealogy.classify_thesis({"symbol": "X", "sources": ["src_zzz"]}, self.SOURCES)
        assert r["missing_ids"] == ["src_zzz"]

    def test_zero_theses_is_not_called_healthy(self, tmp_vault):
        r = genealogy.audit_theses([])
        assert "根を張る対象が無い" in r["message"]

    def test_all_ungrounded_says_so_plainly(self, tmp_vault):
        r = genealogy.audit_theses([{"symbol": "QCOM"}, {"symbol": "MDT"}])
        assert "0件" in r["message"]
        assert r["counts"][genealogy.NO_SOURCE] == 2


class TestAssumptionConcentration:
    def test_missing_assumptions_is_not_diversified(self, tmp_vault):
        r = genealogy.assumption_concentration([{"symbol": "QCOM"}])
        assert r["available"] is False
        assert "分散している』という意味ではありません" in r["reason"]

    def test_shared_assumption_shows_concentration(self, tmp_vault):
        cpt.save(cpt.build(name="半導体サイクル", category=cpt.MECHANISM,
                           body="在庫調整で回る。", counterexample="2020年は例外だった。",
                           is_assumption=True))
        theses = [{"symbol": "SOXL", "concepts": ["半導体サイクル"]},
                  {"symbol": "QCOM", "concepts": ["半導体サイクル"]}]
        r = genealogy.assumption_concentration(theses)
        assert r["available"] is True
        assert r["hhi"] == pytest.approx(1.0)
        assert r["level"] == "danger"
        assert set(r["by_concept"]["半導体サイクル"]) == {"SOXL", "QCOM"}
