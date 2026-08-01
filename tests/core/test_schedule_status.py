"""日程の「状態」— 空リストを取得失敗と誤読させないための区別。

2026-08-01 の週次レポートは 7箇所以上で「日程が取得できなかった」と書いたが、
実際には大半が取得済みで単に翌週の予定が無かっただけだった。原因は各銘柄に
渡していたのが日程の**リスト**だけで、空が「失敗」なのか「予定なし」なのかを
区別する情報がどこにも無かったこと。

守るべき性質:
- 取得成功 × 翌週予定なし を「取得できなかった」と言わない
- ETF/投信の「決算が無い」を「取得できなかった」と言わない
- 本当に取れなかったときだけ unavailable にする
"""

from __future__ import annotations

from datetime import date

from src.core.risk.forward_events import symbol_schedule_status

AS_OF = date(2026, 8, 1)  # 土曜。翌週は 8/3(月)-8/7(金)


def _h(*rows):
    return [{"symbol": s, "name": n, "weight_pct": w} for s, n, w in rows]


def _ev(available=True, earnings=None, ex=None, error=None, source="yfinance"):
    return {"available": available, "earnings_dates": earnings or [],
            "ex_dividend_date": ex, "error": error, "source": source}


def test_next_week_earnings_is_scheduled():
    r = symbol_schedule_status(_h(("2802.T", "味の素", 10.9)), as_of=AS_OF,
                               events_by_symbol={"2802.T": _ev(earnings=["2026-08-06"])})
    row = r["2802.T"]
    assert row["status"] == "scheduled"
    assert row["in_next_week"] is True


def test_fetched_but_nothing_next_week_is_not_a_failure():
    """これが本丸。QCOM は取得できていたのに「取得できなかった」と書かれた。"""
    r = symbol_schedule_status(_h(("QCOM", "Qualcomm", 10.0)), as_of=AS_OF,
                               events_by_symbol={"QCOM": _ev(earnings=["2026-10-30"])})
    row = r["QCOM"]
    assert row["status"] == "none_upcoming"
    assert row["next_earnings"] == "2026-10-30"
    assert row["days_until"] == 90
    assert "取得できません" not in row["label"]
    assert "次回決算" in row["label"]


def test_fetched_without_any_future_date_is_still_not_a_failure():
    r = symbol_schedule_status(_h(("2737.T", "トーメンデバイス", 2.7)), as_of=AS_OF,
                               events_by_symbol={"2737.T": _ev(earnings=[])})
    row = r["2737.T"]
    assert row["status"] == "none_upcoming"
    assert "未公表" in row["label"]


def test_genuine_fetch_failure_is_reported_as_such():
    r = symbol_schedule_status(_h(("9999.T", "謎", 1.0)), as_of=AS_OF,
                               events_by_symbol={"9999.T": _ev(available=False,
                                                               error="HTTP 404")})
    row = r["9999.T"]
    assert row["status"] == "unavailable"
    assert "取得できませんでした" in row["label"]
    assert "予定なし" in row["label"]  # 「予定なしではない」と明記している


def test_etf_has_no_earnings_rather_than_missing_schedule():
    """ETF に決算は「無い」。取得失敗ではない。"""
    r = symbol_schedule_status(_h(("SOXL", "SOXL", 30.0)), as_of=AS_OF,
                               events_by_symbol={"SOXL": _ev(available=False)})
    row = r["SOXL"]
    assert row["status"] == "no_earnings"
    assert row["is_fund"] is True
    assert "取得できません" not in row["label"]


def test_etf_surfaces_component_earnings_with_effective_pct():
    lte = {"events": [{"symbol": "AMD", "day_label": "水 8/5",
                       "effective_pct": 10.6, "via": ["SOXL"]}]}
    r = symbol_schedule_status(_h(("SOXL", "SOXL", 30.0)), as_of=AS_OF,
                               events_by_symbol={"SOXL": _ev(available=False)},
                               lookthrough_events=lte)
    row = r["SOXL"]
    assert len(row["component_events"]) == 1
    assert "10.6%" in row["label"]


def test_jp_ex_dividend_uses_record_day_and_counts_as_scheduled():
    # 8/10(月)が配当落ち → 権利付最終日は 8/7(金) で翌週に入る
    r = symbol_schedule_status(_h(("2802.T", "味の素", 10.9)), as_of=AS_OF,
                               events_by_symbol={"2802.T": _ev(ex="2026-08-10")})
    assert r["2802.T"]["status"] == "scheduled"


def test_multiple_accounts_do_not_duplicate_symbol():
    r = symbol_schedule_status(
        _h(("2802.T", "味の素", 10.0), ("2802.T", "味の素", 1.0)), as_of=AS_OF,
        events_by_symbol={"2802.T": _ev(earnings=["2026-08-06"])})
    assert len(r) == 1
    assert r["2802.T"]["weight_pct"] == 11.0


def test_forward_schedule_no_longer_depends_on_moomoo():
    """moomoo 無効でも yfinance 由来の日程が時系列に載る。"""
    from src.core.research.briefing_pack import _forward_schedule

    forward = {"calendar": {"events": [{"kind": "earnings", "symbol": "2802.T",
                                        "date": "2026-08-06"}], "folded": []},
               "lookthrough_events": {"events": [{"symbol": "AMD",
                                                  "date": "2026-08-05",
                                                  "effective_pct": 10.6}]}}
    out = _forward_schedule({}, forward)
    kinds = {e["kind"] for e in out}
    assert "earnings" in kinds
    assert "component_earnings" in kinds
