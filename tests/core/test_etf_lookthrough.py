"""ETFルックスルーのテスト。

2026-08-01 の週次レポートが自ら指摘した穴を埋める機能:

> SOXL・TECL・TQQQ の3銘柄について、翌週の日程が取得できなかった。
> PFの過半（56.9%）については翌週何が起きるかを把握していない状態でこの週に入る。

守るべき性質:
- レバレッジETFは 1x proxy 経由で中身を見る（スワップ複製で保有が実体を反映しない）
- 実質% = PF比率 × 内部ウェイト × レバレッジ
- ETF は「決算が無い」。「取得失敗」と混同しない
- 構成が取れないことを「中身が無い」と扱わない
"""

from __future__ import annotations

from datetime import date

import pytest

from src.core.risk import etf_lookthrough as lt


@pytest.fixture(autouse=True)
def _clean():
    lt.reset_config_cache()
    yield
    lt.reset_config_cache()


def _cfg(**over):
    base = {
        "proxies": {
            "SOXL": {"proxy": "SOXX", "underlying": "半導体指数", "leverage": 3},
            "TQQQ": {"proxy": "QQQ", "underlying": "ナスダック100", "leverage": 3},
        },
        "fund_proxies": {
            "iFreeNEXT FANG+": {"proxy": "QQQ", "note": "近似"},
        },
        "settings": {"top_n": 10, "min_effective_pct": 0.5, "cache_hours": 168},
    }
    base.update(over)
    return base


def _holdings(*rows):
    return [{"symbol": s, "name": n, "weight_pct": w, "leverage": lev}
            for s, n, w, lev in rows]


# ---------------------------------------------------------------------------
# proxy 解決
# ---------------------------------------------------------------------------


def test_leveraged_etf_resolves_to_one_x_proxy():
    """3x ETF はスワップ複製で保有が実体を反映しない。実測で TQQQ は IQMM 単独。"""
    r = lt.resolve_proxy("SOXL", cfg=_cfg())
    assert r["lookup"] == "SOXX"
    assert r["leverage"] == 3.0
    assert r["kind"] == "leveraged_etf"
    assert r["approximate"] is False


def test_fund_without_ticker_resolves_by_name_and_is_marked_approximate():
    r = lt.resolve_proxy(None, "iFreeNEXT FANG+インデックス", cfg=_cfg())
    assert r["lookup"] == "QQQ"
    assert r["approximate"] is True, "近似であることを必ず明示する"


def test_unmapped_fund_is_reported_not_guessed():
    r = lt.resolve_proxy(None, "謎の投信", cfg=_cfg())
    assert r["lookup"] is None
    assert r["kind"] == "unmapped_fund"


def test_plain_stock_resolves_to_itself():
    r = lt.resolve_proxy("AAPL", cfg=_cfg())
    assert r["lookup"] == "AAPL"
    assert r["kind"] == "direct"
    assert r["leverage"] == 1.0


# ---------------------------------------------------------------------------
# 実質エクスポージャー
# ---------------------------------------------------------------------------


def _fake_holdings(mapping):
    def _fetch(ticker, **kw):
        rows = mapping.get(str(ticker).upper())
        if rows is None:
            return {"ticker": ticker, "available": False, "holdings": [],
                    "error": "構成銘柄が取得できませんでした"}
        return {"ticker": ticker, "available": True,
                "holdings": [{"symbol": s, "weight_pct": w} for s, w in rows],
                "coverage_pct": sum(w for _, w in rows)}
    return _fetch


def test_leverage_multiplies_effective_exposure(monkeypatch):
    """3xを1xとして扱うと実質エクスポージャーを3分の1に見誤る。"""
    monkeypatch.setattr(lt, "fetch_holdings",
                        _fake_holdings({"SOXX": [("NVDA", 20.0)]}))
    r = lt.build_lookthrough(_holdings(("SOXL", "SOXL", 10.0, 3)), cfg=_cfg())
    nvda = next(x for x in r["effective"] if x["symbol"] == "NVDA")
    assert nvda["effective_pct"] == pytest.approx(10.0 * 0.20 * 3)


