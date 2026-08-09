"""多段フォールバック解決 -- 「取得できなかった」で止めない.

## 運用ルール（2026-08-09 に確定）

> あるリソースから情報が取得できなかったら「取得できなかった」と書くんじゃなくて、
> **他のあらゆる手段を講じて取得できるまでトライすること。**
> 意味のないレポートにしない。

これまでの原則「取得失敗を結果と混同しない」は**正しいが不十分**だった。
「取れなかった」と正直に書くだけで、**取りに行く努力をしていなかった**。

新しい原則:

    1. 手段Aで取る
    2. 失敗 → 手段B
    3. 失敗 → 手段C
    4. **全部失敗して初めて**「取得できなかった（試した手段: A/B/C）」と書く

**どの手段で取れたかを必ず記録する。** 予備で取った値を一次経路の値のように
扱うと、品質の差が見えなくなる。

## 実測で確認した経路（2026-08-09）

| 種別 | 経路 | 米国株 | 日本株 |
|:---|:---|:---:|:---:|
| 価格 | `yfinance Ticker.info` | ✅ | ✅ |
| 価格 | `yfinance fast_info` | ✅ | ✅ |
| 価格 | `yf.download()` | ✅ | ✅ |
| 価格 | `finnhub quote` | ✅ | ❌（対応なし） |
| ニュース | `finnhub company_news` | ✅ | ❌ |
| ニュース | `yfinance Ticker.news` | ✅ | ✅ |
| 財務 | `yfinance` | ✅ | ✅ |
| 財務 | `SEC XBRL` | ✅ | ❌ |
| 決算日 | `calendar` → `earnings_dates` | ✅ | ✅ |

stooq は 404 で使えなかった（2026-08-09 実測）。
"""

from __future__ import annotations

from typing import Any, Callable, Optional


class Attempt:
    """1回の取得試行の記録。**何を試して何が起きたかを残す。**"""

    __slots__ = ("source", "ok", "error", "value")

    def __init__(self, source: str, ok: bool, error: str = "", value: Any = None):
        self.source = source
        self.ok = ok
        self.error = error
        self.value = value

    def as_dict(self) -> dict:
        return {"source": self.source, "ok": self.ok, "error": self.error}


def resolve(
    chain: list[tuple[str, Callable[[], Any]]],
    is_valid: Optional[Callable[[Any], bool]] = None,
    label: str = "",
) -> dict:
    """手段を順に試し、**最初に成功したものを返す**。

    Returns
    -------
    dict
        {"available", "value", "source", "attempts", "note"}

    - `source` … 実際に取れた経路。**予備で取ったことを隠さない。**
    - `attempts` … 試した全経路と失敗理由。
    - 全滅時の `note` は「取得できなかった（試した手段: …）」であり、
      **どれだけ手を尽くしたかが読み手に伝わる形**にする。
    """
    valid = is_valid or (lambda v: v is not None)
    attempts: list[Attempt] = []

    for name, fn in chain:
        try:
            value = fn()
        except Exception as exc:
            attempts.append(Attempt(name, False, f"{type(exc).__name__}: {exc}"))
            continue
        if valid(value):
            attempts.append(Attempt(name, True, value=value))
            note = f"{label or '値'}を {name} から取得しました。"
            if len(attempts) > 1:
                failed = "、".join(a.source for a in attempts[:-1])
                note += f"（{failed} は失敗したため予備経路を使用）"
            return {
                "available": True, "value": value, "source": name,
                "fallback_used": len(attempts) > 1,
                "attempts": [a.as_dict() for a in attempts],
                "note": note,
            }
        attempts.append(Attempt(name, False, "空・不正な値"))

    tried = "、".join(a.source for a in attempts) or "（試行なし）"
    return {
        "available": False, "value": None, "source": None,
        "fallback_used": False,
        "attempts": [a.as_dict() for a in attempts],
        "note": (f"{label or '値'}を取得できませんでした。"
                 f"**試した手段: {tried}。** 全て失敗しています。"
                 "これは『データが存在しない』ではありません。"),
    }


# ---------------------------------------------------------------------------
# 価格
# ---------------------------------------------------------------------------


