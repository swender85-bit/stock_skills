"""保有銘柄・主要指数のニュース監視アグリゲーター。

分析系スキル実行時に、保有銘柄すべて＋主要指数の直近ニュースと
指数水準を自動で取得して分析に織り込むための集約レイヤー。

## ソースの層構成（フォールバック）

| 対象 | 一次ソース | フォールバック |
|:---|:---|:---|
| 米国株ニュース | Finnhub company-news | yahoo get_stock_news |
| 日本株ニュース | yahoo get_stock_news | （Finnhubはフリー枠で日本株非対応） |
| マーケット全体 | Finnhub market-news | — |
| 指数水準 | yahoo get_stock_info | — |
| （将来）気配・板 | moomoo OpenD | — |

いずれのソースも未設定/失敗時は空を返す（graceful degradation）。
Finnhub フリー枠は指数クオート・日本株ニュース非対応のため yahoo で補完する。
"""

from __future__ import annotations

from typing import Any, Optional

# 監視対象の主要指数（label, ティッカー）。yahoo_client で水準を取得。
DEFAULT_INDICES: list[dict] = [
    {"label": "S&P500", "symbol": "^GSPC"},
    {"label": "Nasdaq100", "symbol": "^NDX"},
    {"label": "SOX半導体", "symbol": "^SOX"},
    {"label": "日経225", "symbol": "^N225"},
    {"label": "VIX", "symbol": "^VIX"},
]


def get_portfolio_symbols() -> list[str]:
    """保有銘柄のティッカーを取得。

    portfolio.csv（ライブPF）→ 無ければ weekly_holdings.yaml の順。
    どちらも取れなければ空リスト。
    """
    symbols: list[str] = []
    # 1) ライブ PF (portfolio.csv)
    try:
        from src.core.portfolio.portfolio_io import load_portfolio

        for row in load_portfolio():
            sym = (row.get("symbol") or "").strip()
            if sym:
                symbols.append(sym)
    except Exception:
        pass
    if symbols:
        return _dedup(symbols)

    # 2) weekly_holdings.yaml（基準スナップショットの写し）
    try:
        from src.core.portfolio.weekly import load_holdings_config

        config = load_holdings_config()
        for h in config.get("holdings", []):
            sym = h.get("quote_symbol")
            if sym:
                symbols.append(str(sym).strip())
    except Exception:
        pass
    return _dedup(symbols)


def _dedup(items: list[str]) -> list[str]:
    seen: set = set()
    out: list[str] = []
    for it in items:
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _is_japanese(symbol: str) -> bool:
    """日本株ティッカー（.T / .JP サフィックス）か。"""
    s = symbol.upper()
    return s.endswith(".T") or s.endswith(".JP")


def _from_yahoo_news(raw: dict) -> dict:
    """yahoo get_stock_news の dict を共通スキーマに正規化。"""
    return {
        "headline": (raw.get("title") or "").strip(),
        "summary": "",
        "source": raw.get("publisher") or "yahoo",
        "url": raw.get("link") or "",
        "datetime": raw.get("publish_time") or 0,
    }


def gather_holding_news(symbols: list[str], per_symbol: int = 3) -> dict[str, list[dict]]:
    """各保有銘柄の直近ニュースを取得。

    米国株は Finnhub、空/日本株は yahoo にフォールバック。
    返り値: {symbol: [記事dict, ...]}（空の銘柄はキーごと省略）
    """
    from src.data import finnhub_client as fc

    try:
        from src.data import yahoo_client as yc
    except Exception:
        yc = None

    result: dict[str, list[dict]] = {}
    for sym in symbols:
        articles: list[dict] = []
        # 日本株以外はまず Finnhub
        if not _is_japanese(sym):
            articles = fc.get_company_news(sym, days=7, limit=per_symbol)
        # Finnhubが空 or 日本株 → yahoo フォールバック
        if not articles and yc is not None:
            try:
                raw = yc.get_stock_news(sym, count=per_symbol)
                articles = [_from_yahoo_news(a) for a in (raw or []) if a.get("title")]
                articles = articles[:per_symbol]
            except Exception:
                articles = []
        if articles:
            result[sym] = articles
    return result