def test_same_stock_across_multiple_etfs_is_summed(monkeypatch):
    """SOXL と TQQQ の両方に NVDA がいる。合算しないと過小評価する。"""
    monkeypatch.setattr(lt, "fetch_holdings", _fake_holdings({
        "SOXX": [("NVDA", 20.0)], "QQQ": [("NVDA", 10.0)]}))
    r = lt.build_lookthrough(
        _holdings(("SOXL", "SOXL", 10.0, 3), ("TQQQ", "TQQQ", 10.0, 3)),
        cfg=_cfg())
    nvda = next(x for x in r["effective"] if x["symbol"] == "NVDA")
    assert nvda["effective_pct"] == pytest.approx(6.0 + 3.0)
    assert set(nvda["sources"]) == {"SOXL", "TQQQ"}


def test_direct_holding_is_added_to_etf_exposure(monkeypatch):
    monkeypatch.setattr(lt, "fetch_holdings",
                        _fake_holdings({"SOXX": [("NVDA", 20.0)]}))
    r = lt.build_lookthrough(
        _holdings(("SOXL", "SOXL", 10.0, 3), ("NVDA", "NVIDIA", 5.0, None)),
        cfg=_cfg())
    nvda = next(x for x in r["effective"] if x["symbol"] == "NVDA")
    assert nvda["direct_pct"] == pytest.approx(5.0)
    assert nvda["via_etf_pct"] == pytest.approx(6.0)
    assert nvda["effective_pct"] == pytest.approx(11.0)


def test_small_components_are_folded_not_dropped(monkeypatch):
    monkeypatch.setattr(lt, "fetch_holdings", _fake_holdings({
        "SOXX": [("NVDA", 20.0), ("TINY", 0.5)]}))
    r = lt.build_lookthrough(_holdings(("SOXL", "SOXL", 10.0, 3)), cfg=_cfg())
    assert [x["symbol"] for x in r["effective"]] == ["NVDA"]
    assert [x["symbol"] for x in r["folded"]] == ["TINY"]


def test_unresolvable_etf_is_reported_not_treated_as_empty(monkeypatch):
    """構成が取れないことを「中身が無い」と扱わない。"""
    monkeypatch.setattr(lt, "fetch_holdings", _fake_holdings({}))
    r = lt.build_lookthrough(_holdings(("SOXL", "SOXL", 10.0, 3)), cfg=_cfg())
    assert r["effective"] == []
    assert len(r["unresolved"]) == 1
    assert "中身が無いのではなく" in r["note"]


def test_approximate_fund_is_flagged_in_note(monkeypatch):
    monkeypatch.setattr(lt, "fetch_holdings",
                        _fake_holdings({"QQQ": [("NVDA", 10.0)]}))
    r = lt.build_lookthrough(
        [{"symbol": None, "name": "iFreeNEXT FANG+インデックス",
          "weight_pct": 7.0, "leverage": None}], cfg=_cfg())
    assert "近似" in r["note"]


def test_holdings_without_weight_are_skipped(monkeypatch):
    monkeypatch.setattr(lt, "fetch_holdings",
                        _fake_holdings({"SOXX": [("NVDA", 20.0)]}))
    r = lt.build_lookthrough(
        [{"symbol": "SOXL", "name": "SOXL", "weight_pct": None}], cfg=_cfg())
    assert r["effective"] == []


# ---------------------------------------------------------------------------
# 構成取得
# ---------------------------------------------------------------------------


def test_fetch_holdings_rejects_empty_ticker():
    r = lt.fetch_holdings("", use_cache=False)
    assert r["available"] is False


def test_fetch_holdings_reports_error_not_empty(monkeypatch):
    class _Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("network")

    import yfinance as yf

    monkeypatch.setattr(yf, "Ticker", _Boom)
    r = lt.fetch_holdings("SOXX", use_cache=False)
    assert r["available"] is False
    assert "RuntimeError" in r["error"]


# ---------------------------------------------------------------------------
# 前方イベントとの合流
# ---------------------------------------------------------------------------


