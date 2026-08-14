"""前方カレンダー（数ヶ月先）のテスト.

## なぜ必要か

既存の前方イベントは**翌1週間しか見ていなかった**。その結果レポートは:

> 中身の企業の翌週決算: ゼロ。ETF 経由でも翌週に「決算で飛ぶ」経路は無い。

と書いていたが、その時点で実際には **3週間後に NVDA（実効20%）の決算**が
確定していた。「翌週ゼロ」と「3ヶ月ゼロ」はまったく違う。

## 縛っていること

1. **取得できなかった銘柄を「予定なし」と書かない**（2026-08-08 の再発防止）
2. **ETFに決算は「存在しない」** — 取得失敗と区別する
3. **実効エクスポージャーはレバレッジ込み** — 「いつPFの何%が通過するか」
4. 月別の集中度を出す
"""

from __future__ import annotations

from datetime import date

import pytest

from src.core.risk.forward_horizon import build_forward_horizon, format_horizon

TODAY = date(2026, 8, 9)

HOLDINGS = [
    {"symbol": "SOXL", "name": "Direxion 半導体3x", "weight_pct": 27.5, "leverage": 3},
    {"symbol": "MDT", "name": "メドトロニック", "weight_pct": 6.2},
    {"symbol": "QCOM", "name": "クアルコム", "weight_pct": 9.9},
]

LOOKTHROUGH = {
    "effective": [
        {"symbol": "NVDA", "effective_pct": 20.045, "direct_pct": 0.0,
         "via_etf_pct": 20.045, "sources": ["SOXL", "TECL"]},
        {"symbol": "AMAT", "effective_pct": 6.187, "direct_pct": 0.0,
         "via_etf_pct": 6.187, "sources": ["SOXL"]},
        {"symbol": "QCOM", "effective_pct": 10.158, "direct_pct": 9.9,
         "via_etf_pct": 0.258, "sources": ["SOXL"]},
    ]
}


def _events(mapping):
    """symbol -> events_by_symbol の形を作る。"""
    return {k: {"available": True, "source": "yfinance", **v}
            for k, v in mapping.items()}


class TestHorizonWindow:
    def test_sees_beyond_next_week(self):
        """**これが本丸。** 翌週だけ見ていると NVDA 8/27 を見落とす。"""
        ev = _events({
            "NVDA": {"earnings_dates": ["2026-08-27"]},
            "AMAT": {"earnings_dates": ["2026-08-14"]},
            "MDT": {"earnings_dates": ["2026-09-01"]},
            "QCOM": {"earnings_dates": ["2026-10-30"]},
            "SOXL": {"earnings_dates": []},
        })
        r = build_forward_horizon(HOLDINGS, LOOKTHROUGH, as_of=TODAY,
                                  events_by_symbol=ev)
        dates = {e["symbol"]: e["date"] for e in r["events"] if e["kind"] == "earnings"}
        assert dates["AMAT"] == "2026-08-14"     # 翌週内
        assert dates["NVDA"] == "2026-08-27"     # 翌週の外。**従来は見えなかった**
        assert dates["MDT"] == "2026-09-01"
        assert dates["QCOM"] == "2026-10-30"

    def test_events_beyond_horizon_are_excluded(self):
        ev = _events({"NVDA": {"earnings_dates": ["2027-03-01"]},
                      "AMAT": {"earnings_dates": []},
                      "QCOM": {"earnings_dates": []},
                      "MDT": {"earnings_dates": []},
                      "SOXL": {"earnings_dates": []}})
        r = build_forward_horizon(HOLDINGS, LOOKTHROUGH, as_of=TODAY,
                                  events_by_symbol=ev, horizon_days=90)
        assert [e for e in r["events"] if e["kind"] == "earnings"] == []

    def test_past_events_are_excluded(self):
        ev = _events({"NVDA": {"earnings_dates": ["2026-05-20"]},
                      "AMAT": {}, "QCOM": {}, "MDT": {}, "SOXL": {}})
        r = build_forward_horizon(HOLDINGS, LOOKTHROUGH, as_of=TODAY,
                                  events_by_symbol=ev)
        assert not [e for e in r["events"] if e["symbol"] == "NVDA"]


class TestEffectiveExposure:
    def test_leveraged_exposure_is_attached(self):
        ev = _events({"NVDA": {"earnings_dates": ["2026-08-27"]},
                      "AMAT": {}, "QCOM": {}, "MDT": {}, "SOXL": {}})
        r = build_forward_horizon(HOLDINGS, LOOKTHROUGH, as_of=TODAY,
                                  events_by_symbol=ev)
        nvda = next(e for e in r["events"] if e["symbol"] == "NVDA")
        assert nvda["effective_pct"] == pytest.approx(20.045)
        assert nvda["held_directly"] is False
        assert "SOXL" in nvda["via"]

    def test_direct_and_via_are_combined(self):
        """QCOM は直接保有 + SOXL 経由。**二重計上を見える形にする。**"""
        ev = _events({"QCOM": {"earnings_dates": ["2026-10-30"]},
                      "NVDA": {}, "AMAT": {}, "MDT": {}, "SOXL": {}})
        r = build_forward_horizon(HOLDINGS, LOOKTHROUGH, as_of=TODAY,
                                  events_by_symbol=ev)
        q = next(e for e in r["events"] if e["symbol"] == "QCOM")
        assert q["held_directly"] is True
        assert q["direct_pct"] > 0 and q["via_etf_pct"] > 0

    def test_ex_dividend_carries_via(self):
        ev = _events({"NVDA": {"ex_dividend_date": "2026-08-20"},
                      "AMAT": {}, "QCOM": {}, "MDT": {}, "SOXL": {}})
        r = build_forward_horizon(HOLDINGS, LOOKTHROUGH, as_of=TODAY,
                                  events_by_symbol=ev)
        d = next(e for e in r["events"] if e["kind"] == "ex_dividend")
        assert d["via"] == ["SOXL", "TECL"]
        assert "損失ではありません" in d["note"]


