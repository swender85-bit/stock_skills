"""3xスリーブの実体分析 -- 3本のETFを「3つのポジション」と数えない.

## 名指しする問題

SOXL / TECL / TQQQ を保有比率で並べると、3つの独立した資産に見える:

    SOXL 27.5%   TECL 23.2%   TQQQ 11.3%

**だがこの3本は同じ銘柄を持っている。** NVDA は4本すべてに、AVGO は4本に、
AMD・MU は3本に入っている。**分散ではなく、重み違いの同一ポジション**である。

さらに3xには、原資産が同じでも**ボラティリティ・ドラッグ**が効く:

    実効リターン ≈ L·μ − (L·σ)²/2

同じ3xでも原資産のσが違えば期待値がまるで違う。SOXL（半導体σ35%）と
TQQQ（ナスダック100σ22%）では年率ドラッグが 36.7% と 14.5% で倍以上違う。

そして最も欠けていたのが**感応度**である。
「NVDA が実効20%」は分かっても、**NVDA が10%下げたら PF はいくら減るのか**に
答えていなかった。金額で出さないと、リスクの大きさが体感できない。

## 出すもの

1. **重複度** — 3本がどれだけ同じ銘柄を持っているか
2. **ボラドラッグ** — 銘柄ごとの年率ドラッグ（原資産σから導出）
3. **単一銘柄感応度** — 「NVDA -10% で PF は ▲¥X」
4. **実効レバレッジ** — 純資産に対する実質的な倍率
"""

from __future__ import annotations

from typing import Any, Optional

def _sigma_for(symbol: Optional[str], lookup: Optional[str]) -> Optional[float]:
    """原資産のσ前提（年率・小数）。

    ⚠️ **σ前提も再定義しない。** `weekly.py` の `UNDERLYING_ASSUMPTIONS` が
    唯一の定義であり、そこを見る。ここに別の表を置くと、
    片方だけ更新されたときに静かに食い違う。
    """
    try:
        from src.core.portfolio.weekly import UNDERLYING_ASSUMPTIONS

        row = UNDERLYING_ASSUMPTIONS.get(str(symbol or "").upper())
        if row and row.get("annual_vol_pct") is not None:
            return float(row["annual_vol_pct"]) / 100.0
    except Exception:
        pass
    return None

#: 感応度を計算するショック幅
DEFAULT_SHOCKS = (-0.10, -0.20, -0.35)


def volatility_drag(leverage: float, sigma: float) -> float:
    """ボラティリティ・ドラッグ（年率・小数）。

    ⚠️ **計算は再実装せず `src.core.portfolio.projection` に委譲する。**

    実装中に (L·σ)²/2 で計算して 55.1% を出したが、既存実装は
    **L(L-1)σ²/2 で 36.7%** だった。両方とも「ドラッグ」と呼べるが意味が違う:

        (L·σ)²/2      … その ETF 自体の全分散ドラッグ
        L(L-1)σ²/2    … **原資産の L倍に対する超過ドラッグ** ← こちらが正しい

    3xを「原資産の3倍」と期待したときに、実際どれだけ目減りするかを見たいので
    後者を使う。`.claude/rules/weekly-report.md` の表（SOXL 36.7%）とも一致する。

    **同じ量を2箇所で計算すると、必ず数字が食い違う。** 委譲する。
    """
    from src.core.portfolio.projection import volatility_drag as _drag

    return _drag(sigma * 100.0, int(leverage)) / 100.0


