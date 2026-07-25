"""ブリーフィングパック生成 — Claude の深掘り synthesis に渡す「全材料」を1つに束ねる層。

## 位置づけ

週次レポートも個別質問（"この株どう？"）も、根本の欠陥は同じだった:
**材料集め（Python・トークン0）はできているのに、解釈・統合（Claude/LLMの仕事）が
無い。** このモジュールは前者を極限まで厚くし、後者に渡す構造化パックを作る。

パックには「数字」だけでなく **前回からの差分・今後の日程・競合の動き・過去の
テーゼ/懸念/lesson** まで含める。Claude はこれを読んで「銘柄ごとに何が変わり、
PF上どういう立ち位置で、今後の日程がどう効き、どう動くべきか」を書ける。

## 2モード

- ``build_portfolio_briefing()``  … 保有全体（週次レポート用）
- ``build_symbol_briefing(symbol)`` … 単一銘柄＋その競合＋セクター指数（個別質問用）

## graceful degradation

各データ源は独立に try/except で守り、失敗したら該当キーを空にして続行する。
Neo4j/moomoo/Finnhub/ネットワークがどれか落ちてもパックは必ず返る。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional

from src.core.portfolio.weekly import (
    build_report_data,
    load_holdings_config,
)

# パックのスキーマ版。executor 側の互換チェック用。
PACK_VERSION = 1


# ---------------------------------------------------------------------------
# 前回差分（data/history/report のスナップショットから）
# ---------------------------------------------------------------------------

#: 前回差分を計算する数値フィールド
_DELTA_FIELDS = (
    "price", "per", "pbr", "roe", "revenue_growth", "value_score",
    "dividend_yield", "market_cap",
)


def _norm_sym(sym: Any) -> str:
    return str(sym or "").strip().upper()


def _prior_report_index(days_back: int = 180) -> dict[str, list[dict]]:
    """history/report をシンボル別・日付降順に索引化する。"""
    try:
        from src.data.history.load import load_history

        snaps = load_history("report", days_back=days_back)
    except Exception:
        return {}
    index: dict[str, list[dict]] = {}
    for s in snaps:
        key = _norm_sym(s.get("symbol"))
        if key:
            index.setdefault(key, []).append(s)
    # load_history は既に新しい順。各リストの先頭が最新。
    return index


def week_over_week_delta(symbol: str, current: dict,
                         prior_index: dict[str, list[dict]],
                         today: Optional[str] = None) -> Optional[dict]:
    """1銘柄の「前回スナップショットからの変化」を返す。無ければ None。

    current は fundamentals + price を含む dict。
    """
    key = _norm_sym(symbol)
    snaps = prior_index.get(key) or []
    today = today or date.today().isoformat()
    prior = next((s for s in snaps if str(s.get("date", "")) < today), None)
    if not prior:
        return None

    delta: dict[str, Any] = {}
    for f in _DELTA_FIELDS:
        now_v = current.get(f)
        old_v = prior.get(f)
        if isinstance(now_v, (int, float)) and isinstance(old_v, (int, float)):
            diff = now_v - old_v
            pct = (diff / old_v * 100.0) if old_v else None
            delta[f] = {"prior": old_v, "now": now_v, "diff": diff, "pct": pct}
    return {
        "prior_date": prior.get("date"),
        "prior_verdict": prior.get("verdict"),
        "now_verdict": current.get("verdict"),
        "fields": delta,
    }


# ---------------------------------------------------------------------------
# 各データ源の安全ラッパ
# ---------------------------------------------------------------------------


def _safe_news_watch(symbols: list[str]) -> dict:
    try:
        from src.core.research.portfolio_news import build_news_watch

        return build_news_watch(symbols=symbols) or {}
    except Exception:
        return {}


def _safe_moomoo(symbols: list[str]) -> dict:
    """moomoo(OpenD) インサイト。opt-in（MOOMOO_ENABLED=on）かつ起動時のみ。"""
    try:
        from src.data import moomoo_client
        from src.core.research import moomoo_insights
    except Exception:
        return {}
    if not symbols:
        return {}
    try:
        with moomoo_client.ensure_opend() as up:
            if not up:
                return {}
            return moomoo_insights.collect_weekly_insights(symbols) or {}
    except Exception:
        return {}


def _safe_context(query: str) -> str:
    """Neo4j/グラフからの過去テーゼ・懸念・lesson・リサーチ履歴（markdown）。"""
    try:
        from src.data.context.auto_context import get_context

        result = get_context(query)
        if result and result.get("context_markdown"):
            return result["context_markdown"]
    except Exception:
        pass
    return ""


def _safe_competitors(symbols: list[str]) -> dict:
    try:
        from src.core.research.competitors import build_peer_context

        return build_peer_context(symbols) or {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# テクニカルの要約（パックを読みやすく保つため主要指標だけ抜く）
# ---------------------------------------------------------------------------


def _compact_technicals(t: Optional[dict]) -> Optional[dict]:
    if not t:
        return None
    heat = t.get("heat") or {}
    bb = t.get("bollinger") or {}
    rng = t.get("range_52w") or {}
    return {
        "last": t.get("last"),
        "sma50": t.get("sma50"),
        "sma200": t.get("sma200"),
        "sma200_deviation_pct": t.get("sma200_deviation_pct"),
        "rsi14": t.get("rsi14"),
        "percent_b": bb.get("percent_b"),
        "range_52w_position": rng.get("position"),
        "from_high_pct": rng.get("from_high_pct"),
        "volatility_pct": t.get("volatility_pct"),
        "trend": t.get("trend"),
        "heat_state": heat.get("state"),
        "heat_label": heat.get("label"),
        "heat_signals": heat.get("signals"),
    }


def _holding_view(a: dict, prior_index: dict, competitors: dict,
                  total_jpy: float, today: str) -> dict:
    f = a.get("fundamentals") or {}
    # 差分計算に渡す current（fundamentals + price + verdict）
    current = dict(f)
    current["price"] = a.get("price")
    wow = week_over_week_delta(a.get("symbol") or "", current, prior_index, today)
    weight = (a["value_jpy"] / total_jpy * 100.0) if (a.get("value_jpy") and total_jpy) else None
    return {
        "name": a.get("name"),
        "symbol": a.get("symbol"),
        "account": a.get("account"),
        "shares": a.get("shares"),
        "cost_price": a.get("cost_price"),
        "price": a.get("price"),
        "price_source": a.get("price_source"),
        "currency": a.get("currency"),
        "value_jpy": a.get("value_jpy"),
        "pl_jpy": a.get("pl_jpy"),
        "pl_pct": a.get("pl_pct"),
        "weight_pct": weight,
        "week_change_pct": (a.get("week") or {}).get("week_change_pct"),
        "leverage": a.get("leverage"),
        "fundamentals": f,
        "technicals": _compact_technicals(a.get("technicals")),
        "wow_delta": wow,
        "competitors": competitors.get(a.get("symbol") or ""),
        "note": a.get("note"),
        "error": a.get("error"),
    }


# ---------------------------------------------------------------------------
# 今後の日程（moomoo の経済/決算/配当カレンダーを1本の時系列に）
# ---------------------------------------------------------------------------


def _forward_schedule(moomoo: dict) -> list[dict]:
    events: list[dict] = []
    for e in moomoo.get("economic_events") or []:
        events.append({
            "kind": "economic", "title": e.get("title"),
            "country": e.get("country"), "importance": e.get("star"),
            "consensus": e.get("consensus"), "previous": e.get("previous"),
        })
    for e in moomoo.get("earnings") or []:
        events.append({
            "kind": "earnings", "symbol": e.get("security"),
            "date": e.get("date"), "title": f"{e.get('security')} 決算",
            "period": e.get("period"), "eps_predict": e.get("eps_predict"),
        })
    for d in moomoo.get("dividends") or []:
        events.append({
            "kind": "dividend", "symbol": d.get("security"),
            "date": d.get("ex_date"), "title": f"{d.get('security')} 配当除権",
            "statement": d.get("statement"),
        })
    fw = moomoo.get("fed_watch")
    if fw:
        events.append({
            "kind": "fomc", "date": fw.get("next_meeting"),
            "title": "FOMC", "top_range": fw.get("top_range"),
            "top_prob": fw.get("top_prob"),
        })
    return events


# ---------------------------------------------------------------------------
# パック本体
# ---------------------------------------------------------------------------


def _portfolio_summary(base: dict) -> dict:
    analyses = base.get("analyses") or []
    total_pl = sum(a["pl_jpy"] for a in analyses if a.get("pl_jpy") is not None)
    total_cost = sum(a["cost_jpy"] for a in analyses if a.get("cost_jpy") is not None)
    return {
        "total_jpy": base.get("total_jpy"),
        "invested_jpy": base.get("invested_jpy"),
        "cash_jpy": base.get("cash_jpy"),
        "fx_rate": base.get("fx_rate"),
        "total_pl_jpy": total_pl,
        "pl_pct": (total_pl / total_cost * 100.0) if total_cost else None,
    }


def build_portfolio_briefing(
    config: Optional[dict] = None,
    rss_snapshot: Optional[dict] = None,
    monthly_contribution: float = 50000.0,
    include_moomoo: bool = True,
    include_context: bool = True,
) -> dict:
    """保有全体のブリーフィングパックを組み立てる（週次レポート用）。"""
    if config is None:
        config = load_holdings_config()

    base = build_report_data(
        config, rss_snapshot=rss_snapshot, monthly_contribution=monthly_contribution
    )
    analyses = base.get("analyses") or []
    symbols = [a.get("symbol") for a in analyses if a.get("symbol")]
    total_jpy = base.get("total_jpy") or 0.0
    today = date.today().isoformat()

    prior_index = _prior_report_index()
    competitors = _safe_competitors(symbols)
    news = _safe_news_watch(symbols)
    moomoo = _safe_moomoo(symbols) if include_moomoo else {}
    prior_context = _safe_context("週次ポートフォリオ分析 PF全体の状況") if include_context else ""

    holdings = [
        _holding_view(a, prior_index, competitors, total_jpy, today)
        for a in analyses
    ]

    warnings: list[str] = []
    if not news.get("available"):
        warnings.append("ニュース/指数が取得できませんでした（材料なしではなく取得不可）。")
    if not moomoo:
        warnings.append("moomoo インサイトは無効/未起動（決算・配当・アナリスト日程は限定的）。")

    return {
        "pack_version": PACK_VERSION,
        "mode": "portfolio",
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "as_of": today,
            "holdings_source": base.get("holdings_source"),
            "holdings_import": base.get("holdings_import"),
            "fx_rate": base.get("fx_rate"),
            "warnings": warnings,
        },
        "portfolio": _portfolio_summary(base),
        "holdings": holdings,
        "indices": news.get("index_watch") or [],
        "holding_news": news.get("holding_news") or {},
        "market_news": news.get("market_news") or [],
        "moomoo": moomoo,
        "forward_schedule": _forward_schedule(moomoo),
        "projection": base.get("projection"),
        "scenarios": base.get("scenarios"),
        "positions_assumptions": base.get("positions"),
        "monthly_contribution": monthly_contribution,
        "prior_context": prior_context,
    }


def build_symbol_briefing(
    symbol: str,
    include_moomoo: bool = True,
    include_context: bool = True,
) -> dict:
    """単一銘柄＋競合＋指数のブリーフィングパック（個別質問「常に全力」用）。

    保有していれば保有情報も添える。していなくても分析できる。
    """
    from src.data import yahoo_client as yc

    symbol = symbol.strip()
    today = date.today().isoformat()

    info = None
    detail = None
    technicals = None
    try:
        info = yc.get_stock_info(symbol)
    except Exception:
        info = None
    try:
        detail = yc.get_stock_detail(symbol)
    except Exception:
        detail = None
    try:
        hist = yc.get_price_history(symbol, period="2y")
        from src.core.technicals import analyze_prices

        closes = [float(x) for x in hist["Close"].dropna().tolist()] if hist is not None else []
        technicals = analyze_prices(closes) if closes else None
    except Exception:
        technicals = None

    prior_index = _prior_report_index()
    current = dict(detail or info or {})
    current["price"] = (info or {}).get("price")
    wow = week_over_week_delta(symbol, current, prior_index, today)

    competitors = _safe_competitors([symbol]).get(symbol)
    news = _safe_news_watch([symbol])
    moomoo = _safe_moomoo([symbol]) if include_moomoo else {}
    prior_context = _safe_context(f"{symbol} の分析") if include_context else ""

    # 保有チェック
    held = None
    try:
        from src.core.research.portfolio_news import get_portfolio_symbols

        if _norm_sym(symbol) in {_norm_sym(s) for s in get_portfolio_symbols()}:
            held = True
    except Exception:
        held = None

    return {
        "pack_version": PACK_VERSION,
        "mode": "symbol",
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "as_of": today,
            "symbol": symbol,
            "is_held": held,
        },
        "info": info,
        "fundamentals": {k: (detail or {}).get(k) for k in (
            "per", "forward_per", "pbr", "roe", "roa", "profit_margin",
            "operating_margin", "revenue_growth", "earnings_growth",
            "dividend_yield", "debt_to_equity", "market_cap", "beta",
            "sector", "industry", "name",
        )} if detail else (info or {}),
        "technicals": _compact_technicals(technicals),
        "wow_delta": wow,
        "competitors": competitors,
        "indices": news.get("index_watch") or [],
        "news": (news.get("holding_news") or {}).get(symbol) or [],
        "market_news": news.get("market_news") or [],
        "moomoo": moomoo,
        "forward_schedule": _forward_schedule(moomoo),
        "prior_context": prior_context,
    }
