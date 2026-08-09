"""前方カレンダー（数ヶ月先） -- 「翌週」だけを見る構造をやめる.

## 名指しする問題

既存の前方イベントは **`next_week_range()` の1週間しか見ていない**。
その結果、レポートは毎週こう書いていた:

> 中身の企業の翌週決算: ゼロ。つまり ETF 経由でも、翌週に「決算で飛ぶ」経路は無い。

これは事実として正しくても、**意思決定には使えない**。
実際には、その時点で以下が確定していた:

    AMAT  2026-08-14  実効 6.19%（SOXL/TECL 経由・3倍）
    NVDA  2026-08-27  実効 20.05%（PFの5分の1）
    MRVL  2026-08-28  実効 3.44%
    MDT   2026-09-01  直接 6.21%
    AVGO  2026-09-03  実効 11.45%
    MU    2026-09-24  実効 10.64%
    LRCX  2026-10-22  実効 5.23%
    QCOM  2026-10-30  直接+経由 10.16%

**「翌週ゼロ」と「3ヶ月ゼロ」はまったく違う。**
前者だけを見ていると、3週間後に PF の2割が決算を通過することに気づけない。

利用者の指摘そのまま:

> 「翌週」とかでなく「翌週以降」、つまり数カ月後

## 何をするか

保有銘柄と**ETF経由の構成銘柄**の決算・配当を、既定90日先まで並べる。
各イベントに**実効エクスポージャー**（レバレッジ込み）を付けるので、
「PFの何%がその日に通過するか」が読める。

## 守ること

- **取得できなかった銘柄を「予定なし」と書かない。** `unavailable` に必ず載せる。
  2026-08-08 は通信断で全滅したのに「翌週の決算はゼロ」と書かれた。
- ETF 自体に決算は**無い**。「取得失敗」と区別する。
- 期間が長いほど日程は変更されやすい。**確定度を添える。**
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Optional

#: 既定でどこまで先を見るか
DEFAULT_HORIZON_DAYS = 90

#: 集中度の警告水準（同一月にこの比率以上が決算を通過したら注意）
MONTH_CONCENTRATION_WARN = 25.0


def _jp(d: date) -> str:
    return f"{'月火水木金土日'[d.weekday()]} {d.month}/{d.day}"


def _is_fund(symbol: str, name: Optional[str] = None) -> bool:
    """ETF/投信か。**決算が「存在しない」ことを「取得失敗」と混同しないため。**"""
    try:
        from src.core.risk.etf_lookthrough import resolve_proxy

        kind = (resolve_proxy(symbol, name) or {}).get("kind")
        if kind in ("leveraged_etf", "fund_proxy", "unmapped_fund"):
            return True
    except Exception:
        pass
    return False


def _confidence(days_ahead: int) -> str:
    """先の日付ほど動く。確定度を明示して、確定値のように読ませない。"""
    if days_ahead <= 14:
        return "高（直近。変更は稀）"
    if days_ahead <= 45:
        return "中（会社公表ベース。変更あり得る）"
    return "低（推定を含む。決算日は近づくと確定する）"


def build_forward_horizon(
    holdings: list[dict],
    lookthrough: Optional[dict] = None,
    *,
    as_of: Optional[date] = None,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    events_by_symbol: Optional[dict] = None,
    min_effective_pct: float = 0.5,
) -> dict:
    """保有＋ETF構成銘柄の決算・配当を、数ヶ月先まで並べる。

    Returns
    -------
    dict
        {"available", "range", "events", "by_month", "concentration",
         "unavailable", "no_earnings", "note"}

    各イベントの `effective_pct` は**レバレッジ込みの実質エクスポージャー**。
    「その日に PF の何%が決算を通過するか」を読むための数字。
    """
    from src.core.risk.forward_events import _parse, _safe_fetch_events

    ref = as_of or date.today()
    end = ref + timedelta(days=horizon_days)

    # 1) 直接保有
    direct: dict[str, dict] = {}
    for h in holdings or []:
        sym = h.get("symbol")
        if not sym:
            continue
        row = direct.setdefault(sym, {
            "symbol": sym, "name": h.get("name"), "direct_pct": 0.0,
            "via_etf_pct": 0.0, "sources": [], "leverage": h.get("leverage") or 1,
        })
        try:
            row["direct_pct"] += float(h.get("weight_pct") or 0.0)
        except (TypeError, ValueError):
            pass

    # 2) ETF 経由（実効エクスポージャー）
    exposure: dict[str, dict] = {k: dict(v) for k, v in direct.items()}
    for r in (lookthrough or {}).get("effective") or []:
        sym = r.get("symbol")
        if not sym:
            continue
        row = exposure.setdefault(sym, {
            "symbol": sym, "name": None, "direct_pct": 0.0,
            "via_etf_pct": 0.0, "sources": [],
        })
        row["via_etf_pct"] = float(r.get("via_etf_pct") or 0.0)
        row["direct_pct"] = max(row["direct_pct"], float(r.get("direct_pct") or 0.0))
        row["sources"] = r.get("sources") or []

    for row in exposure.values():
        row["effective_pct"] = round(row["direct_pct"] + row["via_etf_pct"], 3)

    targets = {s: r for s, r in exposure.items()
               if r["effective_pct"] >= min_effective_pct}
    if not targets:
        return {"available": False, "events": [], "unavailable": [],
                "no_earnings": [], "by_month": {},
                "note": "対象銘柄がありません（保有もETF展開も空）。"}

    # 3) 日程を取る
    symbols = sorted(targets)
    fetched = (events_by_symbol if events_by_symbol is not None
               else _safe_fetch_events(symbols))

    events: list[dict] = []
    unavailable: list[dict] = []
    no_earnings: list[str] = []

    for sym in symbols:
        ev = fetched.get(sym) or {}
        row = targets[sym]

        if not ev.get("available"):
            # ETF/投信に決算は **存在しない**。「取得できなかった」と混同しない。
            # 見るべきは中身の企業で、それは別途 lookthrough が展開している。
            if _is_fund(sym, row.get("name")):
                no_earnings.append(sym)
                continue
            # **「予定なし」ではなく「取得できなかった」。** ここを混同すると
            # 通信断の週に「決算はゼロ」という嘘が出る（2026-08-08）。
            unavailable.append({
                "symbol": sym, "effective_pct": row["effective_pct"],
                "reason": ev.get("error") or "日程を取得できませんでした",
            })
            continue

        dates = [d for d in (_parse(x) for x in ev.get("earnings_dates") or []) if d]
        upcoming = [d for d in dates if ref <= d <= end]
        if not upcoming:
            no_earnings.append(sym)

        for d in upcoming:
            days_ahead = (d - ref).days
            events.append({
                "kind": "earnings",
                "date": d.isoformat(),
                "day_label": _jp(d),
                "days_ahead": days_ahead,
                "symbol": sym,
                "name": row.get("name"),
                "effective_pct": row["effective_pct"],
                "direct_pct": round(row["direct_pct"], 3),
                "via_etf_pct": round(row["via_etf_pct"], 3),
                "via": row.get("sources") or [],
                "held_directly": row["direct_pct"] > 0,
                "confidence": _confidence(days_ahead),
                "source": ev.get("source") or "yfinance",
            })

        ex = _parse(ev.get("ex_dividend_date"))
        if ex and ref <= ex <= end:
            events.append({
                "kind": "ex_dividend",
                "date": ex.isoformat(),
                "day_label": _jp(ex),
                "days_ahead": (ex - ref).days,
                "symbol": sym,
                "name": row.get("name"),
                "effective_pct": row["effective_pct"],
                "direct_pct": round(row["direct_pct"], 3),
                "via_etf_pct": round(row["via_etf_pct"], 3),
                "via": row.get("sources") or [],
                "held_directly": row["direct_pct"] > 0,
                "confidence": _confidence((ex - ref).days),
                "source": ev.get("source") or "yfinance",
                # 権利落ちの下落は損失ではない。週次騰落から分離して読む。
                "note": "権利落ちの下落は損失ではありません",
            })

    events.sort(key=lambda e: (e["date"], -e["effective_pct"]))

    # 4) 月別の集中度 — 「いつ PF の何%が通過するか」
    by_month: dict[str, dict] = {}
    for e in events:
        if e["kind"] != "earnings":
            continue
        month = e["date"][:7]
        bucket = by_month.setdefault(month, {"month": month, "count": 0,
                                             "effective_pct": 0.0, "symbols": []})
        bucket["count"] += 1
        bucket["effective_pct"] = round(bucket["effective_pct"] + e["effective_pct"], 2)
        bucket["symbols"].append(e["symbol"])

    hot = [m for m in by_month.values()
           if m["effective_pct"] >= MONTH_CONCENTRATION_WARN]
    concentration = {
        "warn_threshold_pct": MONTH_CONCENTRATION_WARN,
        "hot_months": sorted(hot, key=lambda m: -m["effective_pct"]),
        "message": (
            "；".join(f"{m['month']} に実効 {m['effective_pct']:.1f}%"
                      f"（{', '.join(m['symbols'][:4])}）が決算を通過"
                      for m in sorted(hot, key=lambda x: x["month"]))
            if hot else "単月に集中している決算はありません。"
        ),
    }

    note_parts = [
        f"{ref.isoformat()} 〜 {end.isoformat()}（{horizon_days}日先）の"
        f"確定イベント {len(events)}件。実効%はレバレッジ込み。"
    ]
    if unavailable:
        note_parts.append(
            f"⚠️ **{len(unavailable)}銘柄は日程を取得できませんでした**"
            f"（{', '.join(u['symbol'] for u in unavailable[:6])}）。"
            "**『予定なし』ではありません。**")
    if no_earnings:
        note_parts.append(
            f"期間内に決算が無い（取得は成功）: {', '.join(no_earnings[:8])}")

    return {
        "available": True,
        "range": {"start": ref.isoformat(), "end": end.isoformat(),
                  "horizon_days": horizon_days},
        "events": events,
        "by_month": dict(sorted(by_month.items())),
        "concentration": concentration,
        "unavailable": unavailable,
        "no_earnings": no_earnings,
        "note": " ".join(note_parts),
    }


def format_horizon(horizon: Optional[dict], limit: int = 25) -> str:
    """レポート用の表。**実効%付き**で「いつ何%が通過するか」を見せる。"""
    if not horizon or not horizon.get("available"):
        reason = (horizon or {}).get("note") or "前方カレンダーを取得できませんでした。"
        return f"### 前方カレンダー\n\n⚠️ {reason}\n"

    lines = [
        "### 前方カレンダー（数ヶ月先）",
        "",
        f"{horizon['range']['start']} 〜 {horizon['range']['end']}"
        f"（{horizon['range']['horizon_days']}日先）",
        "",
        "| 日付 | 銘柄 | 実効% | 保有形態 | あと | 確定度 |",
        "|:---|:---|---:|:---|---:|:---|",
    ]
    for e in horizon["events"][:limit]:
        kind = "決算" if e["kind"] == "earnings" else "権利落ち"
        via = "直接" if e.get("held_directly") else f"ETF経由({','.join(e.get('via') or [])[:20]})"
        lines.append(
            f"| {e['day_label']} | {e['symbol']} {kind} | {e['effective_pct']:.2f}% "
            f"| {via} | {e['days_ahead']}日 | {e['confidence'].split('（')[0]} |")

    lines += ["", "**月別の通過比率:**", ""]
    for month, b in (horizon.get("by_month") or {}).items():
        lines.append(f"- {month}: 実効 **{b['effective_pct']:.1f}%** "
                     f"（{b['count']}社: {', '.join(b['symbols'][:6])}）")

    conc = horizon.get("concentration") or {}
    if conc.get("hot_months"):
        lines += ["", f"⚠️ {conc['message']}"]

    if horizon.get("unavailable"):
        lines += ["", "🔴 **日程を取得できなかった銘柄**（『予定なし』ではない）:"]
        for u in horizon["unavailable"]:
            lines.append(f"- {u['symbol']}（実効 {u['effective_pct']:.2f}%）— {u['reason']}")

    return "\n".join(lines) + "\n"
