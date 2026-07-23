"""Finnhub API wrapper for portfolio & index news monitoring.

保有銘柄・主要指数のニュースを分析時に取得するためのクライアント。

API key is read from the ``FINNHUB_API_KEY`` environment variable
(loaded from project-root ``.env``). When the key is not set,
:func:`is_available` returns ``False`` and every fetch function returns
an empty result (graceful degradation) — mirrors the grok_client pattern.

設計方針:
- ネットワーク/認証失敗は握り潰して空を返す（分析本体を止めない）
- レート制限/エラーは _error_state に記録し get_error_status() で確認可能
- 直近ニュースは軽量キャッシュ（同一プロセス内・TTL付き）で重複取得を避ける
"""

from __future__ import annotations

import os
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

# Load .env from project root (src/data/finnhub_client.py -> parents[2])
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

_BASE_URL = "https://finnhub.io/api/v1"
_TIMEOUT = 8

# ---------------------------------------------------------------------------
# Error state tracking (grok_client と同型)
# ---------------------------------------------------------------------------
_error_state: dict = {
    "status": "ok",       # not_configured | ok | auth_error | rate_limited | timeout | other_error
    "status_code": None,  # int | None
    "message": "",
}
_error_warned = [False]

# 同一プロセス内キャッシュ: key=(endpoint, params) -> (timestamp, value)
_cache: dict = {}
_CACHE_TTL = 900  # 15分


def get_error_status() -> dict:
    """現在の Finnhub API エラー状態を返す。"""
    return dict(_error_state)


def reset_error_state() -> None:
    """エラー状態を 'ok' に戻す。"""
    _error_state["status"] = "ok"
    _error_state["status_code"] = None
    _error_state["message"] = ""


def reset_cache() -> None:
    """プロセス内キャッシュをクリア（主にテスト用）。"""
    _cache.clear()


def _get_api_key() -> Optional[str]:
    key = os.environ.get("FINNHUB_API_KEY", "").strip()
    return key or None


def is_available() -> bool:
    """API キーが設定されていれば True。"""
    if _get_api_key() is None:
        _error_state["status"] = "not_configured"
        return False
    return True


def _record_error(status: str, code: Optional[int], message: str) -> None:
    _error_state["status"] = status
    _error_state["status_code"] = code
    _error_state["message"] = message[:200]


def _call(endpoint: str, params: dict) -> Optional[list | dict]:
    """Finnhub API を叩く。失敗時は None を返し _error_state に記録。"""
    key = _get_api_key()
    if key is None:
        _record_error("not_configured", None, "FINNHUB_API_KEY 未設定")
        return None

    # キャッシュ判定
    cache_key = (endpoint, tuple(sorted(params.items())))
    hit = _cache.get(cache_key)
    if hit is not None and (time.time() - hit[0]) < _CACHE_TTL:
        return hit[1]

    q = dict(params)
    q["token"] = key
    try:
        resp = requests.get(f"{_BASE_URL}/{endpoint}", params=q, timeout=_TIMEOUT)
    except requests.exceptions.Timeout:
        _record_error("timeout", None, f"{endpoint} timeout")
        return None
    except requests.exceptions.RequestException as e:
        _record_error("other_error", None, f"{endpoint}: {e}")
        return None

    if resp.status_code == 401:
        _record_error("auth_error", 401, "APIキーが無効です")
        return None
    if resp.status_code == 429:
        _record_error("rate_limited", 429, "レート制限に達しました")
        return None
    if resp.status_code != 200:
        _record_error("other_error", resp.status_code, resp.text[:200])
        return None

    try:
        data = resp.json()
    except ValueError:
        _record_error("other_error", 200, "JSON パース失敗")
        return None

    reset_error_state()
    _cache[cache_key] = (time.time(), data)
    return data


def _normalize_article(raw: dict) -> dict:
    """Finnhub のニュース dict を共通スキーマに正規化。"""
    return {
        "headline": (raw.get("headline") or "").strip(),
        "summary": (raw.get("summary") or "").strip(),
        "source": raw.get("source") or "",
        "url": raw.get("url") or "",
        "datetime": raw.get("datetime") or 0,  # unix秒
        "related": raw.get("related") or "",
    }


def get_company_news(symbol: str, days: int = 7, limit: int = 5) -> list[dict]:
    """個別銘柄の直近ニュースを取得（新しい順）。

    キー未設定・エラー時は空リストを返す（graceful degradation）。
    ETF/指数によっては company-news 非対応で空になることがある。
    """
    if not symbol:
        return []
    to = date.today()
    frm = to - timedelta(days=max(1, days))
    data = _call(
        "company-news",
        {"symbol": symbol.upper(), "from": frm.isoformat(), "to": to.isoformat()},
    )
    if not isinstance(data, list):
        return []
    articles = [_normalize_article(a) for a in data if a.get("headline")]
    articles.sort(key=lambda a: a["datetime"], reverse=True)
    return articles[:limit]


def get_market_news(category: str = "general", limit: int = 5) -> list[dict]:
    """マーケット全体のニュースを取得。

    category: general | forex | crypto | merger
    キー未設定・エラー時は空リストを返す。
    """
    data = _call("news", {"category": category})
    if not isinstance(data, list):
        return []
    articles = [_normalize_article(a) for a in data if a.get("headline")]
    articles.sort(key=lambda a: a["datetime"], reverse=True)
    return articles[:limit]


def get_quote(symbol: str) -> Optional[dict]:
    """指数・銘柄の現在値クオートを取得（指数監視用）。

    返り値: {"current": float, "change": float, "percent_change": float,
             "high": float, "low": float, "prev_close": float} or None
    """
    if not symbol:
        return None
    data = _call("quote", {"symbol": symbol.upper()})
    if not isinstance(data, dict) or "c" not in data:
        return None
    # Finnhub quote: c=current, d=change, dp=percent, h=high, l=low, pc=prev close
    if data.get("c") in (0, None) and data.get("pc") in (0, None):
        return None  # 無効シンボル（全ゼロ）
    return {
        "current": data.get("c"),
        "change": data.get("d"),
        "percent_change": data.get("dp"),
        "high": data.get("h"),
        "low": data.get("l"),
        "prev_close": data.get("pc"),
    }
