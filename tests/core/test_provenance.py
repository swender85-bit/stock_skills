"""認識の系譜会計のテスト -- Fable5 第2弾 案C.

受け入れ基準:
  P1 四系譜が排他的に型付く
  P2 型なし書込が拒否される
  P3 深度が正しく計算され、循環が拒否される
  P4 構成比・最大深度が出力される
  P5 閾値超過主張が再導出なしに新解釈へ混入しない
"""

from datetime import datetime, timezone

import pytest

from src.core.provenance import (
    DEPTH_LIMIT_HOLDING,
    DEPTH_LIMIT_WATCH,
    EXTERNAL,
    LEGACY,
    PRIMARY,
    SELF,
    USER,
    CircularDerivationError,
    UntypedClaimError,
    build_claim,
    claims_from_decision_package,
    classify_source,
    filter_usable,
    link_derivation,
    load_claims,
    needs_regrounding,
    provenance_summary,
    reground,
    regrounding_queue,
    save_claim,
    trace_to_primary,
)


AT = datetime(2026, 7, 18, tzinfo=timezone.utc)


def _chain(depth: int):
    """深度 depth の自己推論チェーンを作る。"""
    root = build_claim("決算短信の原文", PRIMARY, symbol="7203.T", at=AT)
    chain = [root]
    current = root
    for i in range(depth):
        current = build_claim(
            f"推論{i}", SELF, symbol="7203.T", derived_from=[current], at=AT
        )
        chain.append(current)
    return chain


class TestTyping:
    """案C P1/P2。"""

    def test_untyped_claim_is_rejected(self):
        with pytest.raises(UntypedClaimError, match="系譜"):
            build_claim("なんとなく強気", "", symbol="7203.T")

    def test_unknown_provenance_is_rejected(self):
        with pytest.raises(UntypedClaimError, match="未知の系譜"):
            build_claim("強気", "vibes", symbol="7203.T")

    def test_four_provenance_types_are_accepted(self):
        for p in (PRIMARY, EXTERNAL, SELF, USER):
            assert build_claim("x", p, symbol="A", at=AT)["provenance"] == p

    def test_legacy_is_preserved_without_retroactive_typing(self):
        """既存ノードは legacy のまま保持する（遡及型付けは捏造リスク）。"""
        claim = build_claim("昔の主張", LEGACY, symbol="A", at=AT)
        assert claim["provenance"] == LEGACY


class TestSourceClassification:
    def test_disclosure_sites_are_primary(self):
        assert classify_source("https://disclosure.edinet-fsa.go.jp/xxx") == PRIMARY
        assert classify_source("https://www.sec.gov/Archives/xxx") == PRIMARY
        assert classify_source("https://release.tdnet.info/xxx") == PRIMARY

    def test_aggregators_are_not_primary(self):
        """yfinance の加工済み指標は他者集計。一次ではない。"""
        assert classify_source("https://finance.yahoo.com/quote/AAPL") == EXTERNAL

    def test_unknown_domain_defaults_to_external(self):
        assert classify_source("https://someblog.example.com/post") == EXTERNAL

    def test_empty_source_is_external_not_primary(self):
        """出所不明を一次観測に格上げしない（系譜偽装の防止）。"""
        assert classify_source("") == EXTERNAL


class TestDepth:
    """案C P3。"""

    def test_primary_observation_is_depth_zero(self):
        assert build_claim("株価", PRIMARY, at=AT)["depth"] == 0

    def test_external_and_user_are_depth_one(self):
        assert build_claim("ニュース", EXTERNAL, at=AT)["depth"] == 1
        assert build_claim("テーゼ", USER, at=AT)["depth"] == 1

    def test_self_inference_increments_from_ancestors(self):
        chain = _chain(3)
        assert [c["depth"] for c in chain] == [0, 1, 2, 3]

    def test_self_inference_without_ancestors_starts_at_one(self):
        assert build_claim("根拠なき推論", SELF, at=AT)["depth"] == 1

    def test_depth_uses_max_of_ancestors(self):
        shallow = build_claim("浅い", PRIMARY, at=AT)
        deep = _chain(3)[-1]
        combined = build_claim("統合", SELF, derived_from=[shallow, deep], at=AT)
        assert combined["depth"] == 4