def analyze_sleeve(
    holdings: list[dict],
    lookthrough: Optional[dict] = None,
    total_jpy: Optional[float] = None,
) -> dict:
    """レバレッジ・スリーブの実体を出す。

    Returns
    -------
    dict
        {"available", "etfs", "overlap", "sensitivity", "effective_leverage",
         "note"}
    """
    lev_rows = [h for h in holdings or []
                if (h.get("leverage") or 1) and float(h.get("leverage") or 1) > 1]
    if not lev_rows:
        return {"available": False, "etfs": [], "overlap": {}, "sensitivity": [],
                "note": "レバレッジ商品の保有がありません。"}

    total = float(total_jpy or sum(float(h.get("value_jpy") or 0) for h in holdings or []))
    resolved = {r.get("symbol"): r
                for r in (lookthrough or {}).get("resolved_etfs") or []
                if r.get("symbol")}

    # --- 1) ETF ごとのドラッグ ------------------------------------------------
    etfs: list[dict] = []
    for h in lev_rows:
        sym = h.get("symbol")
        lev = float(h.get("leverage") or 1)
        info = resolved.get(sym) or {}
        lookup = info.get("lookup")
        sigma = _sigma_for(sym, lookup)
        value = float(h.get("value_jpy") or 0)
        row = {
            "symbol": sym,
            "name": h.get("name"),
            "leverage": lev,
            "value_jpy": value,
            "weight_pct": h.get("weight_pct"),
            "underlying": info.get("underlying"),
            "lookup": lookup,
            "underlying_sigma": sigma,
            "notional_jpy": round(value * lev),
        }
        if sigma is None:
            row["drag_pct"] = None
            row["drag_note"] = (f"原資産（{lookup or '不明'}）のσ前提が未定義のため"
                                "ドラッグを計算できません。**0ではありません。**")
        else:
            drag = volatility_drag(lev, sigma)
            row["effective_sigma"] = round(lev * sigma, 4)
            row["drag_pct"] = round(drag * 100, 2)
            row["drag_jpy_per_year"] = round(value * drag)
            row["drag_note"] = (
                f"原資産σ {sigma:.0%} × {lev:.0f}倍 = 実効σ {lev * sigma:.0%}。"
                f"**原資産の{lev:.0f}倍に対する超過ドラッグ 年率 {drag * 100:.1f}%**"
                f"（評価額換算 ¥{value * drag:,.0f}/年）。"
                "L(L-1)σ²/2。予測モデルに織り込み済みなので別途引くと二重計上。")
        etfs.append(row)

    # --- 2) 重複度 -------------------------------------------------------------
    overlap = _overlap(lookthrough, {r["symbol"] for r in etfs})

    # --- 3) 単一銘柄の感応度 ---------------------------------------------------
    sensitivity = _sensitivity(lookthrough, total)

    sleeve_value = sum(r["value_jpy"] for r in etfs)
    notional = sum(r["notional_jpy"] for r in etfs)
    eff_lev = (notional / total) if total else None

    note_parts = [
        f"レバレッジ商品 {len(etfs)}本、評価額 ¥{sleeve_value:,.0f}"
        f"（PF の {sleeve_value / total * 100:.1f}%）。"
        f"想定元本 ¥{notional:,.0f}。"
    ]
    if eff_lev:
        note_parts.append(f"**PF全体の実効レバレッジ {eff_lev:.2f}倍**"
                          "（純資産に対する実質的な曝露）。")
    missing_sigma = [r["symbol"] for r in etfs if r.get("drag_pct") is None]
    if missing_sigma:
        note_parts.append(f"⚠️ {', '.join(missing_sigma)} はσ前提が無くドラッグ未計算。")

    return {
        "available": True,
        "etfs": etfs,
        "sleeve_value_jpy": sleeve_value,
        "notional_jpy": notional,
        "effective_leverage": round(eff_lev, 3) if eff_lev else None,
        "overlap": overlap,
        "sensitivity": sensitivity,
        "note": " ".join(note_parts),
    }


