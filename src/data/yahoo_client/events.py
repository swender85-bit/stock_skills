"""銘柄の前方イベント（決算日・配当権利日）取得 (土曜設計書 提案4)。

## なぜ yahoo_client の中にあるのか

開発ルール: データ取得は必ず `src/data/yahoo_client` 経由（直接 yfinance を呼ばない）。
決算日・配当落ち日は既存の `get_stock_detail` に含まれていないので、ここに足す。

## 日程は「変更され得る」

決算日はしばしば変更される（設計書 提案4-⑧）。したがって取得値には必ず
`fetched_at` と `source` を添え、確定情報として扱わせない。
前週分との差分検出（決算日の変更＝しばしば重要なシグナル）は上位層が行う。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional

import yfinance as yf

#: 取得結果のメモリキャッシュ（同一プロセス内で保有×複数節ぶん叩かないため）
_cache: dict[str, dict] = {}


def clear_event_cache() -> None:
    _cache.clear()


def _as_date(value: Any) -> Optional[str]:
    """yfinance が返す各種の日付表現を ISO 文字列に均す。"""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()[:10]
        try:
            date.fromisoformat(text)
            return text
        except ValueError:
            return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    # pandas.Timestamp / numpy datetime64
    for attr in ("date", "to_pydatetime"):
        fn = getattr(value, attr, None)
        if callable(fn):
            try:
                got = fn()
                return got.isoformat()[:10] if got else None
            except Exception:
                continue
    # epoch 秒
    if isinstance(value, (int, float)) and value > 1_000_000_000:
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc).date().isoformat()
        except Exception:
            return None
    return None


def get_symbol_events(symbol: str, use_cache: bool = True) -> dict:
    """1銘柄の決算予定日・配当落ち日等を返す。

    Returns:
        {"symbol", "available", "earnings_dates": [ISO...], "ex_dividend_date",
         "dividend_date", "fetched_at", "source", "error"}

    取得できなかった場合は `available=False`。**空リストを「予定なし」と
    解釈してはならない**（設計書の縮退原則）。
    """
    key = (symbol or "").strip().upper()
    if not key:
        return {"symbol": symbol, "available": False, "earnings_dates": [],
                "error": "シンボルが空です"}
    if use_cache and key in _cache:
        return _cache[key]

    result = {
        "symbol": symbol, "available": False, "earnings_dates": [],
        "ex_dividend_date": None, "dividend_date": None,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "yfinance", "error": None,
    }

    try:
        ticker = yf.Ticker(symbol)
    except Exception as e:
        result["error"] = f"{type(e).__name__}"
        return result

    got_any = False

    # calendar は dict または DataFrame で返る（yfinance のバージョン差）
    try:
        cal = ticker.calendar
    except Exception:
        cal = None

    for field, key_names in (
        ("earnings_dates", ("Earnings Date", "earningsDate")),
        ("ex_dividend_date", ("Ex-Dividend Date", "exDividendDate")),
        ("dividend_date", ("Dividend Date", "dividendDate")),
    ):
        value = _lookup(cal, key_names)
        if value is None:
            continue
        if field == "earnings_dates":
            dates = [d for d in (_as_date(v) for v in _listify(value)) if d]
            if dates:
                result["earnings_dates"] = sorted(set(dates))
                got_any = True
        else:
            iso = _as_date(value)
            if iso:
                result[field] = iso
                got_any = True

    # calendar から決算日が取れないことがあるので、専用APIも試す
    if not result["earnings_dates"]:
        try:
            df = ticker.get_earnings_dates(limit=8)
        except Exception:
            df = None
        upcoming = _future_index_dates(df)
        if upcoming:
            result["earnings_dates"] = upcoming
            got_any = True

    result["available"] = got_any
    if not got_any:
        result["error"] = "決算日・配当日を取得できませんでした"

    if use_cache:
        _cache[key] = result
    return result


def _lookup(container: Any, keys: tuple[str, ...]) -> Any:
    if container is None:
        return None
    if isinstance(container, dict):
        for k in keys:
            if k in container and container[k] is not None:
                return container[k]
        return None
    # DataFrame 形式（index に項目名）
    for k in keys:
        try:
            if k in container.index:
                row = container.loc[k]
                return row.iloc[0] if hasattr(row, "iloc") else row
        except Exception:
            continue
    return None


def _listify(value: Any) -> list:
    if isinstance(value, (list, tuple, set)):
        return list(value)
    if hasattr(value, "tolist"):
        try:
            return list(value.tolist())
        except Exception:
            pass
    return [value]


def _future_index_dates(df: Any) -> list[str]:
    """`get_earnings_dates` の index から未来日だけを取る。"""
    if df is None:
        return []
    try:
        idx = list(df.index)
    except Exception:
        return []
    today = date.today().isoformat()
    out = [d for d in (_as_date(i) for i in idx) if d and d >= today]
    return sorted(set(out))


def get_events_for(symbols: list[str], use_cache: bool = True) -> dict[str, dict]:
    """複数銘柄。1銘柄が落ちても他は返す。"""
    out: dict[str, dict] = {}
    for s in symbols or []:
        if not s:
            continue
        try:
            out[s] = get_symbol_events(s, use_cache=use_cache)
        except Exception as e:
            out[s] = {"symbol": s, "available": False, "earnings_dates": [],
                      "error": f"{type(e).__name__}"}
    return out
