"""多段フォールバック解決のテスト.

## 運用ルール（2026-08-09）

> あるリソースから情報が取得できなかったら「取得できなかった」と書くんじゃなくて、
> **他のあらゆる手段を講じて取得できるまでトライすること。**

これまでの「取得失敗を結果と混同しない」は正しいが不十分だった。
正直に「取れなかった」と書くだけで、**取りに行く努力をしていなかった。**

## 縛っていること

1. **最初の失敗で諦めない。** 次の手段を試す
2. **どの経路で取れたかを記録する。** 予備で取った値を一次経路の値のように扱わない
3. **全滅して初めて失敗と書く。** そのとき試した手段を全部列挙する
"""

from __future__ import annotations

import pytest

from src.data import resolver as R


class TestResolveChain:
    def test_first_success_wins(self):
        calls = []

        def a():
            calls.append("a")
            return 100

        def b():
            calls.append("b")
            return 200

        r = R.resolve([("A", a), ("B", b)])
        assert r["value"] == 100
        assert r["source"] == "A"
        assert r["fallback_used"] is False
        assert calls == ["a"], "成功しているのに次の手段まで試している"

    def test_falls_through_to_the_next_source(self):
        r = R.resolve([
            ("A", lambda: None),
            ("B", lambda: 42),
        ])
        assert r["value"] == 42
        assert r["source"] == "B"
        assert r["fallback_used"] is True
        assert "予備経路を使用" in r["note"]

    def test_exception_does_not_stop_the_chain(self):
        def boom():
            raise TimeoutError("timed out")

        r = R.resolve([("A", boom), ("B", lambda: 7)])
        assert r["value"] == 7
        assert r["attempts"][0]["ok"] is False
        assert "TimeoutError" in r["attempts"][0]["error"]

    def test_all_failures_list_every_attempt(self):
        """**全滅して初めて失敗と書く。そのとき手段を全部挙げる。**"""
        r = R.resolve([
            ("A", lambda: None),
            ("B", lambda: None),
            ("C", lambda: None),
        ], label="価格")
        assert r["available"] is False
        assert "試した手段: A、B、C" in r["note"]
        assert "データが存在しない』ではありません" in r["note"]
        assert len(r["attempts"]) == 3

    def test_custom_validity(self):
        r = R.resolve([("A", lambda: 0), ("B", lambda: 5)],
                      is_valid=lambda v: isinstance(v, int) and v > 0)
        assert r["value"] == 5

    def test_empty_chain(self):
        r = R.resolve([])
        assert r["available"] is False
        assert "試行なし" in r["note"]


class TestResolvePrice:
    def test_price_validity_rejects_zero_and_none(self):
        assert R._is_price(120.5) is True
        assert R._is_price(0) is False
        assert R._is_price(-1) is False
        assert R._is_price(None) is False
        assert R._is_price(True) is False       # bool は数値として扱わない

    def test_japanese_symbol_skips_finnhub(self, monkeypatch):
        """finnhub は日本株に対応しない。無駄な経路を並べない。"""
        seen = []

        def spy(chain, **kwargs):
            seen.extend(name for name, _ in chain)
            return {"available": False, "attempts": [], "note": ""}

        monkeypatch.setattr(R, "resolve", spy)
        R.resolve_price("2802.T")
        assert "finnhub.quote" not in seen

    def test_us_symbol_includes_finnhub(self, monkeypatch):
        seen = []

        def spy(chain, **kwargs):
            seen.extend(name for name, _ in chain)
            return {"available": False, "attempts": [], "note": ""}

        monkeypatch.setattr(R, "resolve", spy)
        R.resolve_price("QCOM")
        assert "finnhub.quote" in seen

    def test_chain_order_tries_cheapest_first(self, monkeypatch):
        seen = []

        def spy(chain, **kwargs):
            seen.extend(name for name, _ in chain)
            return {"available": False, "attempts": [], "note": ""}

        monkeypatch.setattr(R, "resolve", spy)
        R.resolve_price("QCOM")
        assert seen[0] == "yfinance.info"
        assert "yfinance.fast_info" in seen
        assert "yfinance.download" in seen


class TestResolveFundamentals:
    def test_sec_is_offered_for_us_only(self, monkeypatch):
        seen = []

        def spy(chain, **kwargs):
            seen.extend(name for name, _ in chain)
            return {"available": False, "attempts": [], "note": ""}

        monkeypatch.setattr(R, "resolve", spy)
        R.resolve_fundamentals("2802.T")
        assert "sec.xbrl" not in seen
        seen.clear()
        R.resolve_fundamentals("QCOM")
        assert "sec.xbrl" in seen


class TestResolveNews:
    def test_finnhub_then_yfinance(self, monkeypatch):
        seen = []

        def spy(chain, **kwargs):
            seen.extend(name for name, _ in chain)
            return {"available": False, "attempts": [], "note": ""}

        monkeypatch.setattr(R, "resolve", spy)
        R.resolve_news("2802.T")
        assert seen == ["finnhub.company_news", "yfinance.news"]


class TestDegradedInfo:
    def test_minimal_info_records_the_source(self, monkeypatch):
        """予備で取った値を一次経路の値のように見せない。"""
        from src.data.yahoo_client import detail

        monkeypatch.setattr(
            "src.data.resolver.resolve_price",
            lambda s: {"available": True, "value": 123.4,
                       "source": "yfinance.fast_info", "fallback_used": True,
                       "attempts": [{"source": "yfinance.info", "ok": False}]})
        monkeypatch.setattr(
            "src.data.resolver.resolve_history",
            lambda s, period="1y": {"available": False})

        got = detail._resolve_minimal("QCOM")
        assert got["price"] == 123.4
        assert got["price_source"] == "yfinance.fast_info"
        assert got["degraded"] is True
        # 指標が無いことを「0」と読ませない
        assert "0ではありません" in got["degraded_note"]

    def test_total_failure_returns_none(self, monkeypatch):
        from src.data.yahoo_client import detail

        monkeypatch.setattr(
            "src.data.resolver.resolve_price",
            lambda s: {"available": False, "value": None,
                       "attempts": [{"source": "yfinance.info", "ok": False},
                                    {"source": "finnhub.quote", "ok": False}]})
        assert detail._resolve_minimal("QCOM") is None
        # 全経路を試したことが記録に残る
        assert "全経路で価格を取得できませんでした" in (
            detail.last_fetch_error("QCOM") or "")
