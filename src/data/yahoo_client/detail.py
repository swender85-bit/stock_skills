"""Stock info and detail fetching (KIK-449, KIK-531)."""

import socket
import time
from typing import Any, Optional

import pandas as pd
import yfinance as yf

from src.data.yahoo_client._cache import (
    _read_cache,
    _write_cache,
    _read_detail_cache,
    _write_detail_cache,
)
from src.data.yahoo_client._memory_cache import stock_detail_cache
from src.data.yahoo_client._normalize import (
    _normalize_ratio,
    _safe_get,
    _sanitize_anomalies,
)


def _try_get_field(df: Any, field_names: list[str]) -> Optional[float]:
    """Try to extract a numeric value from a DataFrame row using multiple
    possible field names.  Returns None if the DataFrame is empty or none of
    the names exist.
    """
    try:
        if df is None or df.empty:
            return None
        for name in field_names:
            if name in df.index:
                value = df.loc[name].iloc[0]
                # Convert numpy / pandas types to plain Python float
                if value is not None and value == value:  # NaN check
                    return float(value)
        return None
    except Exception:
        return None


def _try_get_history(df, field_names: list[str], max_periods: int = 4) -> list[float]:
    """Try to extract multiple periods of data from a DataFrame row.

    Returns a list of floats in latest-to-oldest order.  Stops at the first
    NaN / None so that only contiguous data is returned.
    """
    try:
        if df is None or df.empty:
            return []
        for name in field_names:
            if name in df.index:
                values: list[float] = []
                row = df.loc[name]
                for i in range(min(len(row), max_periods)):
                    val = row.iloc[i]
                    if val is not None and val == val:  # NaN check
                        values.append(float(val))
                    else:
                        break  # contiguous data required
                if values:
                    return values
        return []
    except Exception:
        return []


def _build_dividend_history_from_actions(
    ticker, shares_outstanding, max_years: int = 4
) -> tuple:
    """Build dividend history from ticker.dividends as a fallback (KIK-388).

    When cashflow does not contain dividend payment history, use per-share
    dividend actions grouped by calendar year and multiplied by
    shares_outstanding to estimate total amounts.

    Returns
    -------
    tuple[list[float], list[int]]
        (dividend_amounts, fiscal_years) both in latest-first order.
        Amounts are negative (cash outflow convention matching cashflow).
        Returns ([], []) if data is insufficient.
    """
    try:
        if shares_outstanding is None or shares_outstanding <= 0:
            return [], []

        divs = ticker.dividends
        if divs is None or len(divs) == 0:
            return [], []

        # Group by calendar year, sum per-share dividends
        yearly = divs.groupby(divs.index.year).sum()
        if len(yearly) == 0:
            return [], []

        # Take most recent max_years, sorted latest-first
        years_sorted = sorted(yearly.index, reverse=True)[:max_years]

        amounts: list = []
        fiscal_years: list = []
        for year in years_sorted:
            per_share_total = float(yearly.loc[year])
            if per_share_total > 0:
                # Negative convention (cash outflow) to match cashflow format
                amounts.append(-(per_share_total * shares_outstanding))
                fiscal_years.append(int(year))

        return amounts, fiscal_years
    except Exception:
        return [], []


#: 直近の取得失敗理由。**「データが無い」と「取れなかった」を区別するため。**
#: 2026-08-08 の週次は price=None / error=None という状態で出力され、
#: レポートが「取得できず」としか書けなかった。理由を残せば原因まで書ける。
_FETCH_ERRORS: dict[str, str] = {}


def _record_fetch_error(symbol: str, reason: str) -> None:
    _FETCH_ERRORS[symbol] = reason


def last_fetch_error(symbol: str) -> Optional[str]:
    """その銘柄の直近の取得失敗理由。成功していれば None。"""
    return _FETCH_ERRORS.get(symbol)


def clear_fetch_errors() -> None:
    _FETCH_ERRORS.clear()


