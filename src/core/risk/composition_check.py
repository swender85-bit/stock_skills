"""構成の自己検証 -- 「未確認」を「精度が測定済み」に変える.

## なぜこれを足すか

投信の構成は運用会社の月次レポートにしか無く、機械では確認できない。
だから `config/etf_lookthrough.yaml` の `explicit_constituents` は
`verified_as_of: null`（未確認）のまま置くしかなかった。

**だが「確認できない」と「検証できない」は違う。**

その構成が正しければ、**等ウェイトのバスケットは連動対象指数とほぼ同じ動きをする**。
逆に大きくずれるなら、構成が違う。**指数の時系列は取れるのだから、
突き合わせれば精度は測れる。**

## 実測（iFreeNEXT FANG+ / 2026-08-09）

    想定10銘柄の等ウェイト vs ^NYFANG（250営業日）
      相関           0.9231
      累積リターン   バスケット 22.8% / 指数 22.2%（差 0.6pp）
      トラッキング誤差 8.74%（年率）

相関0.92・累積ほぼ一致だが TE 8.7% は小さくない。**1〜3銘柄ずれている疑い。**

leave-one-out（外して改善するなら、その銘柄は誤って入れている）:

    TSLA 除外 → TE 8.59%（**-0.15 改善**）  ← 構成外の疑い
    AVGO 除外 → TE 11.03%（+2.28 悪化）    ← 確実に構成内
    NVDA 除外 → TE 9.75%（+1.01 悪化）     ← 確実に構成内

**これで「未確認」ではなく「相関0.92で追随、TSLAに疑いあり」と書ける。**

## 守ること

- **これは構成の証明ではない。** 似た値動きの別銘柄でも高い相関は出る。
  あくまで「大きく外れてはいない」ことの傍証。
- 指数が取れなければ検証は**「できなかった」**。合格にしない。
"""

from __future__ import annotations

from typing import Any, Optional

#: これ以上のトラッキング誤差（年率%）なら構成を疑う
TE_WARN_PCT = 5.0
#: これ以上の相関があれば「大きくは外れていない」
CORR_OK = 0.90
#: leave-one-out でこれ以上 TE が改善したら、その銘柄は構成外の疑い
LOO_IMPROVE_PCT = 0.10


def _returns(symbol: str, period: str = "1y"):
    from src.data import yahoo_client as yc

    hist = yc.get_price_history(symbol, period=period)
    if hist is None or hist.empty:
        return None
    close = hist["Close"].dropna()
    try:
        if getattr(close.index, "tz", None) is not None:
            close.index = close.index.tz_localize(None)
    except Exception:
        pass
    return close


