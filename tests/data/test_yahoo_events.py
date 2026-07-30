"""決算日・配当日取得のテスト (土曜設計書 提案4)。

守るべき性質: **取得失敗を「予定なし」と混同しない。**
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from src.data.yahoo_client import events as ev


@pytest.fixture(autouse=True)
def _clear():
    ev.clear_event_cache()
    yield
    ev.clear_event_cache()


class _FakeTicker:
    def __init__(self, calendar=None, earnings_index=None, raise_on=()):
        self._calendar = calendar
        self._earnings_index = earnings_index
        self._raise_on = raise_on

    @property
    def calendar(self):
        if "calendar" in self._raise_on:
            raise RuntimeError("boom")
        return self._calendar

    def get_earnings_dates(self, limit=8):
        if "earnings" in self._raise_on:
            raise RuntimeError("boom")
        if self._earnings_index is None:
            return None

        class _DF:
            index = self._earnings_index

        return _DF()


def _patch(monkeypatch, ticker):
    monkeypatch.setattr(ev.yf, "Ticker", lambda symbol: ticker)


# ---------------------------------------------------------------------------
# 日付の正規化
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    ("2026-08-06", "2026-08-06"),
    ("2026-08-06 15:30:00", "2026-08-06"),
    (date(2026, 8, 6), "2026-08-06"),
    (datetime(2026, 8, 6, 15, 30), "2026-08-06"),
    ("not a date", None),
    (None, None),
    (1, None),
])
def test_as_date(value, expected):
    assert ev._as_date(value) == expected


def test_as_date_handles_epoch_seconds():
    epoch = int(datetime(2026, 8, 6, tzinfo=timezone.utc).timestamp())
    assert ev._as_date(epoch) == "2026-08-06"


# ---------------------------------------------------------------------------
# calendar 経由
# ---------------------------------------------------------------------------


def test_reads_dict_calendar(monkeypatch):
    _patch(monkeypatch, _FakeTicker(calendar={
        "Earnings Date": [date(2026, 8, 6)],
        "Ex-Dividend Date": date(2026, 9, 29),
        "Dividend Date": date(2026, 12, 1),
    }))
    r = ev.get_symbol_events("2802.T", use_cache=False)
    assert r["available"] is True
    assert r["earnings_dates"] == ["2026-08-06"]
    assert r["ex_dividend_date"] == "2026-09-29"
    assert r["dividend_date"] == "2026-12-01"
    assert r["source"] == "yfinance"
    assert r["fetched_at"]


def test_deduplicates_and_sorts_earnings_dates(monkeypatch):
    _patch(monkeypatch, _FakeTicker(calendar={
        "Earnings Date": [date(2026, 8, 6), date(2026, 8, 6), date(2026, 8, 1)]}))
    r = ev.get_symbol_events("X", use_cache=False)
    assert r["earnings_dates"] == ["2026-08-01", "2026-08-06"]


def test_calendar_exception_falls_back_to_earnings_api(monkeypatch):
    future = date.today() + timedelta(days=10)
    _patch(monkeypatch, _FakeTicker(raise_on=("calendar",),
                                    earnings_index=[future]))
    r = ev.get_symbol_events("X", use_cache=False)
    assert r["available"] is True
    assert r["earnings_dates"] == [future.isoformat()]


def test_past_earnings_dates_are_dropped(monkeypatch):
    past = date.today() - timedelta(days=30)
    _patch(monkeypatch, _FakeTicker(calendar=None, earnings_index=[past]))
    r = ev.get_symbol_events("X", use_cache=False)
    assert r["available"] is False
    assert r["earnings_dates"] == []


# ---------------------------------------------------------------------------
# 縮退
# ---------------------------------------------------------------------------


def test_unavailable_is_distinguished_from_no_events(monkeypatch):
    """ETF等は決算が無い。それを『取得成功で0件』にしてはいけない。"""
    _patch(monkeypatch, _FakeTicker(calendar=None, earnings_index=None))
    r = ev.get_symbol_events("SOXL", use_cache=False)
    assert r["available"] is False
    assert r["error"] is not None


def test_ticker_construction_failure_is_handled(monkeypatch):
    def boom(symbol):
        raise RuntimeError("network")

    monkeypatch.setattr(ev.yf, "Ticker", boom)
    r = ev.get_symbol_events("X", use_cache=False)
    assert r["available"] is False
    assert "RuntimeError" in r["error"]


def test_empty_symbol_is_rejected():
    r = ev.get_symbol_events("  ")
    assert r["available"] is False


def test_cache_prevents_repeat_calls(monkeypatch):
    calls = {"n": 0}

    def counting(symbol):
        calls["n"] += 1
        return _FakeTicker(calendar={"Earnings Date": [date(2026, 8, 6)]})

    monkeypatch.setattr(ev.yf, "Ticker", counting)
    ev.get_symbol_events("X")
    ev.get_symbol_events("X")
    assert calls["n"] == 1


def test_get_events_for_isolates_failures(monkeypatch):
    def flaky(symbol):
        if symbol == "BAD":
            raise RuntimeError("boom")
        return _FakeTicker(calendar={"Earnings Date": [date(2026, 8, 6)]})

    monkeypatch.setattr(ev.yf, "Ticker", flaky)
    out = ev.get_events_for(["GOOD", "BAD"], use_cache=False)
    assert out["GOOD"]["available"] is True
    assert out["BAD"]["available"] is False


def test_exported_from_package():
    from src.data import yahoo_client as yc

    assert hasattr(yc, "get_symbol_events")
    assert hasattr(yc, "get_events_for")
    assert hasattr(yc, "clear_event_cache")
