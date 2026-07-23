"""Tests for src/core/research/portfolio_news.py (保有＋指数ニュース監視)."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.core.research import portfolio_news as pn


@pytest.fixture
def fake_finnhub(monkeypatch):
    """finnhub_client をスタブ化（ネットワークを叩かない）。"""
    from src.data import finnhub_client as fc

    monkeypatch.setattr(fc, "get_company_news", lambda *a, **k: [])
    monkeypatch.setattr(fc, "get_market_news", lambda *a, **k: [])
    return fc


@pytest.fixture
def fake_yahoo(monkeypatch):
    """yahoo_client のニュース/価格取得をスタブ化。"""
    from src.data import yahoo_client as yc

    monkeypatch.setattr(yc, "get_stock_news", lambda *a, **k: [], raising=False)
    monkeypatch.setattr(yc, "get_stock_info", lambda *a, **k: None)
    return yc


# ---------------------------------------------------------------------------
# ヘルパ
# ---------------------------------------------------------------------------

class TestIsJapanese:
    @pytest.mark.parametrize("symbol", ["7203.T", "2802.t", "1234.JP"])
    def test_japanese(self, symbol):
        assert pn._is_japanese(symbol) is True

    @pytest.mark.parametrize("symbol", ["AAPL", "SOXL", "D05.SI", "^N225"])
    def test_not_japanese(self, symbol):
        assert pn._is_japanese(symbol) is False


class TestDedup:
    def test_preserves_order_and_removes_dupes(self):
        assert pn._dedup(["A", "B", "A", "C", "B"]) == ["A", "B", "C"]

    def test_drops_empty(self):
        assert pn._dedup(["", "A", ""]) == ["A"]

    def test_empty_input(self):
        assert pn._dedup([]) == []


class TestFromYahooNews:
    def test_normalizes_to_common_schema(self):
        out = pn._from_yahoo_news(
            {"title": "  headline  ", "publisher": "Nikkei",
             "link": "https://x", "publish_time": 1700000000}
        )
        assert out == {
            "headline": "headline", "summary": "", "source": "Nikkei",
            "url": "https://x", "datetime": 1700000000,
        }

    def test_missing_fields_get_defaults(self):
        out = pn._from_yahoo_news({})
        assert out["headline"] == ""
        assert out["source"] == "yahoo"
        assert out["datetime"] == 0


class TestFmtPct:
    def test_positive_gets_plus_sign(self):
        assert pn._fmt_pct(1.234) == "+1.23%"

    def test_negative(self):
        assert pn._fmt_pct(-2.5) == "-2.50%"

    def test_none_is_dash(self):
        """算出できない値を 0.00% と表示すると「動いていない」と誤読される。"""
        assert pn._fmt_pct(None) == "—"

    def test_non_numeric_is_dash(self):
        assert pn._fmt_pct("n/a") == "—"


# ---------------------------------------------------------------------------
# gather_holding_news — ソース層構成
# ---------------------------------------------------------------------------

class TestGatherHoldingNews:
    def test_us_symbol_uses_finnhub(self, monkeypatch, fake_finnhub, fake_yahoo):
        monkeypatch.setattr(
            fake_finnhub, "get_company_news",
            lambda sym, **k: [{"headline": "fh news", "source": "Finnhub"}],
        )
        called = []
        monkeypatch.setattr(
            fake_yahoo, "get_stock_news",
            lambda *a, **k: called.append(1) or [], raising=False,
        )
        out = pn.gather_holding_news(["AAPL"])
        assert out["AAPL"][0]["headline"] == "fh news"
        assert called == []  # Finnhubで取れたら yahoo は呼ばない

    def test_japanese_symbol_skips_finnhub(self, monkeypatch, fake_finnhub, fake_yahoo):
        """Finnhubフリー枠は日本株ニュース非対応のため呼ばない。"""
        called = []
        monkeypatch.setattr(
            fake_finnhub, "get_company_news",
            lambda sym, **k: called.append(sym) or [{"headline": "should not appear"}],
        )
        monkeypatch.setattr(
            fake_yahoo, "get_stock_news",
            lambda *a, **k: [{"title": "yahoo jp", "publisher": "Nikkei"}],
            raising=False,
        )
        out = pn.gather_holding_news(["7203.T"])
        assert called == []
        assert out["7203.T"][0]["headline"] == "yahoo jp"

    def test_falls_back_to_yahoo_when_finnhub_empty(self, monkeypatch,
                                                    fake_finnhub, fake_yahoo):
        monkeypatch.setattr(
            fake_yahoo, "get_stock_news",
            lambda *a, **k: [{"title": "fallback", "publisher": "Y"}], raising=False,
        )
        out = pn.gather_holding_news(["AAPL"])
        assert out["AAPL"][0]["headline"] == "fallback"
        assert out["AAPL"][0]["source"] == "Y"

    def test_symbol_with_no_news_is_omitted(self, fake_finnhub, fake_yahoo):
        assert pn.gather_holding_news(["AAPL", "7203.T"]) == {}

    def test_yahoo_exception_is_swallowed(self, monkeypatch, fake_finnhub, fake_yahoo):
        def boom(*a, **k):
            raise RuntimeError("yahoo down")

        monkeypatch.setattr(fake_yahoo, "get_stock_news", boom, raising=False)
        assert pn.gather_holding_news(["AAPL"]) == {}

    def test_per_symbol_limit_applied_to_yahoo(self, monkeypatch,
                                               fake_finnhub, fake_yahoo):
        monkeypatch.setattr(
            fake_yahoo, "get_stock_news",
            lambda *a, **k: [{"title": f"t{i}"} for i in range(10)], raising=False,
        )
        out = pn.gather_holding_news(["AAPL"], per_symbol=2)
        assert len(out["AAPL"]) == 2

    def test_empty_symbol_list(self, fake_finnhub, fake_yahoo):
        assert pn.gather_holding_news([]) == {}


# ---------------------------------------------------------------------------
# gather_index_watch
# ---------------------------------------------------------------------------

class TestGatherIndexWatch:
    def test_returns_price_and_change(self, monkeypatch, fake_yahoo):
        monkeypatch.setattr(fake_yahoo, "get_stock_info", lambda sym: {"price": 5000.0})
        monkeypatch.setattr(pn, "_daily_change_pct", lambda sym, yc: 1.5)

        out = pn.gather_index_watch([{"label": "S&P500", "symbol": "^GSPC"}])
        assert out == [{"label": "S&P500", "symbol": "^GSPC",
                        "price": 5000.0, "percent_change": 1.5}]

    def test_accepts_current_price_alias(self, monkeypatch, fake_yahoo):
        monkeypatch.setattr(fake_yahoo, "get_stock_info",
                            lambda sym: {"current_price": 123.0})
        monkeypatch.setattr(pn, "_daily_change_pct", lambda sym, yc: None)
        out = pn.gather_index_watch([{"label": "X", "symbol": "X"}])
        assert out[0]["price"] == 123.0

    def test_skips_index_without_price(self, monkeypatch, fake_yahoo):
        monkeypatch.setattr(fake_yahoo, "get_stock_info", lambda sym: {"name": "no price"})
        assert pn.gather_index_watch([{"label": "X", "symbol": "X"}]) == []

    def test_skips_index_on_exception(self, monkeypatch, fake_yahoo):
        def boom(sym):
            raise RuntimeError("down")

        monkeypatch.setattr(fake_yahoo, "get_stock_info", boom)
        assert pn.gather_index_watch([{"label": "X", "symbol": "X"}]) == []

    def test_defaults_to_default_indices(self, monkeypatch, fake_yahoo):
        seen = []
        monkeypatch.setattr(fake_yahoo, "get_stock_info",
                            lambda sym: seen.append(sym) or None)
        pn.gather_index_watch()
        assert seen == [i["symbol"] for i in pn.DEFAULT_INDICES]


class TestDailyChangePct:
    def _yc_with_closes(self, closes):
        import pandas as pd

        yc = MagicMock()
        yc.get_price_history.return_value = pd.DataFrame({"Close": closes})
        return yc

    def test_computes_from_last_two_closes(self):
        yc = self._yc_with_closes([100.0, 110.0])
        assert pn._daily_change_pct("X", yc) == pytest.approx(10.0)

    def test_single_close_returns_none(self):
        yc = self._yc_with_closes([100.0])
        assert pn._daily_change_pct("X", yc) is None

    def test_zero_prev_close_returns_none(self):
        yc = self._yc_with_closes([0.0, 100.0])
        assert pn._daily_change_pct("X", yc) is None

    def test_exception_returns_none(self):
        yc = MagicMock()
        yc.get_price_history.side_effect = RuntimeError("down")
        assert pn._daily_change_pct("X", yc) is None


# ---------------------------------------------------------------------------
# build_news_watch
# ---------------------------------------------------------------------------

class TestBuildNewsWatch:
    def test_available_false_when_nothing_fetched(self, monkeypatch,
                                                  fake_finnhub, fake_yahoo):
        monkeypatch.setattr(pn, "gather_index_watch", lambda idx=None: [])
        data = pn.build_news_watch(symbols=["AAPL"])
        assert data["available"] is False
        assert data["holding_news"] == {}

    def test_available_true_when_index_only(self, monkeypatch, fake_finnhub, fake_yahoo):
        monkeypatch.setattr(
            pn, "gather_index_watch",
            lambda idx=None: [{"label": "VIX", "symbol": "^VIX",
                               "price": 15.0, "percent_change": -1.0}],
        )
        data = pn.build_news_watch(symbols=[])
        assert data["available"] is True

    def test_uses_portfolio_symbols_when_none_given(self, monkeypatch,
                                                    fake_finnhub, fake_yahoo):
        monkeypatch.setattr(pn, "get_portfolio_symbols", lambda: ["SOXL", "TECL"])
        monkeypatch.setattr(pn, "gather_index_watch", lambda idx=None: [])
        data = pn.build_news_watch()
        assert data["symbols"] == ["SOXL", "TECL"]

    def test_skips_holding_news_when_no_symbols(self, monkeypatch,
                                                fake_finnhub, fake_yahoo):
        called = []
        monkeypatch.setattr(pn, "gather_holding_news",
                            lambda *a, **k: called.append(1) or {})
        monkeypatch.setattr(pn, "gather_index_watch", lambda idx=None: [])
        pn.build_news_watch(symbols=[])
        assert called == []


# ---------------------------------------------------------------------------
# format_news_watch
# ---------------------------------------------------------------------------

class TestFormatNewsWatch:
    def test_empty_data_returns_empty_string(self):
        assert pn.format_news_watch({}) == ""
        assert pn.format_news_watch({"available": False}) == ""
        assert pn.format_news_watch(None) == ""

    def test_renders_indices(self):
        data = {
            "available": True,
            "index_watch": [{"label": "VIX", "symbol": "^VIX",
                             "price": 15.4, "percent_change": -2.1}],
            "holding_news": {}, "market_news": [],
        }
        out = pn.format_news_watch(data)
        assert "VIX 15 (-2.10%)" in out
        assert "保有＋指数 ニュース監視" in out

    def test_renders_holding_news(self):
        data = {
            "available": True, "index_watch": [], "market_news": [],
            "holding_news": {"AAPL": [{"headline": "Apple beats", "source": "CNBC"}]},
        }
        out = pn.format_news_watch(data)
        assert "**AAPL**" in out
        assert "Apple beats (CNBC)" in out

    def test_renders_market_news(self):
        data = {
            "available": True, "index_watch": [], "holding_news": {},
            "market_news": [{"headline": "Fed holds", "source": "Reuters"}],
        }
        out = pn.format_news_watch(data)
        assert "マーケット全体" in out
        assert "Fed holds (Reuters)" in out

    def test_max_symbols_caps_output(self):
        holding = {f"S{i}": [{"headline": "h", "source": "s"}] for i in range(10)}
        data = {"available": True, "index_watch": [], "market_news": [],
                "holding_news": holding}
        out = pn.format_news_watch(data, max_symbols=3)
        assert sum(1 for line in out.splitlines() if line.startswith("- **")) == 3

    def test_long_headline_is_truncated(self):
        data = {
            "available": True, "index_watch": [], "market_news": [],
            "holding_news": {"AAPL": [{"headline": "x" * 200, "source": "S"}]},
        }
        out = pn.format_news_watch(data)
        assert "x" * 80 in out
        assert "x" * 81 not in out


# ---------------------------------------------------------------------------
# get_portfolio_symbols
# ---------------------------------------------------------------------------

class TestGetPortfolioSymbols:
    def test_prefers_live_portfolio_csv(self, monkeypatch):
        from src.core.portfolio import portfolio_io, weekly

        monkeypatch.setattr(
            portfolio_io, "load_portfolio",
            lambda *a, **k: [{"symbol": "SOXL"}, {"symbol": "TECL"}],
        )
        monkeypatch.setattr(
            weekly, "load_holdings_config",
            lambda *a, **k: {"holdings": [{"quote_symbol": "SHOULD_NOT_APPEAR"}]},
        )
        assert pn.get_portfolio_symbols() == ["SOXL", "TECL"]

    def test_falls_back_to_weekly_holdings(self, monkeypatch):
        from src.core.portfolio import portfolio_io, weekly

        monkeypatch.setattr(portfolio_io, "load_portfolio", lambda *a, **k: [])
        monkeypatch.setattr(
            weekly, "load_holdings_config",
            lambda *a, **k: {"holdings": [{"quote_symbol": "2802.T"},
                                          {"quote_symbol": "9843.T"}]},
        )
        assert pn.get_portfolio_symbols() == ["2802.T", "9843.T"]

    def test_both_sources_failing_returns_empty(self, monkeypatch):
        from src.core.portfolio import portfolio_io, weekly

        def boom(*a, **k):
            raise RuntimeError("no data")

        monkeypatch.setattr(portfolio_io, "load_portfolio", boom)
        monkeypatch.setattr(weekly, "load_holdings_config", boom)
        assert pn.get_portfolio_symbols() == []

    def test_dedups_and_skips_blank_symbols(self, monkeypatch):
        from src.core.portfolio import portfolio_io

        monkeypatch.setattr(
            portfolio_io, "load_portfolio",
            lambda *a, **k: [{"symbol": "AAPL"}, {"symbol": " "},
                             {"symbol": "AAPL"}, {}],
        )
        assert pn.get_portfolio_symbols() == ["AAPL"]