def gather_market_news(limit: int = 4) -> list[dict]:
    """マーケット全体のニュース（Finnhub general）。"""
    from src.data import finnhub_client as fc

    return fc.get_market_news(category="general", limit=limit)


def gather_index_watch(indices: Optional[list[dict]] = None) -> list[dict]:
    """主要指数の現在水準と騰落率を取得（yahoo）。

    返り値: [{label, symbol, price, percent_change}, ...]（取得失敗はスキップ）
    """
    if indices is None:
        indices = DEFAULT_INDICES
    try:
        from src.data import yahoo_client as yc
    except Exception:
        return []

    out: list[dict] = []
    for idx in indices:
        sym = idx.get("symbol", "")
        try:
            info = yc.get_stock_info(sym)
        except Exception:
            info = None
        if not info:
            continue
        price = info.get("price") or info.get("current_price")
        if price is None:
            continue
        out.append(
            {
                "label": idx.get("label", sym),
                "symbol": sym,
                "price": price,
                "percent_change": _daily_change_pct(sym, yc),
            }
        )
    return out


def _daily_change_pct(symbol: str, yc: Any) -> Optional[float]:
    """直近2終値から日次騰落率(%)を算出。取得不可なら None。

    get_stock_info は日次変化率を持たないため、価格履歴で補う。
    """
    try:
        hist = yc.get_price_history(symbol, period="5d")
        closes = list(hist["Close"].dropna())
        if len(closes) < 2:
            return None
        prev, last = float(closes[-2]), float(closes[-1])
        if prev == 0:
            return None
        return (last - prev) / prev * 100.0
    except Exception:
        return None


def build_news_watch(
    symbols: Optional[list[str]] = None,
    indices: Optional[list[dict]] = None,
    per_symbol: int = 3,
) -> dict:
    """保有＋指数のニュース監視データを一括構築。

    返り値: {
        "symbols": [...], "holding_news": {...},
        "market_news": [...], "index_watch": [...],
        "available": bool  # 何かしら取れたか
    }
    """
    if symbols is None:
        symbols = get_portfolio_symbols()

    holding_news = gather_holding_news(symbols, per_symbol=per_symbol) if symbols else {}
    market_news = gather_market_news()
    index_watch = gather_index_watch(indices)

    available = bool(holding_news or market_news or index_watch)
    return {
        "symbols": symbols,
        "holding_news": holding_news,
        "market_news": market_news,
        "index_watch": index_watch,
        "available": available,
    }


def _fmt_pct(v: Any) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    sign = "+" if f >= 0 else ""
    return f"{sign}{f:.2f}%"


def format_news_watch(data: dict, max_symbols: int = 8) -> str:
    """news_watch データを Markdown に整形。空なら空文字列。"""
    if not data or not data.get("available"):
        return ""

    lines: list[str] = ["---", "## 📡 保有＋指数 ニュース監視"]

    # 指数
    idx = data.get("index_watch") or []
    if idx:
        parts = [f"{i['label']} {i['price']:.0f} ({_fmt_pct(i.get('percent_change'))})" for i in idx]
        lines.append("**指数:** " + " / ".join(parts))

    # 保有銘柄ニュース
    holding_news = data.get("holding_news") or {}
    if holding_news:
        lines.append("\n**保有銘柄の直近ニュース:**")
        for sym in list(holding_news.keys())[:max_symbols]:
            arts = holding_news[sym]
            if not arts:
                continue
            lines.append(f"- **{sym}**")
            for a in arts:
                src = a.get("source", "")
                head = a.get("headline", "")[:80]
                lines.append(f"    - {head} ({src})")

    # マーケット全体
    market = data.get("market_news") or []
    if market:
        lines.append("\n**マーケット全体:**")
        for a in market:
            lines.append(f"- {a.get('headline','')[:80]} ({a.get('source','')})")

    lines.append("")
    return "\n".join(lines)
