"""Tests for src/core/research/moomoo_insights.py.

実 OpenD を使わず、SDK が返す**実測済みのタプル形状**を模したフェイクで検証する。
（ページング系は (ret, df, next_key, total)、配当/プレは (ret, (count, df))）
"""

import sys
from contextlib import contextmanager
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.core.research import moomoo_insights as mi
from src.data import moomoo_client


class FakeSDK:
    RET_OK = 0

    class Market:
        US = "US"


def _df(rows):
    return pd.DataFrame(rows)


class FakeCtx:
    """実測した戻り値形状を再現する OpenQuoteContext スタブ。"""

    def get_fed_watch_target_rate(self):
        return (0, _df([
            {"meeting_date": "2026-09-01", "target_range": "3.25-3.50%", "probability": 40.0},
            {"meeting_date": "2026-07-29", "target_range": "3.50-3.75%", "probability": 66.3},
            {"meeting_date": "2026-07-29", "target_range": "3.75-4.00%", "probability": 33.7},
        ]))

    def get_fed_watch_dot_plot(self):
        return (0, _df([{"year": 2026, "median_rate": 3.875, "current_rate": 3.63}]))

    def get_economic_calendar(self, begin, end_date=None):
        return (0, _df([
            {"title": "CPI", "timestamp": "1", "country": "United States",
             "star": "HIGH", "previous": "3%", "consensus": "3.1%", "actual": ""},
            {"title": "Minor", "timestamp": "2", "country": "United States",
             "star": "LOW", "previous": "", "consensus": "", "actual": ""},
        ]), "next", True)

    def get_earnings_calendar(self, market, begin_date=None, end_date=None):
        return (0, _df([
            {"security": "US.QCOM", "name": "Qualcomm", "earnings_date": "2026-07-29",
             "period_text": "2026Q3", "eps_predict": "2.5"},
            {"security": "US.OTHER", "name": "Other", "earnings_date": "2026-07-30",
             "period_text": "Q2", "eps_predict": "1.0"},
        ]))

    def get_dividend_calendar(self, market, date):
        # 入れ子: (ret, (count, df))
        return (0, (2, _df([
            {"security": "US.MDT", "name": "Medtronic", "statement": "Cash 0.7 USD",
             "record_date": "2026-08-01", "ex_date": "2026-07-30",
             "dividend_payable_date": "2026-08-15"},
        ])))

    def get_rating_change(self, market, count=None):
        return (0, _df([
            {"security": "US.QCOM", "name": "Qualcomm", "rating": "BUY",
             "last_rating": "HOLD", "target_price": "250.0", "change_type": "UPGRADE",
             "institution_name": "Baird", "recommendation_date": "2026-07-24"},
            {"security": "US.ZZZ", "name": "Z", "rating": "SELL", "last_rating": "HOLD",
             "target_price": "10", "change_type": "DOWNGRADE", "institution_name": "X",
             "recommendation_date": "2026-07-24"},
        ]), "k", 6345)

    def get_heat_map_data(self, market=None, count=None):
        return (0, _df([
            {"plate_name": "Semis", "change_rate": "3.2", "leader_stock": "US.NVDA"},
            {"plate_name": "Banks", "change_rate": "-1.0", "leader_stock": "US.JPM"},
        ]), "k", 145)

    def get_ark_fund_holding(self, count=None):
        return (0, _df([
            {"security": "US.TSLA", "name": "Tesla", "weight": "9.5", "weight_change": "0.2"},
        ]), "k", 3)

    def get_rise_fall_distribution(self, market=None):
        return (0, {"plate": "US", "range_list": [
            {"left_border": 1, "right_border": 3, "stock_count": 100},
            {"left_border": -3, "right_border": -1, "stock_count": 60},
        ]})

    def _rank(self, field):
        return (0, (1, _df([
            {"security": "US.AAA", "name": "AaaCorp", field: "12.5"},
        ])))

    def get_us_pre_market_rank(self):
        return self._rank("pre_market_change_ratio")

    def get_us_after_hours_rank(self):
        return self._rank("after_market_change_ratio")

    def get_us_overnight_rank(self):
        return self._rank("overnight_change_ratio")

    def get_search_news(self, query):
        return (0, _df([
            {"title": "Chip news", "source": "Benzinga", "publish_time": "7/22",
             "url": "https://x"},
        ]))

    def get_capital_flow(self, symbol):
        return (0, _df([
            {"main_in_flow": "N/A", "capital_flow_item_time": "t0"},
            {"main_in_flow": "150000000.0", "capital_flow_item_time": "t1"},
        ]))

    def get_valuation_detail(self, symbol):
        return (0, {"valuation_type": 3, "trend": {}})

    def get_research_analyst_consensus(self, symbol):
        if symbol == "US.SOXL":  # ETF はエラー
            return (-1, "Only stocks and REITs are supported")
        return (0, {"highest": "300.0", "average": "218.18", "lowest": "100.0",
                    "rating": "3", "total": "30", "buy": "33.333", "hold": "56.667",
                    "sell": "10.0", "update_time_str": "2026-07-23"})

    def get_research_morningstar_report(self, symbol):
        if symbol == "US.SOXL":
            return (-1, "Only stocks and REITs are supported")
        return (0, {"star_rating": "3", "fair_value": "200.0",
                    "economic_moat_label": "Narrow", "uncertainty_label": "High",
                    "star_update_time_str": "2026-07-23"})

    def get_insider_trade_list(self, symbol):
        if symbol == "US.SOXL":
            return (0, _df([]))  # ETF は空
        return (0, _df([
            {"name": "CFO", "title": "Chief Financial Officer", "trade_shares": "-2500",
             "max_trade_date_str": "2026-07-13", "transaction_type": "Automatic Disposition"},
        ]))

    def close(self):
        pass


