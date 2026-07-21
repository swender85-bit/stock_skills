"""週次ポートフォリオ分析レポートの組み立て.

保有 → 株価 → 銘柄別(業績+テクニカル+過熱判定) → PF集計 → 推移予測 → Markdown。

保有の取得優先順位:
  1. 楽天 MarketSpeed II RSS スナップショット（あれば最優先＝実口座の実データ）
  2. config/weekly_holdings.yaml（CLAUDE.md 基準スナップショットの写し）

株価は常に別途 API でも取得する。RSS が国内株しか返さない場合の補完と、
テクニカル指標に必要な時系列を得るため。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from src.core.portfolio.projection import (
    HORIZON_LABELS,
    drawdown_scenario,
    project_portfolio,
)
from src.core.technicals import analyze_prices


DEFAULT_HOLDINGS_CONFIG = "config/weekly_holdings.yaml"

#: **原資産**の年率リターン/ボラティリティ前提（予測レンジの入力）。
#:
#: レバレッジETFはここに原資産（SOXLなら半導体指数、TQQQならナスダック100）の
#: 前提を書き、倍率の効果は projection.project_value が導出する。
#: ETF自身のボラティリティを書くと二重計上になる。
#:
#: 予言ではなく「この前提を置いた」ことを明示するための表。
ASSET_ASSUMPTIONS: dict[str, dict] = {
    "米国グロース投信": {"annual_return_pct": 10.0, "annual_vol_pct": 22.0},
    "日本・食品": {"annual_return_pct": 5.0, "annual_vol_pct": 18.0},
    "日本・小売": {"annual_return_pct": 5.0, "annual_vol_pct": 22.0},
    "日本・半導体商社": {"annual_return_pct": 8.0, "annual_vol_pct": 30.0},
    "日本株": {"annual_return_pct": 6.0, "annual_vol_pct": 22.0},
    "_default": {"annual_return_pct": 6.0, "annual_vol_pct": 20.0},
}

#: レバレッジETFの原資産前提（銘柄別）。
#: 原資産のボラティリティが高いほど倍率の減衰が効くので、3xを一括りにできない。
#: SOXL(半導体指数 σ~35%) と TQQQ(NDX σ~22%) は期待値がまるで違う。
UNDERLYING_ASSUMPTIONS: dict[str, dict] = {
    "SOXL": {"annual_return_pct": 14.0, "annual_vol_pct": 35.0, "underlying": "半導体指数(SOX)"},
    "TECL": {"annual_return_pct": 13.0, "annual_vol_pct": 26.0, "underlying": "米テクノロジーセクター"},
    "TQQQ": {"annual_return_pct": 12.0, "annual_vol_pct": 22.0, "underlying": "ナスダック100"},
}


def load_holdings_config(path: str = DEFAULT_HOLDINGS_CONFIG) -> dict:
    """保有定義 YAML を読む。"""
    import yaml

    p = Path(path)
    if not p.exists():
        return {"holdings": [], "cash": [], "fx": {}}
    with p.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def resolve_holdings(
    config: dict, rss_snapshot: Optional[dict] = None
) -> tuple[list[dict], str]:
    """保有リストを決定する。RSS スナップショットがあればそちらを正とする。

    Returns
    -------
    (holdings, source)
        source は "rakuten-rss" か "config"。
    """
    if rss_snapshot and rss_snapshot.get("holdings"):
        resolved: list[dict] = []
        for h in rss_snapshot["holdings"]:
            resolved.append(
                {
                    "name": h.get("name") or h.get("quote_symbol", ""),
                    "quote_symbol": h.get("quote_symbol") or None,
                    "account": h.get("account", ""),
                    "shares": h.get("shares") or 0.0,
                    "cost_price": h.get("cost_price") or 0.0,
                    "currency": h.get("currency") or "JPY",
                    "category": _infer_category(h),
                    "unit_divisor": 1,
                    "rss_price": h.get("price"),
                    "price_source": h.get("price_source", "rakuten-rss"),
                }
            )
        return resolved, "rakuten-rss"

    return list(config.get("holdings") or []), "config"


def _infer_category(holding: dict) -> str:
    """RSS 明細からカテゴリを粗く推定する（前提テーブル参照用）。"""
    symbol = (holding.get("quote_symbol") or "").upper()
    if symbol in ("SOXL", "TECL", "TQQQ", "SPXL", "FNGU"):
        return "米国レバレッジETF"
    if symbol.endswith(".T"):
        return "日本株"
    return "_default"


def _assumption(category: str, symbol: Optional[str] = None) -> dict:
    """予測前提を引く。レバレッジETFは銘柄別の原資産前提を優先する。"""
    if symbol:
        specific = UNDERLYING_ASSUMPTIONS.get(symbol.upper())
        if specific:
            return dict(specific)
    return dict(ASSET_ASSUMPTIONS.get(category, ASSET_ASSUMPTIONS["_default"]))


def fetch_prices(symbols: list[str], client: Any = None) -> dict[str, dict]:
    """各銘柄の終値時系列とファンダメンタルを取得する。

    yahoo_client 経由のみ（直接 yfinance を呼ばない規約）。
    取得失敗は None を入れて続行する。
    """
    if client is None:
        from src.data import yahoo_client as client  # type: ignore

    out: dict[str, dict] = {}
    for symbol in symbols:
        if not symbol:
            continue
        entry: dict = {"symbol": symbol, "history": None, "detail": None, "error": None}
        try:
            entry["history"] = client.get_price_history(symbol, period="2y")
        except Exception as e:
            entry["error"] = f"価格履歴の取得に失敗: {e}"
        try:
            entry["detail"] = client.get_stock_detail(symbol)
        except Exception as e:
            entry["error"] = (entry["error"] or "") + f" / 詳細の取得に失敗: {e}"
        out[symbol] = entry
    return out


def _closes(history: Any) -> list[float]:
    """価格履歴 DataFrame から終値列を取り出す。"""
    if history is None:
        return []
    for col in ("Close", "close", "Adj Close"):
        if hasattr(history, "columns") and col in history.columns:
            return [float(x) for x in history[col].dropna().tolist()]
    return []


def week_close(history: Any, as_of: Optional[date] = None) -> Optional[dict]:
    """その週の最終終値と週間騰落率を取り出す。

    土曜朝に走らせる前提なので、直近の営業日終値＝その週の最終終値になる。
    """
    closes = _closes(history)
    if not closes:
        return None
    last = closes[-1]
    # 直近5営業日前との比較 = 週間騰落
    prev = closes[-6] if len(closes) >= 6 else closes[0]
    dates = []
    if hasattr(history, "index"):
        try:
            dates = [str(d)[:10] for d in history.index.tolist()]
        except Exception:
            dates = []
    return {
        "close": last,
        "prev_week_close": prev,
        "week_change_pct": ((last - prev) / prev * 100.0) if prev else None,
        "close_date": dates[-1] if dates else None,
    }


def _fundamentals(detail: Optional[dict]) -> dict:
    """業績・バリュエーション指標を抜き出す。無いものは None のまま残す。"""
    d = detail or {}

    def pick(*keys):
        for k in keys:
            if d.get(k) is not None:
                return d[k]
        return None

    return {
        "per": pick("per", "trailingPE"),
        "forward_per": pick("forwardPE"),
        "pbr": pick("pbr", "priceToBook"),
        "roe": pick("roe", "returnOnEquity"),
        "operating_margin": pick("operating_margin", "operatingMargins"),
        "profit_margin": pick("profit_margin", "profitMargins"),
        "revenue_growth": pick("revenue_growth", "revenueGrowth"),
        "earnings_growth": pick("earnings_growth", "earningsGrowth"),
        "dividend_yield": pick("dividend_yield", "dividendYield"),
        "market_cap": pick("market_cap", "marketCap"),
        "debt_to_equity": pick("debt_to_equity", "debtToEquity"),
        "free_cashflow": pick("free_cashflow", "freeCashflow"),
        "sector": pick("sector"),
        "name": pick("name", "longName", "shortName"),
        "is_etf": bool(pick("is_etf")) or (d.get("quoteType") == "ETF"),
        "expense_ratio": pick("expense_ratio", "annualReportExpenseRatio"),
    }


def analyze_holding(holding: dict, price_data: Optional[dict], fx_rate: float) -> dict:
    """1銘柄分の完全な分析を組み立てる。"""
    symbol = holding.get("quote_symbol")
    shares = float(holding.get("shares") or 0.0)
    divisor = float(holding.get("unit_divisor") or 1)
    cost = float(holding.get("cost_price") or 0.0)
    currency = holding.get("currency") or "JPY"

    history = (price_data or {}).get("history")
    detail = (price_data or {}).get("detail")
    closes = _closes(history)

    wk = week_close(history)
    price = None
    price_source = holding.get("price_source", "")

    if wk:
        price = wk["close"]
        price_source = price_source or "yfinance"
    if holding.get("rss_price"):
        price = float(holding["rss_price"])
        price_source = "rakuten-rss"
    if price is None:
        price = holding.get("last_known_price")
        if price is not None:
            price = float(price)
            price_source = "last_known(価格取得不可)"

    value_local = (price * shares / divisor) if price is not None else None
    cost_local = cost * shares / divisor
    value_jpy = None
    if value_local is not None:
        value_jpy = value_local * fx_rate if currency == "USD" else value_local
    cost_jpy = cost_local * fx_rate if currency == "USD" else cost_local

    pl_jpy = (value_jpy - cost_jpy) if value_jpy is not None else None
    pl_pct = ((price - cost) / cost * 100.0) if (price is not None and cost) else None

    return {
        "name": holding.get("name", ""),
        "symbol": symbol,
        "account": holding.get("account", ""),
        "category": holding.get("category", "_default"),
        "leverage": int(holding.get("leverage") or 1),
        "shares": shares,
        "unit_divisor": divisor,
        "cost_price": cost,
        "price": price,
        "price_source": price_source,
        "currency": currency,
        "value_local": value_local,
        "value_jpy": value_jpy,
        "cost_jpy": cost_jpy,
        "pl_jpy": pl_jpy,
        "pl_pct": pl_pct,
        "week": wk,
        "technicals": analyze_prices(closes) if closes else None,
        "fundamentals": _fundamentals(detail),
        "note": holding.get("note", ""),
        "error": (price_data or {}).get("error"),
    }


def build_report_data(
    config: dict,
    rss_snapshot: Optional[dict] = None,
    client: Any = None,
    fx_rate: Optional[float] = None,
    monthly_contribution: float = 50000.0,
) -> dict:
    """レポートに必要なデータを全て集める。"""
    holdings, source = resolve_holdings(config, rss_snapshot)

    symbols = [h.get("quote_symbol") for h in holdings if h.get("quote_symbol")]
    price_data = fetch_prices(list(dict.fromkeys(symbols)), client=client)

    if fx_rate is None:
        fx_rate = _resolve_fx(config, price_data, client)

    analyses = [
        analyze_holding(h, price_data.get(h.get("quote_symbol") or ""), fx_rate)
        for h in holdings
    ]

    cash_jpy = 0.0
    cash_items: list[dict] = []
    for c in config.get("cash") or []:
        amount = float(c.get("amount") or 0.0)
        jpy = amount * fx_rate if c.get("currency") == "USD" else amount
        cash_jpy += jpy
        cash_items.append({**c, "value_jpy": jpy})

    invested = sum(a["value_jpy"] for a in analyses if a["value_jpy"] is not None)
    total = invested + cash_jpy

    positions = []
    for a in analyses:
        if a["value_jpy"] is None:
            continue
        assumption = _assumption(a["category"], a.get("symbol"))
        positions.append(
            {
                "name": a["name"],
                "symbol": a.get("symbol"),
                "value": a["value_jpy"],
                "leverage": a["leverage"],
                **assumption,
            }
        )

    projection = project_portfolio(
        positions, cash_value=cash_jpy, monthly_contribution=monthly_contribution
    )
    scenarios = [
        drawdown_scenario(positions, drop, cash_jpy) for drop in (-10, -20, -35)
    ]

    # 表示用にドラッグを添えておく（計算には使わない。project_value が導出済み）
    from src.core.portfolio.projection import volatility_drag

    for p in positions:
        p["drag_pct"] = volatility_drag(
            float(p.get("annual_vol_pct") or 0.0), int(p.get("leverage") or 1)
        )

    return {
        "generated_at": datetime.now(timezone.utc),
        "holdings_source": source,
        "positions": positions,
        "rss_snapshot": rss_snapshot,
        "fx_rate": fx_rate,
        "analyses": analyses,
        "cash_items": cash_items,
        "cash_jpy": cash_jpy,
        "invested_jpy": invested,
        "total_jpy": total,
        "projection": projection,
        "scenarios": scenarios,
        "monthly_contribution": monthly_contribution,
    }


def _resolve_fx(config: dict, price_data: dict, client: Any) -> float:
    """USD/JPY を取得する。失敗時は config の fallback。"""
    fx_conf = config.get("fx") or {}
    symbol = fx_conf.get("usdjpy_symbol") or "JPY=X"
    fallback = float(fx_conf.get("fallback_rate") or 150.0)

    if client is None:
        try:
            from src.data import yahoo_client as client  # type: ignore
        except Exception:
            return fallback
    try:
        hist = client.get_price_history(symbol, period="1mo")
        closes = _closes(hist)
        if closes:
            return closes[-1]
    except Exception:
        pass
    return fallback
