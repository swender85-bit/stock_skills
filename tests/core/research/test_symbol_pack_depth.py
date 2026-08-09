"""個別銘柄パックが週次と同じ深さを持つことのテスト.

## 名指しする問題

> 週次報告のためだけに改善しようとしている雰囲気を感じる。
> 通常の質問にも全部適用してほしい。

実際、政策・反証条件・一次観測・外部批評家は PF パックにしか入っておらず、
「〇〇売るべき？」という**最も政策が要る場面**で政策が引かれていなかった。

さらに ETF について「SOXL ってどう？」と聞かれたとき、
**中身を開けずに価格とRSIだけ返していた**。それは中身を見ずに答えているのと同じ。

## 縛っていること

**判断の質を、質問の形式で変えない。**
個別パックにも週次と同じ層が入っていること。
"""

from __future__ import annotations

import pytest

from src.core.research import briefing_pack as BP

#: 個別パックに必ず存在すべき層（週次と同じもの）
REQUIRED_LAYERS = (
    "policy",            # 政策（急変時はこれが先）
    "falsification",     # 反証条件（価格より信念の変化）
    "primary_filings",   # 開示原文（深度0の錨）
    "external_views",    # 外部批評家（citation 付き）
    "assumption_space",  # 前提の衝突
    "lookthrough",       # ETFなら中身
    "constituents",      # 構成銘柄の判断材料
    "forward_horizon",   # 数ヶ月先の日程
    "leverage_sleeve",   # ドラッグ・重複・感応度
    "regime",            # 市況レジーム
)


class TestSymbolLookthrough:
    def test_none_weight_does_not_blank_the_expansion(self):
        """`setdefault` は None を置き換えない。

        holding_row は weight_pct を明示的に None にしているため、
        `setdefault("weight_pct", 100.0)` では展開が丸ごと空になる（実際なった）。
        """
        captured = {}

        def fake_build(rows):
            captured["rows"] = rows
            return {"available": True, "effective": [], "resolved_etfs": []}

        import src.core.risk.etf_lookthrough as LT

        original = LT.build_lookthrough
        LT.build_lookthrough = fake_build
        try:
            BP._safe_symbol_lookthrough({"symbol": "SOXL", "weight_pct": None,
                                         "leverage": 3})
        finally:
            LT.build_lookthrough = original

        assert captured["rows"][0]["weight_pct"] == 100.0, \
            "weight_pct が None のまま渡され、中身が展開されない"

    def test_existing_weight_is_preserved(self):
        captured = {}

        def fake_build(rows):
            captured["rows"] = rows
            return {"available": True, "effective": [], "resolved_etfs": []}

        import src.core.risk.etf_lookthrough as LT

        original = LT.build_lookthrough
        LT.build_lookthrough = fake_build
        try:
            BP._safe_symbol_lookthrough({"symbol": "SOXL", "weight_pct": 27.5})
        finally:
            LT.build_lookthrough = original
        assert captured["rows"][0]["weight_pct"] == 27.5

    def test_failure_is_not_reported_as_empty_contents(self):
        import src.core.risk.etf_lookthrough as LT

        original = LT.build_lookthrough

        def boom(rows):
            raise RuntimeError("nope")

        LT.build_lookthrough = boom
        try:
            got = BP._safe_symbol_lookthrough({"symbol": "SOXL"})
        finally:
            LT.build_lookthrough = original
        assert got["available"] is False
        assert "『中身が無い』ではありません" in got["note"]


class TestLayersArePresent:
    """パック生成をモックして、**層が抜けていないこと**だけを検証する。

    実データ取得は遅く不安定なので、ここでは構造だけを縛る。
    """

    def test_required_layers_are_wired(self, monkeypatch):
        import inspect

        source = inspect.getsource(BP.build_symbol_briefing)
        for layer in REQUIRED_LAYERS:
            assert f'"{layer}"' in source, \
                f"個別パックに {layer} が配線されていない（週次にはある）"

    def test_symbol_pack_uses_hundred_percent_basis(self):
        """単一銘柄の分析では「その銘柄が100%」として感応度を見る。

        実際の保有額を混ぜると、金額が独り歩きして誤読される。
        """
        import inspect

        source = inspect.getsource(BP.build_symbol_briefing)
        assert "sleeve_row" in source
        assert '"weight_pct": 100.0' in source


class TestPolicyIsFirst:
    def test_policy_helper_never_raises(self):
        got = BP._safe_symbol_policy({"symbol": "ZZZZ", "fundamentals": {},
                                      "technicals": {}})
        assert "has_policy" in got

    def test_policy_failure_is_not_no_policy(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("x")

        monkeypatch.setattr("src.core.policy.policy_response", boom)
        got = BP._safe_symbol_policy({"symbol": "QCOM"})
        assert got["has_policy"] is False
        # 「政策が無い」と「照会できなかった」を混同しない
        assert "『政策が無い』ではありません" in got["answer"]


def test_leverage_lookup_reads_current_holdings():
    """レバレッジ倍率は保有設定から引く（金利ゲートの適用判定に使う）。"""
    assert BP._leverage_of("SOXL") == 3
    assert BP._leverage_of("QCOM") in (None, 1)
    assert BP._leverage_of("NOT_HELD") is None
