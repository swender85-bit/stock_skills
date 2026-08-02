"""損益計算書から成長率を導出する（yfinance の比率フィールドが欠けたときの補完）。

## 名指しする問題

2026-08-01 の週次レポートは味の素についてこう書いた:

> 味の素の earnings_growth・forward PER
> 競合比較の最重要項目が欠落。増収 +10.5% が利益に落ちているか不明

これは `info["earningsGrowth"]` が None だったため。しかし同じ銘柄の
`income_stmt` には答えがはっきり載っていた:

| 項目 | FY2025 | FY2026 | YoY |
|:---|---:|---:|---:|
| 純利益 | 70.3B | 134.7B | **+91.6%** |
| 営業利益 | 107.7B | 191.3B | +77.7% |
| 売上高 | 1,530.6B | 1,583.7B | +3.5% |

**「利益に落ちているか不明」ではなく、劇的に落ちていた。**
比率フィールドが空だっただけで、原資料は取れていた。

日本株は `earningsGrowth` が欠けることが多く、その都度
「最重要項目が欠落」という留保付きの分析になっていた。

## 設計

- **導出値であることを必ず示す**（`source: "income_stmt"`）。
  yfinance の比率と同じ顔をさせない。会計期間も添える。
- 前期が赤字・ゼロのときは成長率を返さない。−100%から+50%への「改善」を
  「+150%成長」と書くと意味が反転する。
- 取れなければ None。**推測しない。**
"""

from __future__ import annotations

from typing import Any, Optional

#: 損益計算書の行名。yfinance は表記が揺れるので候補で引く。
_ROW_ALIASES: dict[str, tuple[str, ...]] = {
    "net_income": ("Net Income", "Net Income Common Stockholders",
                   "Net Income From Continuing Operation Net Minority Interest"),
    "operating_income": ("Operating Income", "Total Operating Income As Reported",
                         "EBIT"),
    "revenue": ("Total Revenue", "Operating Revenue"),
    "eps": ("Diluted EPS", "Basic EPS"),
}


def _row(stmt: Any, keys: tuple[str, ...]) -> Optional[list]:
    if stmt is None:
        return None
    try:
        if getattr(stmt, "empty", True):
            return None
        for k in keys:
            if k in stmt.index:
                return list(stmt.loc[k].tolist())
    except Exception:
        return None
    return None


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # NaN 除外


def growth_from_series(values: Optional[list]) -> Optional[float]:
    """直近2期の成長率（比率）。前期がゼロ以下なら None。

    前期が赤字のときに成長率を出すと符号の意味が壊れる
    （-100億 → +50億 を「+150%成長」と書くのは誤読を招く）。
    """
    if not values or len(values) < 2:
        return None
    now, prior = _num(values[0]), _num(values[1])
    if now is None or prior is None or prior <= 0:
        return None
    return (now - prior) / prior


def _periods(stmt: Any) -> list[str]:
    try:
        return [str(c)[:10] for c in list(stmt.columns)[:2]]
    except Exception:
        return []


def derive_growth(symbol: str, ticker: Any = None) -> dict:
    """損益計算書から成長率を導出する。

    Returns:
        {"available", "source", "periods", "earnings_growth",
         "operating_income_growth", "revenue_growth", "eps_growth",
         "turned_profitable", "note"}
    """
    out: dict[str, Any] = {"symbol": symbol, "available": False,
                           "source": "income_stmt"}
    try:
        if ticker is None:
            import yfinance as yf

            ticker = yf.Ticker(symbol)
        stmt = ticker.income_stmt
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out

    if stmt is None or getattr(stmt, "empty", True):
        out["error"] = "損益計算書を取得できませんでした"
        return out

    rows = {name: _row(stmt, keys) for name, keys in _ROW_ALIASES.items()}
    out["periods"] = _periods(stmt)
    out["earnings_growth"] = growth_from_series(rows.get("net_income"))
    out["operating_income_growth"] = growth_from_series(rows.get("operating_income"))
    out["revenue_growth"] = growth_from_series(rows.get("revenue"))
    out["eps_growth"] = growth_from_series(rows.get("eps"))

    # 前期赤字→今期黒字は「成長率」で表せない。事実として別に持つ。
    ni = rows.get("net_income") or []
    if len(ni) >= 2:
        now, prior = _num(ni[0]), _num(ni[1])
        if now is not None and prior is not None and prior <= 0 < now:
            out["turned_profitable"] = True

    out["available"] = any(
        out.get(k) is not None
        for k in ("earnings_growth", "operating_income_growth",
                  "revenue_growth", "eps_growth")
    ) or bool(out.get("turned_profitable"))

    if out["available"]:
        span = " → ".join(reversed(out.get("periods") or [])) or "直近2期"
        out["note"] = (f"yfinance の比率が欠けていたため損益計算書から導出（{span}）。"
                       "会計基準・期ズレの影響を受けるので、他社の比率と"
                       "並べるときは導出値であることを明示すること。")
    else:
        out["error"] = "損益計算書に必要な行が見つかりませんでした"
    return out


def fill_missing_growth(detail: dict, ticker: Any = None) -> dict:
    """`stock_detail` の欠けている成長率だけを導出値で埋める。

    **既にある値は上書きしない。** 埋めた項目は `growth_derived` に残す。
    """
    if not detail:
        return detail
    missing = [k for k in ("earnings_growth", "revenue_growth")
               if detail.get(k) is None]
    if not missing:
        return detail

    derived = derive_growth(detail.get("symbol") or "", ticker=ticker)
    if not derived.get("available"):
        detail["growth_derivation_error"] = derived.get("error")
        return detail

    filled: list[str] = []
    for key in missing:
        value = derived.get(key)
        if value is not None:
            detail[key] = value
            filled.append(key)
    if filled:
        detail["growth_derived"] = {
            "fields": filled,
            "source": derived.get("source"),
            "periods": derived.get("periods"),
            "note": derived.get("note"),
            "operating_income_growth": derived.get("operating_income_growth"),
            "eps_growth": derived.get("eps_growth"),
        }
    if derived.get("turned_profitable"):
        detail["turned_profitable"] = True
    return detail