def _resolve_minimal(symbol: str) -> Optional[dict]:
    """`info` が取れなかったときの最後の手段。

    価格だけでも取れれば、レポートは「値動きが不明」ではなく
    「値は取れたが指標は取れなかった」と書ける。**その差は大きい。**

    どの経路で取れたかを `price_source` / `resolution` に残し、
    **予備で取った値を一次経路の値のように見せない。**
    """
    try:
        from src.data.resolver import resolve_history, resolve_price
    except Exception:
        return None

    price = resolve_price(symbol)
    if not price.get("available"):
        _record_fetch_error(
            symbol,
            f"全経路で価格を取得できませんでした（試行: "
            f"{', '.join(a['source'] for a in price.get('attempts') or [])}）")
        return None

    result: dict = {
        "symbol": symbol,
        "price": price["value"],
        "price_source": price["source"],
        "resolution": {
            "price": {"source": price["source"],
                      "fallback_used": price.get("fallback_used"),
                      "attempts": price.get("attempts")},
        },
        "degraded": True,
        "degraded_note": (
            f"基本情報が取得できず、価格のみ {price['source']} から復旧しました。"
            "PER・成長率などの指標は**取得できていません**（0ではありません）。"),
    }

    # 52週レンジくらいは系列から作れる。指標が全滅した週でも過熱は測れる。
    hist = resolve_history(symbol, period="1y")
    if hist.get("available"):
        try:
            closes = hist["value"]["Close"].dropna()
            result["fifty_two_week_high"] = float(closes.max())
            result["fifty_two_week_low"] = float(closes.min())
            result["resolution"]["history"] = {"source": hist["source"]}
        except Exception:
            pass

    _sanitize_anomalies(result)
    return result


def get_stock_info(symbol: str) -> Optional[dict]:
    """Fetch basic stock information for a single symbol.

    Returns a dict with standardized keys, or None if the fetch fails entirely.
    Individual fields that are unavailable are set to None.

    一時失敗はリトライする（`_net.with_retry`）。ネットワークが落ちている間の
    1回の失敗を「今週は取得不能」として確定させないため。
    """
    # Check cache first
    cached = _read_cache(symbol)
    if cached is not None:
        return cached

    try:
        ticker = yf.Ticker(symbol)

        # 一時失敗を確定にしない (2026-08-08 の週次全滅の再発防止)。
        # yfinance は通信が落ちていると**例外ではなく空 dict** を返すことがあるので、
        # 例外だけでなく「価格が無い」ことも再試行の対象にする。
        from src.data.yahoo_client._net import with_retry

        info, fetch_error = with_retry(
            lambda: ticker.info,
            label=f"{symbol} の基本情報",
            is_empty=lambda d: not d or d.get("regularMarketPrice") is None,
        )

        if not info or info.get("regularMarketPrice") is None:
            # ⚠️ **ここで諦めない。** 運用ルール:
            #   「取得できなかったら他のあらゆる手段を講じて取得できるまでトライする」
            # `info` が落ちても fast_info / download / finnhub が通ることがある。
            # 実測で確認済みの経路を順に試し、**全滅して初めて** None を返す。
            resolved = _resolve_minimal(symbol)
            if resolved is not None:
                return resolved
            _record_fetch_error(symbol, fetch_error or "価格が取得できませんでした")
            return None

        result = {
            "symbol": symbol,
            "name": _safe_get(info, "shortName") or _safe_get(info, "longName"),
            "sector": _safe_get(info, "sector"),
            "industry": _safe_get(info, "industry"),
            "currency": _safe_get(info, "currency"),
            # Price
            "price": _safe_get(info, "regularMarketPrice"),
            "market_cap": _safe_get(info, "marketCap"),
            # Valuation
            "per": _safe_get(info, "trailingPE"),
            "forward_per": _safe_get(info, "forwardPE"),
            "pbr": _safe_get(info, "priceToBook"),
            "psr": _safe_get(info, "priceToSalesTrailing12Months"),
            # Profitability
            "roe": _safe_get(info, "returnOnEquity"),
            "roa": _safe_get(info, "returnOnAssets"),
            "profit_margin": _safe_get(info, "profitMargins"),
            "operating_margin": _safe_get(info, "operatingMargins"),
            # Dividend (yfinance returns percentage, e.g. 2.52 for 2.52%)
            "dividend_yield": _normalize_ratio(_safe_get(info, "dividendYield")),
            # Trailing dividend yield (already a ratio from yfinance, e.g. 0.025 = 2.5%)
            "dividend_yield_trailing": _safe_get(info, "trailingAnnualDividendYield"),
            "payout_ratio": _safe_get(info, "payoutRatio"),
            # Growth
            "revenue_growth": _safe_get(info, "revenueGrowth"),
            "earnings_growth": _safe_get(info, "earningsGrowth"),
            # Financial health
            "debt_to_equity": _safe_get(info, "debtToEquity"),
            "current_ratio": _safe_get(info, "currentRatio"),
            "free_cashflow": _safe_get(info, "freeCashflow"),
            # Other
            "beta": _safe_get(info, "beta"),
            "fifty_two_week_high": _safe_get(info, "fiftyTwoWeekHigh"),
            "fifty_two_week_low": _safe_get(info, "fiftyTwoWeekLow"),
            # Quote type (KIK-469)
            "quoteType": _safe_get(info, "quoteType"),
        }

        _sanitize_anomalies(result)
        # 日本株は earningsGrowth が欠けることが多い。損益計算書から導出して埋める。
        # 埋めなかった場合、「増収が利益に落ちているか不明」という留保付きの分析に
        # なるが、原資料には答えが載っている（味の素は純利益 +91.6% だった）。
        try:
            from src.data.yahoo_client.financials import fill_missing_growth

            fill_missing_growth(result, ticker=ticker)
        except Exception:
            pass
        _write_cache(symbol, result)
        return result

    except (TimeoutError, socket.timeout) as e:
        print(
            f"⚠️  Yahoo Financeへの接続がタイムアウトしました ({symbol})\n"
            "    原因: ネットワーク接続が不安定、またはYahoo Financeが一時的に応答していません\n"
            "    対処: ネットワーク接続を確認し、再試行してください"
        )
        return None
    except Exception as e:
        if "timed out" in str(e).lower() or "timeout" in str(e).lower():
            print(
                f"⚠️  Yahoo Financeへの接続がタイムアウトしました ({symbol})\n"
                "    原因: ネットワーク接続が不安定、またはYahoo Financeが一時的に応答していません\n"
                "    対処: ネットワーク接続を確認し、再試行してください"
            )
        else:
            print(f"[yahoo_client] Error fetching {symbol}: {e}")
        return None


