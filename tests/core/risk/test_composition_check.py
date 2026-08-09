"""構成の自己検証のテスト.

## なぜ必要か

投信の構成は運用会社の月次レポートにしか無く、機械では確認できない。
だから `verified_as_of: null`（未確認）のまま置くしかなかった。

**だが「確認できない」と「検証できない」は違う。**
構成が正しければ等ウェイトのバスケットは指数とほぼ同じ動きをする。
指数の時系列は取れるのだから、精度は測れる。

## 縛っていること

1. **これは構成の証明ではない**と必ず書く（似た値動きの別銘柄でも相関は出る）
2. 指数が取れなければ**「検証できなかった」**。合格にしない
3. leave-one-out で構成外の疑いを名指しする
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.core.risk import composition_check as CC


def _series(values, start="2025-08-01"):
    idx = pd.bdate_range(start=start, periods=len(values))
    return pd.Series(values, index=idx)


def _walk(n=260, drift=0.0005, vol=0.01, seed=0):
    rng = np.random.default_rng(seed)
    steps = rng.normal(drift, vol, n)
    return 100.0 * np.cumprod(1 + steps)


@pytest.fixture
def fake_prices(monkeypatch):
    """指数 = 3銘柄の等ウェイト（＝完全に一致するはず）。"""
    a, b, c = _walk(seed=1), _walk(seed=2), _walk(seed=3)
    basket = (a / a[0] + b / b[0] + c / c[0]) / 3 * 100
    noise = _walk(seed=9, drift=0.0, vol=0.03)      # 無関係な銘柄

    table = {"A": _series(a), "B": _series(b), "C": _series(c),
             "NOISE": _series(noise), "^IDX": _series(basket)}
    monkeypatch.setattr(CC, "_returns",
                        lambda sym, period="1y": table.get(sym))
    return table


class TestVerification:
    def test_correct_composition_tracks_well(self, fake_prices):
        r = CC.verify_composition(["A", "B", "C"], "^IDX")
        assert r["available"] is True
        assert r["correlation"] > 0.95
        assert r["tracking_error_pct"] < 5.0
        assert "良好" in r["verdict"]

    def test_wrong_composition_is_detected(self, fake_prices):
        r = CC.verify_composition(["A", "B", "NOISE"], "^IDX")
        assert r["tracking_error_pct"] > 5.0
        assert "良好" not in r["verdict"]

    def test_leave_one_out_names_the_suspect(self, fake_prices):
        """外して改善する銘柄＝構成外の疑い。"""
        r = CC.verify_composition(["A", "B", "C", "NOISE"], "^IDX")
        suspects = [s["symbol"] for s in r["suspects"]]
        assert "NOISE" in suspects
        assert r["suspects"][0]["improvement_pct"] > 0

    def test_no_suspects_for_correct_composition(self, fake_prices):
        r = CC.verify_composition(["A", "B", "C"], "^IDX")
        assert r["suspects"] == []


class TestHonesty:
    def test_never_claims_proof(self, fake_prices):
        r = CC.verify_composition(["A", "B", "C"], "^IDX")
        assert "構成の証明ではありません" in r["note"]

    def test_missing_index_is_not_a_pass(self, monkeypatch):
        monkeypatch.setattr(CC, "_returns", lambda sym, period="1y": None)
        r = CC.verify_composition(["A", "B"], "^IDX")
        assert r["available"] is False
        assert "『検証済み』ではありません" in r["note"]

    def test_missing_constituents_are_listed(self, fake_prices, monkeypatch):
        original = CC._returns
        monkeypatch.setattr(
            CC, "_returns",
            lambda sym, period="1y": None if sym == "C" else original(sym, period))
        r = CC.verify_composition(["A", "B", "C"], "^IDX")
        assert "C" in r["missing"]

    def test_too_few_constituents(self, monkeypatch):
        idx = _series(_walk())
        monkeypatch.setattr(
            CC, "_returns",
            lambda sym, period="1y": idx if sym == "^IDX" else None)
        r = CC.verify_composition(["A", "B", "C"], "^IDX")
        assert r["available"] is False
        assert "足りず" in r["note"]


class TestFormatting:
    def test_report_shows_metrics_and_caveat(self, fake_prices):
        text = CC.format_composition_check(
            {"テスト投信": CC.verify_composition(["A", "B", "C", "NOISE"], "^IDX")})
        assert "相関" in text and "トラッキング誤差" in text
        assert "構成の証明ではありません" in text
        assert "NOISE" in text

    def test_empty_input(self):
        assert CC.format_composition_check(None) == ""

    def test_unavailable_is_shown(self):
        text = CC.format_composition_check(
            {"X": {"available": False, "note": "指数が取れませんでした"}})
        assert "指数が取れませんでした" in text


def test_configured_funds_use_the_index_proxy(monkeypatch):
    """`explicit_constituents` と `technical_proxies` を突き合わせる。"""
    cfg = {
        "explicit_constituents": {
            "iFreeNEXT FANG+": {"symbols": ["A", "B"], "verified_as_of": None}},
        "technical_proxies": {"iFreeNEXT FANG+": {"proxy": "^NYFANG"}},
    }
    seen = {}

    def fake(symbols, index_symbol, **k):
        seen["symbols"] = symbols
        seen["index"] = index_symbol
        return {"available": True, "note": "ok"}

    monkeypatch.setattr(CC, "verify_composition", fake)
    out = CC.verify_configured_funds(cfg)
    assert seen["index"] == "^NYFANG"
    assert seen["symbols"] == ["A", "B"]
    assert out["iFreeNEXT FANG+"]["verified_as_of"] is None


def test_missing_index_definition_is_reported():
    cfg = {"explicit_constituents": {"X": {"symbols": ["A"]}},
           "technical_proxies": {}}
    out = CC.verify_configured_funds(cfg)
    assert out["X"]["available"] is False
    assert "指数が定義されていません" in out["X"]["note"]