# ---------------------------------------------------------------------------
# 低レベルヘルパ
# ---------------------------------------------------------------------------


def test_unwrap_two_tuple():
    df = _df([{"a": 1}])
    ret, data = mi._unwrap((0, df))
    assert ret == 0 and data is df


def test_unwrap_four_tuple_takes_index1():
    df = _df([{"a": 1}])
    ret, data = mi._unwrap((0, df, "next", 6345))
    assert data is df


def test_unwrap_nested_count_df():
    df = _df([{"a": 1}])
    ret, data = mi._unwrap((0, (123, df)))
    assert data is df


def test_unwrap_non_tuple():
    assert mi._unwrap("boom") == (None, None)


@pytest.mark.parametrize("raw,expected", [
    ("178.268", 178.268), ("N/A", None), ("", None), (None, None), ("-2500", -2500.0),
])
def test_float_coerce(raw, expected):
    assert mi._f(raw) == expected


# ---------------------------------------------------------------------------
# 個別ゲッター
# ---------------------------------------------------------------------------


def test_fed_watch_picks_nearest_meeting_top_prob():
    fw = mi.fed_watch(FakeCtx(), FakeSDK)
    assert fw["next_meeting"] == "2026-07-29"
    assert fw["top_range"] == "3.50-3.75%"
    assert fw["top_prob"] == 66.3
    assert fw["dot_plot"]["median_rate"] == 3.875


def test_economic_events_high_first():
    ev = mi.economic_events(FakeCtx(), FakeSDK)
    assert ev[0]["title"] == "CPI"  # HIGH が先頭


def test_earnings_filters_to_holdings():
    e = mi.earnings_for(FakeCtx(), FakeSDK, ["US.QCOM"], days=7)
    assert len(e) == 1 and e[0]["security"] == "US.QCOM"


def test_dividends_nested_shape_parsed():
    d = mi.dividend_for(FakeCtx(), FakeSDK, ["US.MDT"])
    assert d and d[0]["ex_date"] == "2026-07-30"


def test_rating_changes_filtered():
    rc = mi.rating_changes_for(FakeCtx(), FakeSDK, ["US.QCOM"])
    assert len(rc) == 1 and rc[0]["change_type"] == "UPGRADE"
    assert rc[0]["target_price"] == 250.0


def test_capital_flow_skips_na_takes_latest_valid():
    cf = mi.capital_flow(FakeCtx(), FakeSDK, "US.QCOM")
    assert cf["main_in_flow"] == 150000000.0 and cf["time"] == "t1"


def test_analyst_and_morningstar_dicts():
    an = mi.analyst_consensus(FakeCtx(), FakeSDK, "US.QCOM")
    assert an["average"] == 218.18 and an["buy"] == 33.333
    ms = mi.morningstar(FakeCtx(), FakeSDK, "US.QCOM")
    assert ms["fair_value"] == 200.0 and ms["moat"] == "Narrow"


def test_etf_analyst_returns_none():
    assert mi.analyst_consensus(FakeCtx(), FakeSDK, "US.SOXL") is None


def test_rise_fall_counts():
    rf = mi.rise_fall(FakeCtx(), FakeSDK)
    assert rf["up"] == 100 and rf["down"] == 60


def test_prepost_movers_sorted():
    m = mi.prepost_movers(FakeCtx(), FakeSDK, "pre")
    assert m and m[0]["change_ratio"] == 12.5


# ---------------------------------------------------------------------------
# オーケストレーション + 整形
# ---------------------------------------------------------------------------


def test_to_us_symbols_filters_jp():
    us = mi._to_us_symbols(["QCOM", "MDT", "7203.T", "SOXL"])
    assert "US.QCOM" in us and "US.SOXL" in us
    assert all(not s.startswith("JP") for s in us)


@pytest.fixture
def _fake_pipeline(monkeypatch):
    @contextmanager
    def fake_ctx():
        yield FakeCtx()
    monkeypatch.setattr(mi, "_quote_ctx", fake_ctx)
    monkeypatch.setattr(moomoo_client, "_import_sdk", lambda: FakeSDK)


def test_collect_weekly_insights(_fake_pipeline):
    data = mi.collect_weekly_insights(["QCOM", "MDT", "SOXL", "7203.T"])
    assert data["fed_watch"]["top_prob"] == 66.3
    # SOXL は analyst/morningstar が None だが capital_flow はあるので per_stock に載る
    assert "US.QCOM" in data["per_stock"]
    assert data["per_stock"]["US.QCOM"]["analyst"]["average"] == 218.18


def test_collect_returns_empty_when_ctx_none(monkeypatch):
    @contextmanager
    def none_ctx():
        yield None
    monkeypatch.setattr(mi, "_quote_ctx", none_ctx)
    assert mi.collect_weekly_insights(["QCOM"]) == {}


def test_format_empty_is_empty_string():
    assert mi.format_weekly_section({}) == ""


def test_format_section_contains_key_markers(_fake_pipeline):
    data = mi.collect_weekly_insights(["QCOM", "MDT"])
    md = mi.format_weekly_section(data)
    assert "FedWatch" in md
    assert "3.50-3.75%" in md
    assert "US.QCOM" in md
    assert "moomoo インサイト" in md


def test_format_no_float_noise(_fake_pipeline):
    data = mi.collect_weekly_insights(["QCOM"])
    md = mi.format_weekly_section(data)
    assert "56.667000" not in md  # 端数ノイズが出ていない
