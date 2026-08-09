"""構成銘柄インテリジェンス -- ETFの中身を「名前の一覧」で終わらせない.

## 名指しする問題

ルックスルーで実効エクスポージャーが出せるようになった。だがレポートに載るのは
**銘柄名と比率だけ**で、そこから先が無かった:

    | 実質保有 | 実効比率 |
    | NVDA    | 20.05%  |
    | AAPL    | 12.03%  |

利用者の指摘そのまま:

> このFANG＋の構成についても、中身の企業が与える今後の影響とか分析していないから、
> なんの意味もなしていない

**「NVDA が20%」は情報だが、「だから何を見るべきか」が無い限り判断に使えない。**

## 何を集めるか

各構成銘柄について、判断に必要な最小セットを1つの dossier にまとめる:

| 要素 | 何のために |
|:---|:---|
| 価格・週次・月次 | 短期の勢いと、直近1ヶ月の位置 |
| RSI・200日乖離 | 過熱と平均回帰の余地 |
| PER・予想PER | 期待の織り込み度 |
| 次回決算日と残り日数 | いつ再評価が起きるか |
| **ニュース** | **何が起きているか** |
| 実効エクスポージャー | それが PF にどれだけ効くか |

そして **「週次はプラスだが月次はマイナス」** のような、単独指標では見えない
形を `signal` として明示する。2026-08-09 の実データではこれが決定的だった:

    AMD -11.6%(月) / +1.5%(週)、MU -11.5%(月) / +6.6%(週)
    → 「上放れ」ではなく「下げた分の戻り」

## 守ること

- 取れなかったものを「無い」と書かない。`missing` に必ず載せる
- ニュースは**外部言説（深度1）**。一次観測ではない
- ここで得た銘柄を「個別に売買する対象」として扱わない。
  目的は**自分が何に曝されているかを知ること**
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

#: dossier を作る上位何銘柄か。全部やると時間もAPIも食う。
DEFAULT_TOP_N = 12

#: 「戻り」と判定する閾値（週次プラス・月次マイナス）
REBOUND_WEEK_MIN = 2.0
REBOUND_MONTH_MAX = -3.0

#: 200日線からの乖離がこれを超えたら平均回帰の圧力を明示する
STRETCHED_DEV_PCT = 40.0


def _pct_change(closes: list[float], back: int) -> Optional[float]:
    if not closes or len(closes) <= back:
        return None
    prev = closes[-1 - back]
    if not prev:
        return None
    return (closes[-1] / prev - 1.0) * 100.0


def _classify(week: Optional[float], month: Optional[float],
              rsi: Optional[float], dev: Optional[float]) -> list[str]:
    """単独指標では見えない形に名前を付ける。"""
    out: list[str] = []
    if week is not None and month is not None:
        if week >= REBOUND_WEEK_MIN and month <= REBOUND_MONTH_MAX:
            out.append("戻り（週次プラス・月次マイナス。上放れではない）")
        elif week >= REBOUND_WEEK_MIN and month > 0:
            out.append("継続上昇（週次・月次ともプラス）")
        elif week < 0 and month <= REBOUND_MONTH_MAX:
            out.append("下降継続")
    if rsi is not None:
        if rsi >= 70:
            out.append(f"過熱（RSI {rsi:.0f}）")
        elif rsi <= 30:
            out.append(f"売られすぎ（RSI {rsi:.0f}）")
    if dev is not None and dev >= STRETCHED_DEV_PCT:
        out.append(f"200日線から +{dev:.0f}% 乖離（平均回帰の余地が大きい）")
    return out


def build_dossier(
    symbol: str,
    effective_pct: float = 0.0,
    via: Optional[list[str]] = None,
    held_directly: bool = False,
    news_days: int = 7,
    news_limit: int = 3,
) -> dict:
    """構成銘柄1つ分の判断材料をまとめる。

    **取れなかった項目は `missing` に載せる。** 空欄を「異常なし」と読ませない。
    """
    from src.data import yahoo_client as yc

    row: dict[str, Any] = {
        "symbol": symbol,
        "effective_pct": round(float(effective_pct or 0.0), 3),
        "via": list(via or []),
        "held_directly": bool(held_directly),
        "missing": [],
    }

    info = detail = None
    try:
        info = yc.get_stock_info(symbol)
        detail = yc.get_stock_detail(symbol)
    except Exception:
        pass
    if not info:
        row["missing"].append("価格・基本情報")
    else:
        row["name"] = info.get("name")
        row["price"] = info.get("price")
        row["sector"] = info.get("sector")

    d = detail or info or {}
    for key, label in (("per", "PER"), ("forward_per", "予想PER"),
                       ("pbr", "PBR"), ("operating_margin", "営業利益率"),
                       ("revenue_growth", "売上成長率")):
        value = d.get(key)
        if value is None:
            row["missing"].append(label)
        row[key] = value

    closes: list[float] = []
    try:
        hist = yc.get_price_history(symbol, period="1y")
        if hist is not None and not hist.empty:
            closes = [float(x) for x in hist["Close"].dropna().tolist()]
    except Exception:
        pass

    if not closes:
        row["missing"].append("価格系列")
    else:
        row["week_change_pct"] = _pct_change(closes, 5)
        row["month_change_pct"] = _pct_change(closes, 21)
        row["quarter_change_pct"] = _pct_change(closes, 63)
        try:
            from src.core.technicals import analyze_prices

            t = analyze_prices(closes) or {}
            rng = t.get("range_52w") or {}
            row["rsi14"] = t.get("rsi14")
            row["sma200_deviation_pct"] = t.get("sma200_deviation_pct")
            row["range_52w_position"] = rng.get("position")
            row["from_high_pct"] = rng.get("from_high_pct")
            row["volatility_pct"] = t.get("volatility_pct")
        except Exception:
            row["missing"].append("テクニカル")

    # 次回決算
    try:
        from src.data.yahoo_client.events import get_symbol_events

        ev = get_symbol_events(symbol) or {}
        if ev.get("available"):
            upcoming = sorted(x for x in (ev.get("earnings_dates") or [])
                              if str(x) >= date.today().isoformat())
            if upcoming:
                row["next_earnings"] = upcoming[0]
                row["days_to_earnings"] = (
                    date.fromisoformat(upcoming[0][:10]) - date.today()).days
        else:
            row["missing"].append("決算日")
    except Exception:
        row["missing"].append("決算日")

    # ニュース（外部言説・深度1）
    row["news"] = _news_for(symbol, news_days, news_limit)
    if not row["news"]:
        row["missing"].append("ニュース")

    row["signals"] = _classify(row.get("week_change_pct"), row.get("month_change_pct"),
                               row.get("rsi14"), row.get("sma200_deviation_pct"))
    return row


def _news_for(symbol: str, days: int, limit: int) -> list[dict]:
    """ニュース。**一次観測ではなく外部言説（深度1）。**

    ⚠️ **finnhub だけを見ない。** finnhub のフリー枠は日本株のニュースに対応せず、
    味の素・ニトリが常に「取得できませんでした」になっていた。実際には
    yfinance 経由で 2802.T は10件、9843.T は3件取れる。
    §16-8「単一の取得元に依存しない」の違反そのものだった。
    """
    out: list[dict] = []

    try:
        from src.data import finnhub_client

        if finnhub_client.is_available():
            out = [{**a, "provenance": "external_discourse", "source_api": "finnhub"}
                   for a in (finnhub_client.get_company_news(
                       symbol, days=days, limit=limit) or [])]
    except Exception:
        pass
    if out:
        return out

    # yfinance へフォールバック（日本株はこちらでしか取れない）
    try:
        import yfinance as yf

        for raw in (yf.Ticker(symbol).news or [])[:limit]:
            content = raw.get("content") or raw
            title = content.get("title") or content.get("headline")
            if not title:
                continue
            out.append({
                "headline": str(title),
                "url": ((content.get("canonicalUrl") or {}).get("url")
                        if isinstance(content.get("canonicalUrl"), dict)
                        else content.get("link")) or "",
                "datetime": content.get("pubDate") or content.get("providerPublishTime"),
                "provenance": "external_discourse",
                "source_api": "yfinance",
            })
    except Exception:
        pass
    return out


def build_constituent_intel(
    lookthrough: Optional[dict],
    holdings: Optional[list[dict]] = None,
    top_n: int = DEFAULT_TOP_N,
    min_effective_pct: float = 1.0,
) -> dict:
    """実効エクスポージャー上位の構成銘柄について dossier を作る。

    Returns
    -------
    dict
        {"available", "dossiers", "covered_pct", "signals", "missing_news",
         "note"}
    """
    rows = (lookthrough or {}).get("effective") or []
    direct = {h.get("symbol") for h in holdings or [] if h.get("symbol")}
    targets = [r for r in rows
               if (r.get("effective_pct") or 0) >= min_effective_pct][:top_n]

    if not targets:
        return {"available": False, "dossiers": [], "covered_pct": 0.0,
                "signals": {}, "missing_news": [],
                "note": "ルックスルーが展開できていないため構成銘柄を分析できません。"
                        "**『中身が無い』ではありません。**"}

    dossiers = []
    for r in targets:
        dossiers.append(build_dossier(
            r["symbol"],
            effective_pct=r.get("effective_pct") or 0.0,
            via=r.get("sources"),
            held_directly=(r["symbol"] in direct) or bool(r.get("direct_pct")),
        ))

    covered = round(sum(d["effective_pct"] for d in dossiers), 2)

    # 形ごとの集計。**個別に見ていると気づけない共通パターンを出す。**
    buckets: dict[str, list[str]] = {}
    for d in dossiers:
        for s in d["signals"]:
            key = s.split("（")[0]
            buckets.setdefault(key, []).append(d["symbol"])

    missing_news = [d["symbol"] for d in dossiers if "ニュース" in d["missing"]]

    note = (f"実効エクスポージャー上位 {len(dossiers)}銘柄"
            f"（合計 {covered:.1f}%）を分析しました。")
    if missing_news:
        note += (f" ⚠️ **{len(missing_news)}銘柄はニュースを取得できませんでした**"
                 f"（{', '.join(missing_news[:6])}）。**『材料なし』ではありません。**")

    return {
        "available": True,
        "dossiers": dossiers,
        "covered_pct": covered,
        "signals": buckets,
        "missing_news": missing_news,
        "note": note,
    }


def format_constituent_intel(intel: Optional[dict], limit: int = 12) -> str:
    """レポート用。**比率の一覧で終わらせず、形と見どころまで書く。**"""
    if not intel or not intel.get("available"):
        return ("### 構成銘柄の分析\n\n⚠️ "
                + ((intel or {}).get("note") or "展開できませんでした。") + "\n")

    lines = [
        "### 構成銘柄の分析（実効エクスポージャー上位）",
        "",
        "| 銘柄 | 実効% | 週 | 月 | RSI | 200d乖離 | PER | 次回決算 | 形 |",
        "|:---|---:|---:|---:|---:|---:|---:|:---|:---|",
    ]
    for d in intel["dossiers"][:limit]:
        def f(v, suffix="", digits=1):
            return f"{v:+.{digits}f}{suffix}" if isinstance(v, (int, float)) else "—"

        earn = d.get("next_earnings") or "—"
        if d.get("days_to_earnings") is not None:
            earn += f"({d['days_to_earnings']}日)"
        shape = "／".join(s.split("（")[0] for s in d["signals"]) or "—"
        lines.append(
            f"| {d['symbol']} | {d['effective_pct']:.2f}% "
            f"| {f(d.get('week_change_pct'), '%')} | {f(d.get('month_change_pct'), '%')} "
            f"| {d.get('rsi14', 0) or 0:.0f} | {f(d.get('sma200_deviation_pct'), '%', 0)} "
            f"| {d.get('per') or '—'} | {earn} | {shape} |")

    if intel.get("signals"):
        lines += ["", "**共通して現れている形:**", ""]
        for shape, syms in sorted(intel["signals"].items(),
                                  key=lambda kv: -len(kv[1])):
            lines.append(f"- **{shape}**: {', '.join(syms)}（{len(syms)}銘柄）")

    news_rows = [d for d in intel["dossiers"] if d.get("news")]
    if news_rows:
        lines += ["", "**構成銘柄のニュース（外部言説・深度1）:**", ""]
        for d in news_rows[:6]:
            for a in d["news"][:2]:
                lines.append(f"- [{d['symbol']}] {str(a.get('headline'))[:88]}")

    if intel.get("missing_news"):
        lines += ["", f"⚠️ ニュース未取得: {', '.join(intel['missing_news'])}"
                      "（**『材料なし』ではありません**）"]

    lines += ["", f"{intel['note']}"]
    return "\n".join(lines) + "\n"