def verify_composition(
    symbols: list[str],
    index_symbol: str,
    period: str = "1y",
    leave_one_out: bool = True,
) -> dict:
    """想定構成が指数に追随しているかを実測する。

    Returns
    -------
    dict
        {"available", "correlation", "tracking_error_pct", "basket_return_pct",
         "index_return_pct", "suspects", "verdict", "note"}
    """
    import pandas as pd

    out: dict[str, Any] = {"available": False, "symbols": list(symbols),
                           "index_symbol": index_symbol}

    index_close = _returns(index_symbol, period)
    if index_close is None or len(index_close) < 60:
        out["note"] = (f"指数 {index_symbol} の時系列を取得できず、構成を検証できません。"
                       "**『検証済み』ではありません。**")
        return out

    prices = {}
    missing = []
    for s in symbols:
        c = _returns(s, period)
        if c is None or len(c) < 60:
            missing.append(s)
            continue
        prices[s] = c
    if len(prices) < 3:
        out["note"] = (f"構成銘柄の時系列が足りず検証できません（取得できたのは "
                       f"{len(prices)}/{len(symbols)}）。")
        out["missing"] = missing
        return out

    idx_ret = index_close.pct_change().dropna()

    def _stats(names: list[str]) -> Optional[dict]:
        df = pd.DataFrame({n: prices[n] for n in names if n in prices}).dropna()
        if df.empty or len(df) < 60:
            return None
        basket = df.pct_change().dropna().mean(axis=1)
        joined = basket.to_frame("b").join(idx_ret.to_frame("i"), how="inner").dropna()
        if len(joined) < 60:
            return None
        diff = joined["b"] - joined["i"]
        return {
            "n": len(joined),
            "correlation": round(float(joined["b"].corr(joined["i"])), 4),
            "tracking_error_pct": round(float(diff.std() * (252 ** 0.5) * 100), 2),
            "basket_return_pct": round(float((1 + joined["b"]).prod() - 1) * 100, 1),
            "index_return_pct": round(float((1 + joined["i"]).prod() - 1) * 100, 1),
        }

    base = _stats(list(prices))
    if base is None:
        out["note"] = "重なる営業日が足りず検証できませんでした。"
        return out

    out.update(base)
    out["available"] = True
    out["missing"] = missing

    # leave-one-out: 外して改善するなら、その銘柄は構成外の疑い
    suspects: list[dict] = []
    if leave_one_out and len(prices) > 3:
        for name in list(prices):
            trimmed = _stats([n for n in prices if n != name])
            if not trimmed:
                continue
            delta = trimmed["tracking_error_pct"] - base["tracking_error_pct"]
            if delta <= -LOO_IMPROVE_PCT:
                suspects.append({
                    "symbol": name,
                    "te_without": trimmed["tracking_error_pct"],
                    "improvement_pct": round(-delta, 2),
                    "note": (f"{name} を外すとトラッキング誤差が "
                             f"{-delta:.2f}pt 改善する。**構成外の疑い。**"),
                })
    suspects.sort(key=lambda s: -s["improvement_pct"])
    out["suspects"] = suspects

    # 判定
    corr = base["correlation"]
    te = base["tracking_error_pct"]
    gap = abs(base["basket_return_pct"] - base["index_return_pct"])
    if corr >= CORR_OK and te <= TE_WARN_PCT:
        verdict = "良好（構成はほぼ正しいとみてよい）"
    elif corr >= CORR_OK:
        verdict = "概ね追随（ただしTEが大きく、数銘柄ずれている疑い）"
    else:
        verdict = "**追随していない。構成を見直すこと。**"
    out["verdict"] = verdict

    out["note"] = (
        f"想定{len(prices)}銘柄の等ウェイト vs {index_symbol}（{base['n']}営業日）: "
        f"相関 {corr:.4f}、トラッキング誤差 {te:.2f}%（年率）、"
        f"累積リターン バスケット {base['basket_return_pct']:+.1f}% / "
        f"指数 {base['index_return_pct']:+.1f}%（差 {gap:.1f}pp）。{verdict}"
        + (f" 構成外の疑い: {', '.join(s['symbol'] for s in suspects)}。"
           if suspects else "")
        + " ⚠️ **これは構成の証明ではありません。**"
          "似た値動きの別銘柄でも高い相関は出ます。大きく外れていないことの傍証です。"
    )
    if missing:
        out["note"] += f" 取得できなかった銘柄: {', '.join(missing)}。"
    return out


def verify_configured_funds(cfg: Optional[dict] = None) -> dict:
    """`explicit_constituents` に定義された投信を、対応する指数で検証する。"""
    from src.core.risk.etf_lookthrough import load_config

    cfg = cfg or load_config()
    explicit = cfg.get("explicit_constituents") or {}
    tech = cfg.get("technical_proxies") or {}

    results: dict[str, dict] = {}
    for name, entry in explicit.items():
        symbols = [str(s).upper() for s in (entry or {}).get("symbols") or []]
        if not symbols:
            continue
        index_symbol = None
        for key, t in tech.items():
            if str(key) in str(name) or str(name) in str(key):
                index_symbol = t.get("proxy")
                break
        if not index_symbol:
            results[name] = {"available": False,
                             "note": "検証に使う指数が定義されていません。"}
            continue
        results[name] = verify_composition(symbols, index_symbol)
        results[name]["verified_as_of"] = (entry or {}).get("verified_as_of")
    return results


def format_composition_check(results: Optional[dict]) -> str:
    if not results:
        return ""
    lines = ["### 構成の自己検証", ""]
    for name, r in results.items():
        lines.append(f"**{name}**")
        if not r.get("available"):
            lines += ["", f"⚠️ {r.get('note')}", ""]
            continue
        lines += [
            "",
            f"- 相関 **{r['correlation']:.4f}** / トラッキング誤差 "
            f"**{r['tracking_error_pct']:.2f}%**（年率・{r['n']}営業日）",
            f"- 累積リターン: バスケット {r['basket_return_pct']:+.1f}% / "
            f"指数 {r['index_return_pct']:+.1f}%",
            f"- 判定: {r['verdict']}",
        ]
        for s in r.get("suspects") or []:
            lines.append(f"- 🔴 {s['note']}")
        lines += ["", "⚠️ **構成の証明ではありません。**"
                      "似た値動きの別銘柄でも高い相関は出ます。", ""]
    return "\n".join(lines)
