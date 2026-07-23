"""Tests for src/data/finnhub_client.py (保有＋指数ニュース監視)."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.data import finnhub_client as fc

# このモジュールは finnhub_client 自体をテストするため、
# conftest の FINNHUB_API_KEY delenv を回避する（各テストで明示的に設定する）
pytestmark = pytest.mark.no_auto_mock


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """各テスト前にキャッシュとエラー状態をリセットし、キー未設定を既定にする。"""
    fc.reset_cache()
    fc.reset_error_state()
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    yield
    fc.reset_cache()
    fc.reset_error_state()


def _resp(status_code=200, json_data=None, text=""):
    r = MagicMock()
    r.status_code = status_code
    r.text = text
    if json_data is None:
        r.json.side_effect = ValueError("no json")
    else:
        r.json.return_value = json_data
    return r


# ---------------------------------------------------------------------------
# is_available
# ---------------------------------------------------------------------------

class TestIsAvailable:
    def test_with_key(self, monkeypatch):
        monkeypatch.setenv("FINNHUB_API_KEY", "fh-test-key")
        assert fc.is_available() is True

    def test_without_key(self, monkeypatch):
        monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
        assert fc.is_available() is False
        assert fc.get_error_status()["status"] == "not_configured"

    def test_empty_key(self, monkeypatch):
        monkeypatch.setenv("FINNHUB_API_KEY", "   ")
        assert fc.is_available() is False


# ---------------------------------------------------------------------------
# graceful degradation (キー未設定)
# ---------------------------------------------------------------------------

class TestGracefulDegradationWithoutKey:
    def test_company_news_returns_empty(self, monkeypatch):
        monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
        with patch.object(fc.requests, "get") as mock_get:
            assert fc.get_company_news("AAPL") == []
            mock_get.assert_not_called()

    def test_market_news_returns_empty(self, monkeypatch):
        monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
        with patch.object(fc.requests, "get") as mock_get:
            assert fc.get_market_news() == []
            mock_get.assert_not_called()

    def test_quote_returns_none(self, monkeypatch):
        monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
        with patch.object(fc.requests, "get") as mock_get:
            assert fc.get_quote("AAPL") is None
            mock_get.assert_not_called()


# ---------------------------------------------------------------------------
# get_company_news
# ---------------------------------------------------------------------------

class TestGetCompanyNews:
    def test_returns_normalized_sorted_articles(self, monkeypatch):
        monkeypatch.setenv("FINNHUB_API_KEY", "fh-test-key")
        payload = [
            {"headline": "old", "summary": "s1", "source": "Reuters",
             "url": "u1", "datetime": 100, "related": "AAPL"},
            {"headline": "new", "summary": "s2", "source": "CNBC",
             "url": "u2", "datetime": 300, "related": "AAPL"},
            {"headline": "mid", "summary": "s3", "source": "WSJ",
             "url": "u3", "datetime": 200, "related": "AAPL"},
        ]
        with patch.object(fc.requests, "get", return_value=_resp(json_data=payload)):
            out = fc.get_company_news("aapl", days=7, limit=5)

        assert [a["headline"] for a in out] == ["new", "mid", "old"]
        assert out[0]["source"] == "CNBC"
        assert out[0]["url"] == "u2"

    def test_limit_is_applied(self, monkeypatch):
        monkeypatch.setenv("FINNHUB_API_KEY", "fh-test-key")
        payload = [{"headline": f"h{i}", "datetime": i} for i in range(10)]
        with patch.object(fc.requests, "get", return_value=_resp(json_data=payload)):
            out = fc.get_company_news("AAPL", limit=3)
        assert len(out) == 3

    def test_skips_articles_without_headline(self, monkeypatch):
        monkeypatch.setenv("FINNHUB_API_KEY", "fh-test-key")
        payload = [{"headline": "", "datetime": 1}, {"headline": "ok", "datetime": 2}]
        with patch.object(fc.requests, "get", return_value=_resp(json_data=payload)):
            out = fc.get_company_news("AAPL")
        assert [a["headline"] for a in out] == ["ok"]

    def test_empty_symbol_short_circuits(self, monkeypatch):
        monkeypatch.setenv("FINNHUB_API_KEY", "fh-test-key")
        with patch.object(fc.requests, "get") as mock_get:
            assert fc.get_company_news("") == []
            mock_get.assert_not_called()

    def test_symbol_is_uppercased_in_request(self, monkeypatch):
        monkeypatch.setenv("FINNHUB_API_KEY", "fh-test-key")
        with patch.object(fc.requests, "get", return_value=_resp(json_data=[])) as mock_get:
            fc.get_company_news("aapl")
        assert mock_get.call_args.kwargs["params"]["symbol"] == "AAPL"

    def test_non_list_payload_returns_empty(self, monkeypatch):
        monkeypatch.setenv("FINNHUB_API_KEY", "fh-test-key")
        with patch.object(fc.requests, "get", return_value=_resp(json_data={"error": "x"})):
            assert fc.get_company_news("AAPL") == []


# ---------------------------------------------------------------------------
# get_market_news
# ---------------------------------------------------------------------------

class TestGetMarketNews:
    def test_returns_sorted_articles(self, monkeypatch):
        monkeypatch.setenv("FINNHUB_API_KEY", "fh-test-key")
        payload = [
            {"headline": "a", "datetime": 10, "source": "S1"},
            {"headline": "b", "datetime": 50, "source": "S2"},
        ]
        with patch.object(fc.requests, "get", return_value=_resp(json_data=payload)):
            out = fc.get_market_news(limit=5)
        assert [a["headline"] for a in out] == ["b", "a"]

    def test_category_passed_through(self, monkeypatch):
        monkeypatch.setenv("FINNHUB_API_KEY", "fh-test-key")
        with patch.object(fc.requests, "get", return_value=_resp(json_data=[])) as mock_get:
            fc.get_market_news(category="forex")
        assert mock_get.call_args.kwargs["params"]["category"] == "forex"


# ---------------------------------------------------------------------------
# get_quote
# ---------------------------------------------------------------------------

class TestGetQuote:
    def test_maps_finnhub_keys(self, monkeypatch):
        monkeypatch.setenv("FINNHUB_API_KEY", "fh-test-key")
        payload = {"c": 100.5, "d": 1.5, "dp": 1.5, "h": 101, "l": 99, "pc": 99.0}
        with patch.object(fc.requests, "get", return_value=_resp(json_data=payload)):
            q = fc.get_quote("AAPL")
        assert q == {
            "current": 100.5, "change": 1.5, "percent_change": 1.5,
            "high": 101, "low": 99, "prev_close": 99.0,
        }

    def test_all_zero_quote_returns_none(self, monkeypatch):
        """無効シンボルは全ゼロで返るため None にする（0円と誤読させない）。"""
        monkeypatch.setenv("FINNHUB_API_KEY", "fh-test-key")
        payload = {"c": 0, "d": None, "dp": None, "h": 0, "l": 0, "pc": 0}
        with patch.object(fc.requests, "get", return_value=_resp(json_data=payload)):
            assert fc.get_quote("^GSPC") is None

    def test_missing_c_key_returns_none(self, monkeypatch):
        monkeypatch.setenv("FINNHUB_API_KEY", "fh-test-key")
        with patch.object(fc.requests, "get", return_value=_resp(json_data={})):
            assert fc.get_quote("AAPL") is None

    def test_empty_symbol_returns_none(self, monkeypatch):
        monkeypatch.setenv("FINNHUB_API_KEY", "fh-test-key")
        with patch.object(fc.requests, "get") as mock_get:
            assert fc.get_quote("") is None
            mock_get.assert_not_called()


# ---------------------------------------------------------------------------
# エラー状態トラッキング
# ---------------------------------------------------------------------------

class TestErrorState:
    def test_auth_error(self, monkeypatch):
        monkeypatch.setenv("FINNHUB_API_KEY", "bad-key")
        with patch.object(fc.requests, "get", return_value=_resp(status_code=401)):
            assert fc.get_company_news("AAPL") == []
        st = fc.get_error_status()
        assert st["status"] == "auth_error"
        assert st["status_code"] == 401

    def test_rate_limited(self, monkeypatch):
        monkeypatch.setenv("FINNHUB_API_KEY", "fh-test-key")
        with patch.object(fc.requests, "get", return_value=_resp(status_code=429)):
            assert fc.get_market_news() == []
        assert fc.get_error_status()["status"] == "rate_limited"

    def test_timeout(self, monkeypatch):
        monkeypatch.setenv("FINNHUB_API_KEY", "fh-test-key")
        with patch.object(fc.requests, "get",
                          side_effect=requests.exceptions.Timeout()):
            assert fc.get_company_news("AAPL") == []
        assert fc.get_error_status()["status"] == "timeout"

    def test_connection_error(self, monkeypatch):
        monkeypatch.setenv("FINNHUB_API_KEY", "fh-test-key")
        with patch.object(fc.requests, "get",
                          side_effect=requests.exceptions.ConnectionError("boom")):
            assert fc.get_company_news("AAPL") == []
        assert fc.get_error_status()["status"] == "other_error"

    def test_http_500(self, monkeypatch):
        monkeypatch.setenv("FINNHUB_API_KEY", "fh-test-key")
        with patch.object(fc.requests, "get",
                          return_value=_resp(status_code=500, text="server error")):
            assert fc.get_market_news() == []
        st = fc.get_error_status()
        assert st["status"] == "other_error"
        assert st["status_code"] == 500

    def test_invalid_json(self, monkeypatch):
        monkeypatch.setenv("FINNHUB_API_KEY", "fh-test-key")
        with patch.object(fc.requests, "get", return_value=_resp(json_data=None)):
            assert fc.get_market_news() == []
        assert fc.get_error_status()["status"] == "other_error"

    def test_success_resets_error_state(self, monkeypatch):
        monkeypatch.setenv("FINNHUB_API_KEY", "fh-test-key")
        with patch.object(fc.requests, "get", return_value=_resp(status_code=429)):
            fc.get_market_news()
        assert fc.get_error_status()["status"] == "rate_limited"

        fc.reset_cache()
        with patch.object(fc.requests, "get", return_value=_resp(json_data=[])):
            fc.get_market_news()
        assert fc.get_error_status()["status"] == "ok"


# ---------------------------------------------------------------------------
# キャッシュ
# ---------------------------------------------------------------------------

class TestCache:
    def test_second_call_hits_cache(self, monkeypatch):
        monkeypatch.setenv("FINNHUB_API_KEY", "fh-test-key")
        payload = [{"headline": "h", "datetime": 1}]
        with patch.object(fc.requests, "get",
                          return_value=_resp(json_data=payload)) as mock_get:
            fc.get_company_news("AAPL")
            fc.get_company_news("AAPL")
        assert mock_get.call_count == 1

    def test_different_symbols_are_cached_separately(self, monkeypatch):
        monkeypatch.setenv("FINNHUB_API_KEY", "fh-test-key")
        with patch.object(fc.requests, "get",
                          return_value=_resp(json_data=[])) as mock_get:
            fc.get_company_news("AAPL")
            fc.get_company_news("MSFT")
        assert mock_get.call_count == 2

    def test_expired_cache_refetches(self, monkeypatch):
        monkeypatch.setenv("FINNHUB_API_KEY", "fh-test-key")
        with patch.object(fc.requests, "get",
                          return_value=_resp(json_data=[])) as mock_get:
            fc.get_company_news("AAPL")
            # 全エントリの timestamp を TTL より古くする
            for k, (_, v) in list(fc._cache.items()):
                fc._cache[k] = (0.0, v)
            fc.get_company_news("AAPL")
        assert mock_get.call_count == 2

    def test_reset_cache_clears(self, monkeypatch):
        monkeypatch.setenv("FINNHUB_API_KEY", "fh-test-key")
        with patch.object(fc.requests, "get",
                          return_value=_resp(json_data=[])) as mock_get:
            fc.get_company_news("AAPL")
            fc.reset_cache()
            fc.get_company_news("AAPL")
        assert mock_get.call_count == 2

    def test_error_response_is_not_cached(self, monkeypatch):
        """失敗を15分キャッシュすると復旧を待てないので、エラーはキャッシュしない。"""
        monkeypatch.setenv("FINNHUB_API_KEY", "fh-test-key")
        with patch.object(fc.requests, "get",
                          return_value=_resp(status_code=500)) as mock_get:
            fc.get_market_news()
            fc.get_market_news()
        assert mock_get.call_count == 2