class TestCircularDerivation:
    """循環導出は深度が無限になるため作成時に拒否する。"""

    def test_direct_cycle_is_rejected(self):
        a = build_claim("A", PRIMARY, at=AT)
        b = build_claim("B", SELF, derived_from=[a], at=AT)
        with pytest.raises(CircularDerivationError):
            link_derivation(a, b, [a, b])

    def test_indirect_cycle_is_rejected(self):
        a = build_claim("A", PRIMARY, at=AT)
        b = build_claim("B", SELF, derived_from=[a], at=AT)
        c = build_claim("C", SELF, derived_from=[b], at=AT)
        with pytest.raises(CircularDerivationError):
            link_derivation(a, c, [a, b, c])

    def test_valid_link_updates_depth(self):
        a = build_claim("A", PRIMARY, at=AT)
        deep = _chain(2)[-1]
        target = build_claim("T", SELF, derived_from=[a], at=AT)
        link_derivation(target, deep, [a, target, *_chain(2)])
        assert target["depth"] >= 1


class TestTraceToPrimary:
    def test_finds_the_primary_anchor(self):
        chain = _chain(3)
        found = trace_to_primary(chain[-1], chain)
        assert len(found) == 1
        assert found[0]["provenance"] == PRIMARY

    def test_no_primary_anchor_returns_empty(self):
        """一次情報に接地していない結論を検出できる。"""
        news = build_claim("噂", EXTERNAL, at=AT)
        inference = build_claim("結論", SELF, derived_from=[news], at=AT)
        assert trace_to_primary(inference, [news, inference]) == []

    def test_handles_missing_ancestors(self):
        orphan = build_claim("孤児", SELF, at=AT)
        orphan["derived_from"] = ["claim_does_not_exist"]
        assert trace_to_primary(orphan, [orphan]) == []


class TestProvenanceSummary:
    """案C P4。"""

    def test_composition_and_max_depth(self):
        claims = [
            build_claim("価格", PRIMARY, at=AT),
            build_claim("ニュース", EXTERNAL, at=AT),
            build_claim("解釈", SELF, at=AT),
            build_claim("テーゼ", USER, at=AT),
        ]
        result = provenance_summary(claims)
        assert result["total"] == 4
        assert result["composition"][PRIMARY] == 0.25
        assert result["max_depth"] == 1

    def test_summary_string_is_report_ready(self):
        claims = [build_claim("価格", PRIMARY, at=AT), build_claim("解釈", SELF, at=AT)]
        assert "一次観測" in provenance_summary(claims)["summary"]
        assert "最大深度" in provenance_summary(claims)["summary"]

    def test_deep_self_reference_is_flagged_as_contaminated(self):
        chain = _chain(3)
        result = provenance_summary(chain)
        assert result["contaminated"] is True
        assert "独立根拠が薄い" in result["summary"]

    def test_self_dominance_is_flagged(self):
        claims = [
            build_claim("価格", PRIMARY, at=AT),
            build_claim("解釈1", SELF, at=AT),
            build_claim("解釈2", SELF, at=AT),
        ]
        assert provenance_summary(claims)["contaminated"] is True

    def test_grounded_interpretation_is_clean(self):
        claims = [
            build_claim("価格", PRIMARY, at=AT),
            build_claim("開示", PRIMARY, at=AT),
            build_claim("解釈", SELF, at=AT),
        ]
        result = provenance_summary(claims)
        assert result["contaminated"] is False

    def test_empty_input_is_safe(self):
        result = provenance_summary([])
        assert result["total"] == 0
        assert result["contaminated"] is False


