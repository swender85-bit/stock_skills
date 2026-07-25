"""briefing_pack.py のテスト（ネットワーク非依存・モック）。"""

import json
from pathlib import Path

import pytest

from src.core.research import briefing_pack as bp


# --- week_over_week_delta ---------------------------------------------------


def test_wow_delta_computes_diffs():
    prior_index = {
        "QCOM": [
            {"date": "2026-07-01", "per": 20.0, "price": 150.0, "verdict": "適正"},
            {"date": "2026-06-01", "per": 22.0, "price": 140.0},
        ]
    }
    current = {"per": 18.0, "price": 170.0, "verdict": "やや割高"}
    d = bp.week_over_week_delta("QCOM", current, prior_index, today="2026-07-25")
    assert d["prior_date"] == "2026-07-01"  # today より前で最新
    assert d["fields"]["per"]["prior"] == 20.0
    assert d["fields"]["per"]["now"] == 18.0
    assert d["fields"]["per"]["diff"] == -2.0
    assert d["now_verdict"] == "やや割高"
    assert d["prior_verdict"] == "適正"


def test_wow_delta_none_when_no_prior():
    d = bp.week_over_week_delta("XXX", {"per": 1.0}, {}, today="2026-07-25")
    assert d is None


def test_wow_delta_ignores_future_snapshots():
    prior_index = {"AAA": [{"date": "2026-08-01", "per": 5.0}]}  # 未来
    d = bp.week_over_week_delta("AAA", {"per": 6.0}, prior_index, today="2026-07-25")
    assert d is None


# --- _prior_report_index (temp history dir) --------------------------------


def test_prior_report_index_indexes_by_symbol(monkeypatch):
    fake_snaps = [
        {"symbol": "QCOM", "date": "2026-07-20", "per": 18.0},
        {"symbol": "qcom", "date": "2026-07-10", "per": 19.0},  # 大小混在
        {"symbol": "SOXL", "date": "2026-07-20", "price": 130.0},
    ]
    import src.data.history.load as hload

    monkeypatch.setattr(hload, "load_history",
                        lambda category, days_back=None: fake_snaps)
    idx = bp._prior_report_index()
    assert set(idx) == {"QCOM", "SOXL"}
    assert len(idx["QCOM"]) == 2  # 大小同一キーに集約


# --- _forward_schedule ------------------------------------------------------


def test_forward_schedule_merges_calendars():
    moomoo = {
        "economic_events": [{"title": "CPI", "country": "US", "star": "HIGH"}],
        "earnings": [{"security": "QCOM", "date": "2026-07-30", "period": "Q3"}],
        "dividends": [{"security": "MDT", "ex_date": "2026-08-01"}],
        "fed_watch": {"next_meeting": "2026-09-17", "top_range": "425-450", "top_prob": 70.0},
    }
    sched = bp._forward_schedule(moomoo)
    kinds = {e["kind"] for e in sched}
    assert kinds == {"economic", "earnings", "dividend", "fomc"}


def test_forward_schedule_empty_on_empty_moomoo():
    assert bp._forward_schedule({}) == []


# --- _compact_technicals ----------------------------------------------------


def test_compact_technicals_subset():
    t = {
        "last": 100.0, "sma200": 90.0, "rsi14": 65.0,
        "bollinger": {"percent_b": 0.8},
        "range_52w": {"position": 0.7, "from_high_pct": -5.0},
        "heat": {"state": "overbought", "label": "買われすぎ", "signals": ["RSI>70"]},
    }
    c = bp._compact_technicals(t)
    assert c["rsi14"] == 65.0
    assert c["percent_b"] == 0.8
    assert c["heat_state"] == "overbought"


def test_compact_technicals_none():
    assert bp._compact_technicals(None) is None


# --- build_symbol_briefing (fully mocked) -----------------------------------


def test_build_symbol_briefing_graceful(monkeypatch):
    import src.data.yahoo_client as yc

    monkeypatch.setattr(yc, "get_stock_info",
                        lambda s: {"name": "Toyota", "price": 2800.0, "per": 10.0})
    monkeypatch.setattr(yc, "get_stock_detail", lambda s: None)
    monkeypatch.setattr(yc, "get_price_history", lambda s, period="2y": None)
    # 外部依存は全て空に
    monkeypatch.setattr(bp, "_prior_report_index", lambda days_back=180: {})
    monkeypatch.setattr(bp, "_safe_competitors", lambda syms: {})
    monkeypatch.setattr(bp, "_safe_news_watch", lambda syms: {})
    monkeypatch.setattr(bp, "_safe_moomoo", lambda syms: {})
    monkeypatch.setattr(bp, "_safe_context", lambda q: "")

    pack = bp.build_symbol_briefing("7203.T")
    assert pack["mode"] == "symbol"
    assert pack["meta"]["symbol"] == "7203.T"
    assert pack["pack_version"] == bp.PACK_VERSION
    assert pack["info"]["name"] == "Toyota"
