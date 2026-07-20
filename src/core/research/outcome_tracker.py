"""過去判断の答え合わせ（学習ループ / upgrade v1.0 Phase 4）。

Screen で挙げた銘柄・Report で下した判断は、記録時点の株価とともに
`data/history/` に残っている。このモジュールはそれらを「答え合わせ」し、
記録時の株価と現在株価を比較して命中率（30/90/180日経過分）を集計する。

設計方針:
- I/O（履歴読込・株価取得・ノート保存）と純粋計算を分離してテスト可能にする
- 現在株価が取れない判断はスキップ（graceful degradation）
- 近似: 「H日後ちょうど」の株価ではなく現在株価で判定する（履歴株価は保持していないため）。
  経過日数が H 以上の判断を「H日バケット」に含める。この近似は出力にも明記する。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Callable, Iterable, Optional

# 判断の方向。買い寄り＝上昇で的中、回避寄り＝下落/据置で的中、中立＝命中率から除外。
BUY = "buy"
AVOID = "avoid"
NEUTRAL = "neutral"

# Report verdict → 方向。部分一致で判定する。
_BUY_HINTS = ("割安", "買")
_AVOID_HINTS = ("割高", "売")

# 命中率を集計する経過日数バケット
DEFAULT_HORIZONS = (30, 90, 180)


def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def verdict_direction(verdict: Optional[str], value_score: Optional[float] = None) -> str:
    """Report の verdict（＋補助的に value_score）から方向を決める。"""
    v = verdict or ""
    if any(h in v for h in _BUY_HINTS):
        return BUY
    if any(h in v for h in _AVOID_HINTS):
        return AVOID
    # verdict が中立/ETF等でも、明確に高いスコアがあれば買い寄りとみなす
    if value_score is not None and value_score >= 70:
        return BUY
    return NEUTRAL


def compute_outcome(
    entry_price: Optional[float],
    current_price: Optional[float],
    entry_date: Optional[str],
    today: Optional[date] = None,
) -> Optional[dict]:
    """記録時株価・現在株価・記録日から結果を計算する。

    無効（株価欠損/ゼロ/負・日付欠損）なら None を返す。
    """
    today = today or date.today()
    d0 = _parse_date(entry_date)
    if d0 is None:
        return None
    try:
        ep = float(entry_price)
        cp = float(current_price)
    except (TypeError, ValueError):
        return None
    if ep <= 0 or cp <= 0:
        return None

    days = (today - d0).days
    if days < 0:
        return None
    return {
        "entry_price": ep,
        "current_price": cp,
        "return_pct": (cp - ep) / ep,
        "days_elapsed": days,
    }


def is_hit(direction: str, return_pct: float) -> Optional[bool]:
    """方向と実現リターンから的中/外れを判定する。中立は None（集計対象外）。"""
    if direction == BUY:
        return return_pct > 0
    if direction == AVOID:
        return return_pct <= 0
    return None


def collect_judgments(base_dir: str = "data/history") -> list[dict]:
    """screen / report 履歴を、答え合わせ可能な判断リストに正規化する。

    各判断: {source, symbol, name, entry_date, entry_price, currency,
             direction, label, preset/region(任意)}
    """
    from src.data.history.load import load_history

    judgments: list[dict] = []

    for rec in load_history("report", base_dir=base_dir):
        sym = rec.get("symbol")
        if not sym:
            continue
        judgments.append({
            "source": "report",
            "symbol": sym,
            "name": rec.get("name"),
            "entry_date": rec.get("date"),
            "entry_price": rec.get("price"),
            "currency": rec.get("currency"),
            "direction": verdict_direction(rec.get("verdict"), rec.get("value_score")),
            "label": rec.get("verdict") or "",
        })

    for rec in load_history("screen", base_dir=base_dir):
        preset = rec.get("preset")
        region = rec.get("region")
        edate = rec.get("date")
        for r in rec.get("results", []) or []:
            sym = r.get("symbol")
            if not sym:
                continue
            # スクリーニングで挙がった銘柄は「買い候補」として提示された＝買い方向
            judgments.append({
                "source": "screen",
                "symbol": sym,
                "name": r.get("name"),
                "entry_date": edate,
                "entry_price": r.get("price"),
                "currency": r.get("currency"),
                "direction": BUY,
                "label": f"{region}/{preset}",
                "preset": preset,
                "region": region,
            })

    return judgments


def evaluate(
    judgments: Iterable[dict],
    price_fn: Callable[[str], Optional[float]],
    today: Optional[date] = None,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
) -> dict:
    """各判断を答え合わせし、全体＋バケット別＋preset別に集計する。

    price_fn(symbol) は現在株価（float）または None（取得不可）を返す。
    """
    today = today or date.today()
    price_cache: dict[str, Optional[float]] = {}

    def _price(sym: str) -> Optional[float]:
        if sym not in price_cache:
            try:
                price_cache[sym] = price_fn(sym)
            except Exception:
                price_cache[sym] = None
        return price_cache[sym]

    evaluated: list[dict] = []
    skipped = 0
    for j in judgments:
        if j.get("direction") == NEUTRAL:
            skipped += 1
            continue
        cp = _price(j["symbol"])
        outcome = compute_outcome(j.get("entry_price"), cp, j.get("entry_date"), today)
        if outcome is None:
            skipped += 1
            continue
        hit = is_hit(j["direction"], outcome["return_pct"])
        if hit is None:
            skipped += 1
            continue
        row = {**j, **outcome, "hit": hit}
        evaluated.append(row)

    # バケット別集計（経過日数 >= H を H バケットに含める）
    horizon_stats = {}
    for h in horizons:
        rows = [r for r in evaluated if r["days_elapsed"] >= h]
        hits = sum(1 for r in rows if r["hit"])
        horizon_stats[h] = {
            "n": len(rows),
            "hits": hits,
            "hit_rate": (hits / len(rows)) if rows else None,
            "avg_return": (sum(r["return_pct"] for r in rows) / len(rows)) if rows else None,
        }

    # preset別（screen のみ）
    preset_stats: dict[str, dict] = {}
    for r in evaluated:
        if r["source"] != "screen":
            continue
        key = r.get("preset") or "unknown"
        s = preset_stats.setdefault(key, {"n": 0, "hits": 0})
        s["n"] += 1
        s["hits"] += 1 if r["hit"] else 0
    for s in preset_stats.values():
        s["hit_rate"] = s["hits"] / s["n"] if s["n"] else None

    overall_hits = sum(1 for r in evaluated if r["hit"])
    return {
        "as_of": today.isoformat(),
        "evaluated": evaluated,
        "n_evaluated": len(evaluated),
        "n_skipped": skipped,
        "overall_hit_rate": (overall_hits / len(evaluated)) if evaluated else None,
        "horizons": horizon_stats,
        "presets": preset_stats,
    }


def find_big_misses(
    summary: dict,
    threshold_pct: float = -0.20,
    min_days: int = 90,
) -> list[dict]:
    """買い判断だったのに大きく下落した「学ぶべき外し」を抽出する。"""
    out = []
    for r in summary.get("evaluated", []):
        if (
            r["direction"] == BUY
            and not r["hit"]
            and r["return_pct"] <= threshold_pct
            and r["days_elapsed"] >= min_days
        ):
            out.append(r)
    out.sort(key=lambda r: r["return_pct"])  # 最悪から
    return out


def _fmt_pct(x: Optional[float]) -> str:
    return f"{x*100:+.1f}%" if x is not None else "—"


def _fmt_rate(x: Optional[float]) -> str:
    return f"{x*100:.0f}%" if x is not None else "—"


def render_markdown(summary: dict, misses: Optional[list[dict]] = None) -> str:
    """OUTCOMES.md 用の Markdown を生成する。"""
    lines: list[str] = []
    lines.append("---")
    lines.append("title: 判断の答え合わせ（学習ループ）")
    lines.append("tags: [outcomes, learning]")
    lines.append(f"created: {summary['as_of']}")
    lines.append("---")
    lines.append("")
    lines.append("# 判断の答え合わせ（Outcomes）")
    lines.append("")
    lines.append(f"- 集計日: {summary['as_of']}")
    lines.append(f"- 評価できた判断: {summary['n_evaluated']} 件（株価取得不可・中立などで除外: {summary['n_skipped']} 件）")
    lines.append(f"- 総合命中率: {_fmt_rate(summary['overall_hit_rate'])}")
    lines.append("")
    lines.append("> 注: 「H日後ちょうど」の株価ではなく現在株価で判定した近似値。"
                 "経過日数が H 日以上の判断を各バケットに含めています。")
    lines.append("")
    lines.append("## 経過日数バケット別")
    lines.append("")
    lines.append("| 経過 | 件数 | 命中 | 命中率 | 平均リターン |")
    lines.append("|---|---|---|---|---|")
    for h, s in summary["horizons"].items():
        lines.append(f"| {h}日以上 | {s['n']} | {s['hits']} | {_fmt_rate(s['hit_rate'])} | {_fmt_pct(s['avg_return'])} |")
    lines.append("")
    if summary["presets"]:
        lines.append("## スクリーニングpreset別（買い候補の命中率）")
        lines.append("")
        lines.append("| preset | 件数 | 命中 | 命中率 |")
        lines.append("|---|---|---|---|")
        for k, s in sorted(summary["presets"].items(), key=lambda kv: -(kv[1]["hit_rate"] or 0)):
            lines.append(f"| {k} | {s['n']} | {s['hits']} | {_fmt_rate(s['hit_rate'])} |")
        lines.append("")
    if misses:
        lines.append("## 学ぶべき外し（買い判断→大幅下落）")
        lines.append("")
        lines.append("| 銘柄 | 出所 | 記録日 | 記録株価 | 現在株価 | リターン | 経過 |")
        lines.append("|---|---|---|---|---|---|---|")
        for r in misses:
            lines.append(
                f"| {r['symbol']} | {r['source']}({r.get('label','')}) | {r['entry_date']} | "
                f"{r['entry_price']:.2f} | {r['current_price']:.2f} | {_fmt_pct(r['return_pct'])} | {r['days_elapsed']}日 |"
            )
        lines.append("")
    return "\n".join(lines) + "\n"