class TestMissingDataIsNotSilence:
    def test_fetch_failure_is_never_reported_as_no_events(self):
        """2026-08-08 の再発防止。通信断を「決算ゼロ」と書かない。"""
        ev = {"NVDA": {"available": False, "error": "timeout"},
              "AMAT": {"available": False, "error": "timeout"},
              "QCOM": {"available": False, "error": "timeout"},
              "MDT": {"available": False, "error": "timeout"},
              "SOXL": {"available": False, "error": "timeout"}}
        r = build_forward_horizon(HOLDINGS, LOOKTHROUGH, as_of=TODAY,
                                  events_by_symbol=ev)
        failed = {u["symbol"] for u in r["unavailable"]}
        assert {"NVDA", "AMAT", "QCOM", "MDT"} <= failed
        assert "予定なし』ではありません" in r["note"]
        assert "取得できませんでした" in r["note"]

    def test_etf_has_no_earnings_rather_than_failed(self):
        """ETFに決算は**存在しない**。取得失敗と混同しない。"""
        ev = {"SOXL": {"available": False, "error": "決算日を取得できませんでした"},
              "NVDA": {"available": True, "earnings_dates": []},
              "AMAT": {"available": True, "earnings_dates": []},
              "QCOM": {"available": True, "earnings_dates": []},
              "MDT": {"available": True, "earnings_dates": []}}
        r = build_forward_horizon(HOLDINGS, LOOKTHROUGH, as_of=TODAY,
                                  events_by_symbol=ev)
        assert "SOXL" in r["no_earnings"]
        assert "SOXL" not in {u["symbol"] for u in r["unavailable"]}

    def test_successful_fetch_with_no_events_is_distinguished(self):
        ev = _events({"NVDA": {"earnings_dates": []}, "AMAT": {"earnings_dates": []},
                      "QCOM": {"earnings_dates": []}, "MDT": {"earnings_dates": []},
                      "SOXL": {"earnings_dates": []}})
        r = build_forward_horizon(HOLDINGS, LOOKTHROUGH, as_of=TODAY,
                                  events_by_symbol=ev)
        assert r["unavailable"] == []
        assert "NVDA" in r["no_earnings"]
        assert "取得は成功" in r["note"]


class TestConcentration:
    def test_month_concentration_is_computed(self):
        ev = _events({
            "NVDA": {"earnings_dates": ["2026-10-29"]},
            "AMAT": {"earnings_dates": ["2026-10-22"]},
            "QCOM": {"earnings_dates": ["2026-10-30"]},
            "MDT": {"earnings_dates": ["2026-09-01"]},
            "SOXL": {"earnings_dates": []},
        })
        r = build_forward_horizon(HOLDINGS, LOOKTHROUGH, as_of=TODAY,
                                  events_by_symbol=ev)
        oct_bucket = r["by_month"]["2026-10"]
        assert oct_bucket["count"] == 3
        # NVDA 20.045 + AMAT 6.187 + QCOM 10.158
        assert oct_bucket["effective_pct"] == pytest.approx(36.39, abs=0.1)
        assert r["concentration"]["hot_months"], "集中月を検出できていない"

    def test_quiet_months_produce_no_warning(self):
        ev = _events({"MDT": {"earnings_dates": ["2026-09-01"]},
                      "NVDA": {}, "AMAT": {}, "QCOM": {}, "SOXL": {}})
        r = build_forward_horizon(HOLDINGS, LOOKTHROUGH, as_of=TODAY,
                                  events_by_symbol=ev)
        assert r["concentration"]["hot_months"] == []
        assert "集中している決算はありません" in r["concentration"]["message"]


class TestConfidence:
    def test_far_dates_are_marked_less_certain(self):
        ev = _events({"AMAT": {"earnings_dates": ["2026-08-14"]},
                      "QCOM": {"earnings_dates": ["2026-10-30"]},
                      "NVDA": {}, "MDT": {}, "SOXL": {}})
        r = build_forward_horizon(HOLDINGS, LOOKTHROUGH, as_of=TODAY,
                                  events_by_symbol=ev)
        near = next(e for e in r["events"] if e["symbol"] == "AMAT")
        far = next(e for e in r["events"] if e["symbol"] == "QCOM")
        assert near["confidence"].startswith("高")
        assert far["confidence"].startswith("低")


class TestFormatting:
    def test_table_shows_effective_pct_and_via(self):
        ev = _events({"NVDA": {"earnings_dates": ["2026-08-27"]},
                      "AMAT": {}, "QCOM": {}, "MDT": {}, "SOXL": {}})
        text = format_horizon(build_forward_horizon(
            HOLDINGS, LOOKTHROUGH, as_of=TODAY, events_by_symbol=ev))
        assert "NVDA" in text and "20.05%" in text
        assert "月別の通過比率" in text

    def test_unavailable_section_says_not_no_events(self):
        ev = {s: {"available": False, "error": "timeout"}
              for s in ("NVDA", "AMAT", "QCOM", "MDT", "SOXL")}
        text = format_horizon(build_forward_horizon(
            HOLDINGS, LOOKTHROUGH, as_of=TODAY, events_by_symbol=ev))
        assert "『予定なし』ではない" in text

    def test_unavailable_horizon_is_reported(self):
        text = format_horizon(None)
        assert "取得できませんでした" in text

    def test_empty_targets_do_not_crash(self):
        r = build_forward_horizon([], None, as_of=TODAY)
        assert r["available"] is False