class TestRegroundingGate:
    """案C P5: 閾値超過主張が再導出なしに新解釈へ混入しない。"""

    def test_shallow_claims_are_usable(self):
        assert needs_regrounding(_chain(1)[-1]) is False

    def test_deep_claims_require_regrounding_for_holdings(self):
        deep = _chain(DEPTH_LIMIT_HOLDING)[-1]
        assert needs_regrounding(deep, is_holding=True) is True

    def test_watchlist_threshold_is_more_lenient(self):
        """保有=厳格、ウォッチ=緩和の二段。"""
        claim = _chain(DEPTH_LIMIT_HOLDING)[-1]
        assert needs_regrounding(claim, is_holding=True) is True
        assert needs_regrounding(claim, is_holding=False) is False

    def test_filter_separates_usable_from_blocked(self):
        chain = _chain(DEPTH_LIMIT_HOLDING)
        usable, blocked = filter_usable(chain, is_holding=True)
        assert all(c["depth"] < DEPTH_LIMIT_HOLDING for c in usable)
        assert all(c["depth"] >= DEPTH_LIMIT_HOLDING for c in blocked)

    def test_regrounded_claim_becomes_usable_again(self):
        deep = _chain(DEPTH_LIMIT_HOLDING)[-1]
        anchor = build_claim("決算説明資料", PRIMARY, at=AT)
        reground(deep, anchor, at=AT)
        assert deep["depth"] == 1
        assert needs_regrounding(deep) is False

    def test_regrounding_anchor_must_be_primary(self):
        """又聞きニュースへの接地は再接地ではない。"""
        deep = _chain(DEPTH_LIMIT_HOLDING)[-1]
        news = build_claim("ニュース", EXTERNAL, at=AT)
        with pytest.raises(UntypedClaimError, match="一次観測"):
            reground(deep, news)

    def test_queue_is_ordered_deepest_first(self):
        claims = _chain(5)
        queue = regrounding_queue(claims, is_holding=True)
        depths = [c["depth"] for c in queue]
        assert depths == sorted(depths, reverse=True)

    def test_queue_is_empty_when_everything_is_grounded(self):
        assert regrounding_queue(_chain(1)) == []


class TestPersistence:
    def test_save_and_load_roundtrip(self, tmp_path):
        claim = build_claim("価格", PRIMARY, symbol="7203.T", at=AT)
        save_claim(claim, base_dir=str(tmp_path))
        loaded = load_claims(base_dir=str(tmp_path))
        assert len(loaded) == 1
        assert loaded[0]["id"] == claim["id"]

    def test_filter_by_symbol(self, tmp_path):
        save_claim(build_claim("a", PRIMARY, symbol="7203.T", at=AT), base_dir=str(tmp_path))
        save_claim(build_claim("b", PRIMARY, symbol="AAPL", at=AT), base_dir=str(tmp_path))
        assert len(load_claims("AAPL", base_dir=str(tmp_path))) == 1

    def test_missing_dir_returns_empty(self, tmp_path):
        assert load_claims(base_dir=str(tmp_path / "nope")) == []


class TestDecisionPackageBridge:
    """案B の DecisionPackage を Claim の供給源にする（二重実装の回避）。"""

    def test_used_evidence_becomes_claims_and_conclusion_is_self(self):
        from src.core.decision import InformationBoundary, build_package

        package = build_package(
            "7203.T",
            "buy",
            rationale="競争優位あり",
            boundary=InformationBoundary(
                used=[{"label": "決算短信", "disclosed_at": "2026-07-15T15:00:00+09:00"}]
            ),
        )
        claims = claims_from_decision_package(package)

        assert claims[0]["provenance"] == PRIMARY
        assert claims[-1]["provenance"] == SELF
        assert claims[-1]["text"] == "競争優位あり"
        assert claims[-1]["depth"] == 1

    def test_conclusion_traces_back_to_primary(self):
        from src.core.decision import InformationBoundary, build_package

        package = build_package(
            "7203.T", "buy", rationale="強気",
            boundary=InformationBoundary(
                used=[{"label": "決算", "disclosed_at": "2026-07-15T15:00:00+09:00"}]
            ),
        )
        claims = claims_from_decision_package(package)
        assert len(trace_to_primary(claims[-1], claims)) == 1

    def test_package_without_evidence_yields_ungrounded_conclusion(self):
        from src.core.decision import build_package

        package = build_package("7203.T", "buy", rationale="なんとなく")
        claims = claims_from_decision_package(package)
        assert trace_to_primary(claims[-1], claims) == []