def _is_price(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0


def resolve_price(symbol: str) -> dict:
    """価格を、使える手段を全部試して取る。

    yfinance の `info` が落ちても `fast_info` や `download` が通ることがある
    （実測済み）。米国株なら finnhub の quote も使える。
    """
    def via_info():
        import yfinance as yf

        return (yf.Ticker(symbol).info or {}).get("regularMarketPrice")

    def via_fast():
        import yfinance as yf

        fi = yf.Ticker(symbol).fast_info
        return fi.get("lastPrice") if hasattr(fi, "get") else None

    def via_download():
        import yfinance as yf

        df = yf.download(symbol, period="5d", progress=False,
                         auto_adjust=True, threads=False)
        if df is None or df.empty:
            return None
        close = df["Close"]
        if hasattr(close, "columns"):
            close = close[close.columns[0]]
        series = close.dropna()
        return float(series.iloc[-1]) if len(series) else None

    def via_history():
        from src.data import yahoo_client as yc

        hist = yc.get_price_history(symbol, period="5d")
        if hist is None or hist.empty:
            return None
        series = hist["Close"].dropna()
        return float(series.iloc[-1]) if len(series) else None

    def via_finnhub():
        from src.data import finnhub_client as fh

        if not fh.is_available():
            return None
        q = fh.get_quote(symbol) or {}
        return q.get("current")

    chain = [
        ("yfinance.info", via_info),
        ("yfinance.fast_info", via_fast),
        ("yfinance.download", via_download),
        ("yfinance.history", via_history),
    ]
    # finnhub は日本株に対応しないので、米国株のときだけ足す
    if not str(symbol).upper().endswith(".T"):
        chain.append(("finnhub.quote", via_finnhub))

    return resolve(chain, is_valid=_is_price, label=f"{symbol} の価格")


# ---------------------------------------------------------------------------
# 価格系列
# ---------------------------------------------------------------------------


def resolve_history(symbol: str, period: str = "1y") -> dict:
    """価格系列。`Ticker.history` が空でも `download` が通ることがある。"""
    def via_history():
        import yfinance as yf

        df = yf.Ticker(symbol).history(period=period)
        return df if df is not None and not df.empty else None

    def via_download():
        import yfinance as yf

        df = yf.download(symbol, period=period, progress=False,
                         auto_adjust=True, threads=False)
        if df is None or df.empty:
            return None
        if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
            df = df.xs(symbol, axis=1, level=1, drop_level=True)
        return df

    return resolve(
        [("yfinance.history", via_history), ("yfinance.download", via_download)],
        is_valid=lambda df: df is not None and not getattr(df, "empty", True),
        label=f"{symbol} の価格系列",
    )


# ---------------------------------------------------------------------------
# 決算日
# ---------------------------------------------------------------------------


def resolve_earnings_dates(symbol: str) -> dict:
    """決算日。calendar → earnings_dates の順に試す。"""
    def via_calendar():
        import yfinance as yf

        cal = yf.Ticker(symbol).calendar
        if isinstance(cal, dict):
            value = cal.get("Earnings Date") or cal.get("earningsDate")
            if value is None:
                return None
            items = value if isinstance(value, (list, tuple)) else [value]
            return [str(d)[:10] for d in items if d]
        return None

    def via_earnings_dates():
        from datetime import date

        import yfinance as yf

        df = yf.Ticker(symbol).earnings_dates
        if df is None or df.empty:
            return None
        today = date.today()
        out = [d.date().isoformat() for d in df.index
               if hasattr(d, "date") and d.date() >= today]
        return out or None

    return resolve(
        [("yfinance.calendar", via_calendar),
         ("yfinance.earnings_dates", via_earnings_dates)],
        is_valid=lambda v: bool(v),
        label=f"{symbol} の決算日",
    )


# ---------------------------------------------------------------------------
# 財務（米国株は SEC XBRL に落とせる）
# ---------------------------------------------------------------------------


def resolve_fundamentals(symbol: str) -> dict:
    """主要ファンダ。yfinance が欠けたら SEC XBRL（米国株のみ）で補う。"""
    def via_yfinance():
        from src.data import yahoo_client as yc

        d = yc.get_stock_detail(symbol) or yc.get_stock_info(symbol)
        if not d:
            return None
        keys = ("per", "pbr", "roe", "operating_margin", "revenue_growth")
        return d if any(d.get(k) is not None for k in keys) else None

    def via_sec():
        from src.data import edgar_client

        if not edgar_client.is_available():
            return None
        res = edgar_client.key_financials(symbol)
        if not res.get("available"):
            return None
        facts = res["facts"]
        return {
            "symbol": symbol,
            "_source": "sec_xbrl",
            "revenue": (facts.get("revenue") or {}).get("value"),
            "operating_income": (facts.get("operating_income") or {}).get("value"),
            "net_income": (facts.get("net_income") or {}).get("value"),
            "equity": (facts.get("equity") or {}).get("value"),
            "assets": (facts.get("assets") or {}).get("value"),
            "period": (facts.get("revenue") or {}).get("end"),
            "note": "yfinance が欠けたため SEC の XBRL（開示原文）から補完。",
        }

    chain = [("yfinance", via_yfinance)]
    if not str(symbol).upper().endswith(".T"):
        chain.append(("sec.xbrl", via_sec))
    return resolve(chain, label=f"{symbol} のファンダメンタル")


# ---------------------------------------------------------------------------
# ニュース
# ---------------------------------------------------------------------------


def resolve_news(symbol: str, days: int = 7, limit: int = 3) -> dict:
    """ニュース。finnhub（米国）→ yfinance（日米両対応）。"""
    def via_finnhub():
        from src.data import finnhub_client as fh

        if not fh.is_available():
            return None
        rows = fh.get_company_news(symbol, days=days, limit=limit) or []
        return [{**a, "source_api": "finnhub",
                 "provenance": "external_discourse"} for a in rows] or None

    def via_yfinance():
        import yfinance as yf

        out = []
        for raw in (yf.Ticker(symbol).news or [])[:limit]:
            c = raw.get("content") or raw
            title = c.get("title") or c.get("headline")
            if not title:
                continue
            url = c.get("canonicalUrl")
            out.append({
                "headline": str(title),
                "url": (url.get("url") if isinstance(url, dict) else c.get("link")) or "",
                "datetime": c.get("pubDate") or c.get("providerPublishTime"),
                "source_api": "yfinance",
                "provenance": "external_discourse",
            })
        return out or None

    return resolve(
        [("finnhub.company_news", via_finnhub), ("yfinance.news", via_yfinance)],
        is_valid=lambda v: bool(v),
        label=f"{symbol} のニュース",
    )