def _overlap(lookthrough: Optional[dict], etf_symbols: set) -> dict:
    """3本がどれだけ同じ銘柄を持っているか。

    **「3本持っている＝分散」ではない。** 同じ銘柄が複数本に入っていれば、
    それは重み違いの同一ポジションである。
    """
    rows = (lookthrough or {}).get("effective") or []
    if not rows:
        return {"available": False,
                "reason": "ルックスルーが展開できていないため重複を測れません。"}

    shared: list[dict] = []
    for r in rows:
        sources = [s for s in (r.get("sources") or []) if s in etf_symbols]
        if len(sources) >= 2:
            shared.append({"symbol": r["symbol"],
                           "effective_pct": r.get("effective_pct"),
                           "in_etfs": sources, "count": len(sources)})
    shared.sort(key=lambda x: (-x["count"], -(x["effective_pct"] or 0)))
    shared_pct = round(sum(x["effective_pct"] or 0 for x in shared), 2)

    return {
        "available": True,
        "shared_names": shared,
        "shared_effective_pct": shared_pct,
        "message": (
            f"**{len(shared)}銘柄が2本以上のレバレッジETFに重複**しており、"
            f"実効エクスポージャー合計 {shared_pct:.1f}% に相当します。"
            "3本を別々のポジションと数えると、集中を過小評価します。"
            if shared else "レバレッジETF間で重複する銘柄はありません。"
        ),
    }


def _sensitivity(lookthrough: Optional[dict], total_jpy: float,
                 top_n: int = 6, shocks=DEFAULT_SHOCKS) -> list[dict]:
    """単一銘柄が動いたときの PF への影響（金額）。

    「NVDA が実効20%」では大きさが伝わらない。
    **「NVDA -10% で PF は ▲¥447,000」**と書けば伝わる。
    """
    rows = (lookthrough or {}).get("effective") or []
    out: list[dict] = []
    for r in rows[:top_n]:
        eff = float(r.get("effective_pct") or 0) / 100.0
        if eff <= 0 or not total_jpy:
            continue
        out.append({
            "symbol": r["symbol"],
            "effective_pct": r.get("effective_pct"),
            "via": r.get("sources") or [],
            "impacts": [
                {"shock_pct": round(s * 100, 1),
                 "pf_change_jpy": round(total_jpy * eff * s),
                 "pf_change_pct": round(eff * s * 100, 2)}
                for s in shocks
            ],
        })
    return out


def format_sleeve(sleeve: Optional[dict]) -> str:
    """レポート用。"""
    if not sleeve or not sleeve.get("available"):
        return ("### レバレッジ・スリーブの実体\n\n⚠️ "
                + ((sleeve or {}).get("note") or "分析できませんでした。") + "\n")

    lines = ["### レバレッジ・スリーブの実体", "", sleeve["note"], "",
             "| ETF | 倍率 | 評価額 | 想定元本 | 原資産σ(前提) | 実効σ | **年率ドラッグ** |",
             "|:---|---:|---:|---:|---:|---:|---:|"]
    for e in sleeve["etfs"]:
        sig = f"{e['underlying_sigma']:.0%}" if e.get("underlying_sigma") else "—"
        esig = f"{e['effective_sigma']:.0%}" if e.get("effective_sigma") else "—"
        drag = f"**{e['drag_pct']:.1f}%**" if e.get("drag_pct") is not None else "計算不可"
        lines.append(
            f"| {e['symbol']} | {e['leverage']:.0f}x | ¥{e['value_jpy']:,.0f} "
            f"| ¥{e['notional_jpy']:,.0f} | {sig} | {esig} | {drag} |")

    ov = sleeve.get("overlap") or {}
    if ov.get("available"):
        lines += ["", "**重複（3本を別ポジションと数えない）**", "", ov["message"], ""]
        if ov.get("shared_names"):
            lines += ["| 銘柄 | 実効% | 重複しているETF |", "|:---|---:|:---|"]
            for s in ov["shared_names"][:10]:
                lines.append(f"| {s['symbol']} | {s['effective_pct']:.2f}% "
                             f"| {', '.join(s['in_etfs'])}（{s['count']}本） |")

    if sleeve.get("sensitivity"):
        lines += ["", "**単一銘柄が動いたときの PF への影響（金額）**", "",
                  "| 銘柄 | 実効% | -10% | -20% | -35% |", "|:---|---:|---:|---:|---:|"]
        for s in sleeve["sensitivity"]:
            cells = " | ".join(f"▲¥{abs(i['pf_change_jpy']):,}" for i in s["impacts"])
            lines.append(f"| {s['symbol']} | {s['effective_pct']:.2f}% | {cells} |")

    return "\n".join(lines) + "\n"