def _lookthrough(rows, min_pct=0.5):
    return {"available": True, "min_effective_pct": min_pct,
            "effective": [{"symbol": s, "effective_pct": p,
                           "via_etf_pct": p, "direct_pct": 0.0,
                           "sources": ["SOXL"]} for s, p in rows],
            "folded": [], "resolved_etfs": [], "unresolved": []}


def test_lookthrough_events_surface_component_earnings():
    """ETF に決算は無いが、中身の企業の決算は倍率で効く。"""
    events = {"AMD": {"symbol": "AMD", "available": True, "source": "test",
                      "earnings_dates": ["2026-08-05"], "ex_dividend_date": None}}
    r = lt.lookthrough_events(_lookthrough([("AMD", 10.6)]),
                              as_of=date(2026, 8, 1),
                              events_by_symbol=events)
    assert r["available"] is True
    assert r["events"][0]["symbol"] == "AMD"
    assert r["total_effective_pct"] == pytest.approx(10.6)
    assert "レバレッジ分を含めた" in r["message"]


def test_lookthrough_events_ignore_dates_outside_next_week():
    events = {"AMD": {"symbol": "AMD", "available": True,
                      "earnings_dates": ["2026-09-01"], "ex_dividend_date": None}}
    r = lt.lookthrough_events(_lookthrough([("AMD", 10.6)]),
                              as_of=date(2026, 8, 1), events_by_symbol=events)
    assert r["events"] == []
    assert "ありません" in r["message"]


def test_lookthrough_events_report_unavailable_symbols():
    events = {"AMD": {"symbol": "AMD", "available": False, "earnings_dates": []}}
    r = lt.lookthrough_events(_lookthrough([("AMD", 10.6)]),
                              as_of=date(2026, 8, 1), events_by_symbol=events)
    assert r["unavailable_symbols"] == ["AMD"]


def test_lookthrough_events_unavailable_without_expansion():
    r = lt.lookthrough_events({"available": True, "effective": []},
                              as_of=date(2026, 8, 1))
    assert r["available"] is False


def test_lookthrough_events_carry_anti_trading_caveat():
    events = {"AMD": {"symbol": "AMD", "available": True,
                      "earnings_dates": ["2026-08-05"], "ex_dividend_date": None}}
    r = lt.lookthrough_events(_lookthrough([("AMD", 10.6)]),
                              as_of=date(2026, 8, 1), events_by_symbol=events)
    assert "個別に売買する対象のリストではなく" in r["caveat"]


# ---------------------------------------------------------------------------
# 出力
# ---------------------------------------------------------------------------


def test_formatter_distinguishes_etf_from_fetch_failure():
    """ETF は「決算が無い」。「取得できなかった」と混同しない。"""
    from src.output.forward_formatter import format_calendar

    cal = {"range": {"start": "2026-08-03", "end": "2026-08-07"},
           "events": [], "folded": [],
           "unavailable_symbols": ["SOXL", "9999.T"], "note": "テスト"}
    text = format_calendar(cal, resolved_via_lookthrough=["SOXL"])
    assert "SOXL は ETF/投信のため決算がありません" in text
    assert "日程を取得できなかった銘柄: 9999.T" in text


def test_formatter_renders_lookthrough_section():
    from src.output.forward_formatter import format_lookthrough_events

    bundle = {
        "lookthrough": {**_lookthrough([("NVDA", 16.1)]),
                        "note": "1件を展開しました。"},
        "lookthrough_events": {
            "available": True,
            "events": [{"symbol": "AMD", "day_label": "水 8/5",
                        "effective_pct": 10.6}],
            "message": "実質 10.6% が決算を通過します。",
            "caveat": "曝露を知るためのものです。"},
    }
    text = format_lookthrough_events(bundle)
    assert "ETFルックスルー" in text
    assert "NVDA" in text
    assert "水 8/5" in text


def test_formatter_silent_without_lookthrough():
    from src.output.forward_formatter import format_lookthrough_events

    assert format_lookthrough_events({"lookthrough": {"available": False}}) == ""