def get_multiple_stocks(symbols: list[str]) -> dict[str, Optional[dict]]:
    """Fetch stock info for multiple symbols with a 1-second delay between requests.

    Returns a dict mapping symbol -> stock info (or None on failure).
    """
    results: dict[str, Optional[dict]] = {}
    for i, symbol in enumerate(symbols):
        results[symbol] = get_stock_info(symbol)
        # Wait 1 second between requests (skip after the last one)
        if i < len(symbols) - 1:
            time.sleep(1)
    return results


# ---------------------------------------------------------------------------
# get_stock_detail
# ---------------------------------------------------------------------------

def get_stock_detail(symbol: str) -> Optional[dict]:
    """Fetch detailed stock information including financial statements.

    Extends the base data from ``get_stock_info`` with price history,
    balance-sheet ratios, cash-flow, EPS growth, and debt/EBITDA figures.

    Returns a merged dict or None if the base data cannot be fetched.
    Uses memory → file → API three-tier cache (KIK-531).
    """
    # 0. Check in-memory cache first (KIK-531)
    mem_cached = stock_detail_cache.get(symbol)
    if mem_cached is not None:
        return mem_cached

    # 1. Get base data first
    base = get_stock_info(symbol)
    if base is None:
        return None

    # 2. Check file-based detail cache
    cached = _read_detail_cache(symbol)
    if cached is not None:
        stock_detail_cache.set(symbol, cached)
        return cached

    # 3. Fetch additional data from yfinance
    try:
        time.sleep(1)  # rate-limit consistent with existing pattern
        ticker = yf.Ticker(symbol)

        # --- Price history (2 years for ~24 monthly returns) ---
        price_history: Optional[list[float]] = None
        try:
            hist = ticker.history(period="2y")
            if hist is not None and not hist.empty and "Close" in hist.columns:
                price_history = [float(v) for v in hist["Close"].tolist()]
        except Exception:
            pass

        # --- Balance sheet: equity ratio, total_assets, equity_history ---
        equity_ratio: Optional[float] = None
        total_assets: Optional[float] = None
        equity_history: list[float] = []
        try:
            bs = ticker.balance_sheet
            if bs is not None and not bs.empty:
                col = bs.iloc[:, 0]  # most recent column
                equity = _try_get_field(bs, [
                    "Stockholders Equity",
                    "Total Stockholder Equity",
                    "Stockholders' Equity",
                    "StockholdersEquity",
                    "Total Equity Gross Minority Interest",
                ])
                total_assets = _try_get_field(bs, [
                    "Total Assets",
                    "TotalAssets",
                ])
                if equity is not None and total_assets is not None and total_assets != 0:
                    equity_ratio = float(equity / total_assets)

                # Multi-period equity history for ROE trend analysis
                equity_history = _try_get_history(bs, [
                    "Stockholders Equity",
                    "Total Stockholder Equity",
                    "Stockholders' Equity",
                    "StockholdersEquity",
                    "Total Equity Gross Minority Interest",
                ])
        except Exception:
            pass

        # --- Cash flow ---
        operating_cashflow: Optional[float] = None
        fcf: Optional[float] = None
        dividend_paid: Optional[float] = None
        stock_repurchase: Optional[float] = None
        try:
            cf = ticker.cashflow
            operating_cashflow = _try_get_field(cf, [
                "Operating Cash Flow",
                "Total Cash From Operating Activities",
                "Cash Flow From Continuing Operating Activities",
            ])
            fcf = _try_get_field(cf, [
                "Free Cash Flow",
                "FreeCashFlow",
            ])
            # KIK-375: Shareholder return data
            dividend_paid = _try_get_field(cf, [
                "Common Stock Dividend Paid",
                "Cash Dividends Paid",
                "Payment Of Dividends",
            ])
            stock_repurchase = _try_get_field(cf, [
                "Repurchase Of Capital Stock",
                "Common Stock Payments",
            ])
            if stock_repurchase is None:
                net_issuance = _try_get_field(cf, [
                    "Net Common Stock Issuance",
                ])
                if net_issuance is not None and net_issuance < 0:
                    stock_repurchase = net_issuance

            # KIK-380: Shareholder return 3-year history
            dividend_paid_history: list[float] = []
            stock_repurchase_history: list[float] = []
            cashflow_fiscal_years: list[int] = []
            div_field_names = [
                "Common Stock Dividend Paid",
                "Cash Dividends Paid",
                "Payment Of Dividends",
            ]
            rep_field_names = [
                "Repurchase Of Capital Stock",
                "Common Stock Payments",
            ]
            dividend_paid_history = _try_get_history(cf, div_field_names)
            stock_repurchase_history = _try_get_history(cf, rep_field_names)
            # Fallback: Net Common Stock Issuance (negative = repurchase)
            if not stock_repurchase_history:
                net_iss_hist = _try_get_history(cf, ["Net Common Stock Issuance"])
                stock_repurchase_history = [v for v in net_iss_hist if v < 0]
            # Extract fiscal year labels from cashflow column dates
            try:
                if cf is not None and not cf.empty:
                    for i in range(min(len(cf.columns), 4)):
                        col = cf.columns[i]
                        if hasattr(col, "year"):
                            cashflow_fiscal_years.append(int(col.year))
            except Exception:
                pass
        except Exception:
            pass

        # KIK-388: Fallback to ticker.dividends when cashflow dividend history is sparse
        if len(dividend_paid_history) < 2:
            shares_out = _safe_get(ticker.info, "sharesOutstanding")
            fb_amounts, fb_years = _build_dividend_history_from_actions(
                ticker, shares_out
            )
            if len(fb_amounts) >= 2:
                dividend_paid_history = fb_amounts
                if not cashflow_fiscal_years:
                    cashflow_fiscal_years = fb_years

        # --- Income statement: EPS, net income, revenue/NI history ---
        eps_current: Optional[float] = None
        eps_previous: Optional[float] = None
        eps_growth: Optional[float] = None
        net_income_stmt: Optional[float] = None
        revenue_history: list[float] = []
        net_income_history: list[float] = []
        try:
            inc = ticker.income_stmt
            if inc is not None and not inc.empty:
                # Net income from most recent period
                net_income_stmt = _try_get_field(inc, [
                    "Net Income",
                    "NetIncome",
                    "Net Income Common Stockholders",
                ])

                # Multi-period revenue history for acceleration analysis
                revenue_history = _try_get_history(inc, [
                    "Total Revenue",
                    "Revenue",
                ])

                # Multi-period net income history for ROE trend analysis
                net_income_history = _try_get_history(inc, [
                    "Net Income",
                    "NetIncome",
                    "Net Income Common Stockholders",
                ])

                # Diluted EPS – latest two years for growth calculation
                eps_field_name = None
                for candidate in ["Diluted EPS", "DilutedEPS"]:
                    if candidate in inc.index:
                        eps_field_name = candidate
                        break

                if eps_field_name is not None:
                    eps_row = inc.loc[eps_field_name]
                    if len(eps_row) >= 1:
                        val = eps_row.iloc[0]
                        if val is not None and val == val:
                            eps_current = float(val)
                    if len(eps_row) >= 2:
                        val = eps_row.iloc[1]
                        if val is not None and val == val:
                            eps_previous = float(val)
                    if (
                        eps_current is not None
                        and eps_previous is not None
                        and eps_previous != 0
                    ):
                        eps_growth = float(
                            (eps_current - eps_previous) / abs(eps_previous)
                        )
        except Exception:
            pass

        # --- Additional info fields ---
        total_debt: Optional[float] = None
        ebitda: Optional[float] = None
        target_high_price: Optional[float] = None
        target_low_price: Optional[float] = None
        target_mean_price: Optional[float] = None
        number_of_analyst_opinions: Optional[int] = None
        recommendation_mean: Optional[float] = None
        forward_eps: Optional[float] = None
        try:
            info = ticker.info
            total_debt = _safe_get(info, "totalDebt")
            ebitda = _safe_get(info, "ebitda")
            target_high_price = _safe_get(info, "targetHighPrice")
            target_low_price = _safe_get(info, "targetLowPrice")
            target_mean_price = _safe_get(info, "targetMeanPrice")
            number_of_analyst_opinions_val = _safe_get(info, "numberOfAnalystOpinions")
            number_of_analyst_opinions = int(number_of_analyst_opinions_val) if number_of_analyst_opinions_val is not None else None
            recommendation_mean = _safe_get(info, "recommendationMean")
            forward_eps = _safe_get(info, "forwardEps")
            # ETF-specific fields (KIK-469)
            expense_ratio: Optional[float] = _safe_get(info, "annualReportExpenseRatio")
            total_assets_fund: Optional[float] = _safe_get(info, "totalAssets")  # AUM
            fund_category: Optional[str] = _safe_get(info, "category")
            fund_family: Optional[str] = _safe_get(info, "fundFamily")
            quote_type: Optional[str] = _safe_get(info, "quoteType")
        except Exception:
            pass

        # 4. Merge into base dict
        result = dict(base)  # shallow copy to avoid mutating cached base
        result.update({
            "price_history": price_history,
            "equity_ratio": equity_ratio,
            "operating_cashflow": operating_cashflow,
            "net_income_stmt": net_income_stmt,
            "fcf": fcf,
            "total_debt": total_debt,
            "ebitda": ebitda,
            # Analyst fields (KIK-359)
            "target_high_price": target_high_price,
            "target_low_price": target_low_price,
            "target_mean_price": target_mean_price,
            "number_of_analyst_opinions": number_of_analyst_opinions,
            "recommendation_mean": recommendation_mean,
            "forward_eps": forward_eps,
            "eps_current": eps_current,
            "eps_previous": eps_previous,
            "eps_growth": eps_growth,
            # Alpha signal fields (KIK-346)
            "total_assets": total_assets,
            "revenue_history": revenue_history,
            "net_income_history": net_income_history,
            # Shareholder return fields (KIK-375)
            "dividend_paid": dividend_paid,
            "stock_repurchase": stock_repurchase,
            "equity_history": equity_history,
            # Shareholder return history (KIK-380)
            "dividend_paid_history": dividend_paid_history,
            "stock_repurchase_history": stock_repurchase_history,
            "cashflow_fiscal_years": cashflow_fiscal_years,
            # ETF fields (KIK-469)
            "quoteType": quote_type,
            "expense_ratio": expense_ratio,
            "total_assets_fund": total_assets_fund,
            "fund_category": fund_category,
            "fund_family": fund_family,
        })

        # 5. Cache the result (file + memory)
        _write_detail_cache(symbol, result)
        stock_detail_cache.set(symbol, result)
        return result

    except (TimeoutError, socket.timeout) as e:
        print(
            f"⚠️  Yahoo Financeへの接続がタイムアウトしました ({symbol})\n"
            "    原因: ネットワーク接続が不安定、またはYahoo Financeが一時的に応答していません\n"
            "    対処: ネットワーク接続を確認し、再試行してください"
        )
        return None
    except Exception as e:
        if "timed out" in str(e).lower() or "timeout" in str(e).lower():
            print(
                f"⚠️  Yahoo Financeへの接続がタイムアウトしました ({symbol})\n"
                "    原因: ネットワーク接続が不安定、またはYahoo Financeが一時的に応答していません\n"
                "    対処: ネットワーク接続を確認し、再試行してください"
            )
        else:
            print(f"[yahoo_client] Error fetching detail for {symbol}: {e}")
        return None
