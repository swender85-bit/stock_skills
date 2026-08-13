"""V0 — 保有の記述状態4分類。

「孤児0件」と「thesis は2件しか無い」が同じレポートに並ぶ矛盾を再発させない。
"""
import pytest

from src.core.portfolio import description_state as ds


def _intent(thesis=False, policy=False, falsification=None):
    theses = []
    if thesis:
        theses = [{"content": "なぜ持つか", "falsification": falsification}]
    return {"has_thesis": thesis, "has_policy": policy, "theses": theses,
            "policies": [{"id": "p1"}] if policy else []}


class TestClassify:
    def test_healthy_needs_all_three(self):
        assert ds.classify_position(
            _intent(thesis=True, policy=True, falsification="price <= 158")) == ds.HEALTHY

    def test_thesis_and_policy_without_falsification_is_not_healthy(self):
        """何があったら間違いだったと分かるか、が無い状態を健全と呼ばない。"""
        assert ds.classify_position(_intent(thesis=True, policy=True)) == ds.UNRULED

    def test_policy_without_thesis_is_ungrounded(self):
        """3xスリーブ・FANG+ の形。旧実装はこれを孤児から除外していた。"""
        assert ds.classify_position(_intent(policy=True)) == ds.UNGROUNDED

    def test_thesis_without_policy_is_unruled(self):
        assert ds.classify_position(_intent(thesis=True, falsification="x<=1")) == ds.UNRULED

    def test_neither_is_orphan(self):
        assert ds.classify_position(_intent()) == ds.ORPHAN

    def test_empty_falsification_does_not_count(self):
        for empty in ("", "   ", [], {}, None, [None, ""]):
            assert ds.classify_position(
                _intent(thesis=True, policy=True, falsification=empty)) == ds.UNRULED


class TestDescribe:
    def _portfolio(self):
        """2026-08-08 の実データに対応する10ポジション。"""
        return [
            {"symbol": "QCOM", "weight_pct": 11.2,
             "intent": _intent(thesis=True, falsification="price <= 158")},
            {"symbol": "MDT", "weight_pct": 6.1, "intent": _intent(thesis=True)},
            {"symbol": "SOXL", "weight_pct": 31.0, "intent": _intent(policy=True)},
            {"symbol": "TECL", "weight_pct": 20.0, "intent": _intent(policy=True)},
            {"symbol": "TQQQ", "weight_pct": 11.0, "intent": _intent(policy=True)},
            {"name": "iFreeNEXT FANG+", "weight_pct": 6.5, "intent": _intent(policy=True)},
            {"symbol": "2737.T", "weight_pct": 2.7, "intent": _intent()},
            {"symbol": "2802.T", "weight_pct": 9.0, "intent": _intent()},
            {"symbol": "2802.T", "weight_pct": 0.9, "intent": _intent()},
            {"symbol": "9843.T", "weight_pct": 3.7, "intent": _intent()},
        ]

    def test_matches_the_spec_counts(self):
        d = ds.describe(self._portfolio())
        assert d["counts"][ds.HEALTHY] == 0
        assert d["counts"][ds.UNRULED] == 2      # QCOM, MDT
        assert d["counts"][ds.UNGROUNDED] == 4   # 3x + FANG+
        assert d["counts"][ds.ORPHAN] == 4       # 日本株
        assert d["total"] == 10

    def test_headline_is_healthy_zero_not_orphan_zero(self):
        d = ds.describe(self._portfolio())
        assert d["healthy_count"] == 0
        joined = " ".join(d["messages"])
        assert "健全なポジションは 0 / 10" in joined

    def test_ungrounded_is_called_more_dangerous_than_orphan(self):
        d = ds.describe(self._portfolio())
        joined = " ".join(d["messages"])
        assert "孤児より危険" in joined

    def test_explains_why_the_old_number_was_zero(self):
        d = ds.describe(self._portfolio())
        joined = " ".join(d["messages"])
        assert "AND条件" in joined

    def test_weights_are_summed_per_state(self):
        d = ds.describe(self._portfolio())
        assert d["weight_pct"][ds.UNGROUNDED] == pytest.approx(68.5, abs=0.05)
        assert d["weight_pct"][ds.ORPHAN] == pytest.approx(16.3, abs=0.05)

    def test_empty_portfolio_does_not_crash(self):
        d = ds.describe([])
        assert d["total"] == 0
        assert d["messages"]

    def test_block_lists_every_state(self):
        block = ds.format_block(ds.describe(self._portfolio()))
        for label in ds.LABELS.values():
            assert label in block
        assert "SOXL" in block and "2737.T" in block


class TestReconciliationWiring:
    def test_reconcile_carries_the_description(self):
        from src.core.portfolio.reconciliation import reconcile

        model = [{"symbol": "QCOM", "name": "Qualcomm", "shares": 85},
                 {"symbol": "2737.T", "name": "トーメンデバイス", "shares": 40}]
        r = reconcile(model, [], values_jpy={"QCOM": 2_400_000, "2737.T": 600_000},
                      total_jpy=3_000_000)
        assert r.get("description") is not None
        assert r["description"]["total"] == 2
        assert set(r["description"]["counts"]) == set(ds.ORDER)

    def test_orphans_key_is_still_present(self):
        """後方互換。既存の表示・テストを壊さない。"""
        from src.core.portfolio.reconciliation import reconcile

        r = reconcile([{"symbol": "2737.T", "name": "t", "shares": 40}], [])
        assert "orphans" in r
