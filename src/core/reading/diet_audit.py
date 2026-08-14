"""偏食監査（読書台帳仕様 v2 第4部）— 5指標。

すべて `raw/` の frontmatter だけで計算できる。**追加のデータ取得を要さない。**
価格が全滅した週でも動く、システム内で唯一の分析層である。

## 最小サンプルの絶対規則

**未達の指標は、値を表示しない。**「蓄積中（現在N件 / 必要M件）」とだけ出す。
少ないサンプルから結論を出すことが、この種の自己分析における最大の失敗である。

## トーン（仕様 4-3）

偏食監査は、扱いを誤ると**説教装置**になり、記録行為そのものをやめさせる。

- 禁止: 「〜すべきです」「〜できていません」「偏っています」（単独評価語）、目標値の提示
- 使う: 事実＋含意、比較による提示、議題化

常設する一文:

> 偏りは人間の標準であり、異常ではない。この節は矯正のためではなく、
> **自分の形を知るため**にある。
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from src.core.portfolio.concentration import compute_hhi   # 新規実装せず共用（仕様 4-1c）
from src.core.reading import entities as ent
from src.core.reading import schema, vault

DEFAULT_WINDOW_DAYS = 90

MIN_SAMPLES = {
    "holding_bias": 20,
    "stance_asymmetry": 5,        # 銘柄あたり
    "source_hhi": 30,
    "dormant_ideas": 10,          # 各群
    "idea_to_execution": 8,
}

DISCLAIMER = ("偏りは人間の標準であり、異常ではない。"
              "この節は矯正のためではなく、**自分の形を知るため**にある。")


def load_sources(config: Optional[dict] = None, days: Optional[int] = None) -> dict:
    """索引から raw の frontmatter を読む。

    索引が無ければ `raw/` を直接走査する（索引はあくまで高速化のためのもので、
    **原本がマスター**である）。
    """
    try:
        root = vault.require_vault(config)
    except vault.VaultUnavailable as exc:
        return {"available": False, "reason": str(exc), "rows": []}

    rows: list = []
    idx = vault.index_path(root, "sources.jsonl")
    if idx.exists():
        for line in idx.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    else:
        raw_dir = root / vault.RAW_DIR
        if raw_dir.exists():
            for p in raw_dir.rglob("*.md"):
                if p.name == "README.md":
                    continue
                try:
                    fm, _ = schema.parse_markdown(p.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if fm:
                    rows.append(fm)

    if days:
        cutoff = datetime.now().astimezone() - timedelta(days=days)
        kept = []
        for r in rows:
            ts = _dt(r.get("ingested_at"))
            if ts is None or ts >= cutoff:
                kept.append(r)
        rows = kept

    return {"available": True, "root": str(root), "rows": rows}


def _dt(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        d = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _pending(name: str, have: int) -> dict:
    need = MIN_SAMPLES[name]
    return {"available": False, "status": "accumulating",
            "have": have, "need": need,
            "message": f"蓄積中（現在 {have}件 / 必要 {need}件）"}


# --- (a) 保有偏重率 --------------------------------------------------------


def holding_bias(rows: list, held: Optional[list] = None) -> dict:
    """保有銘柄に言及する raw の比率。

    分母から `entities` が空の raw（一般法則・マクロ等）を除く。
    """
    held = set(held if held is not None else ent.held_symbols())
    with_entities = [r for r in rows if r.get("entities")]
    if len(with_entities) < MIN_SAMPLES["holding_bias"]:
        return _pending("holding_bias", len(with_entities))

    hit = [r for r in with_entities if set(r.get("entities") or []) & held]
    ratio = len(hit) / len(with_entities)
    if ratio >= 0.70:
        note = "新しい機会に触れた記録が少ない状態です。"
    elif ratio >= 0.50:
        note = "標準的な範囲です。"
    else:
        note = "保有外への探索が記録されています。"
    return {"available": True, "value": round(ratio * 100, 1),
            "hit": len(hit), "total": len(with_entities), "message": note}


# --- (b) 論調非対称 --------------------------------------------------------


def stance_asymmetry(rows: list, held: Optional[list] = None) -> dict:
    """保有銘柄ごとの支持/批判。

    🔴 **最も重要な出力は比率ではなく「批判ゼロの保有」の列挙である。**
    批判0件を比率にすると分母1で歪むため、別枠で扱う。
    """
    held = list(held if held is not None else ent.held_symbols())
    per: dict = {}
    for sym in held:
        related = [r for r in rows
                   if sym in (r.get("entities") or [])
                   and r.get("stance") != schema.IRRELEVANT]
        support = sum(1 for r in related if r.get("stance") == schema.SUPPORT)
        critical = sum(1 for r in related if r.get("stance") == schema.CRITICAL)
        per[sym] = {"total": len(related), "support": support, "critical": critical,
                    "enough": len(related) >= MIN_SAMPLES["stance_asymmetry"]}

    # 🔴 「その銘柄について1件も読んでいない」を「批判が0件」と報告しない。
    #
    # 取り込みがゼロの時点で全銘柄を zero_critical に並べると、
    # **未測定が「批判的材料を避けている」という所見に化ける。**
    # これはこのシステムが最も繰り返してきた誤りの形（取得失敗を0と書く）である。
    # 批判ゼロを主張できるのは、その銘柄について**何かは読んでいる**ときだけ。
    zero_critical = [s for s, v in per.items() if v["total"] > 0 and v["critical"] == 0]
    unmeasured = [s for s, v in per.items() if v["total"] == 0]
    ratios = {s: round(v["support"] / max(v["critical"], 1), 2)
              for s, v in per.items() if v["enough"] and v["critical"] > 0}

    if zero_critical:
        message = (f"批判的な材料が0件の保有: {', '.join(zero_critical)}"
                   "（読んではいるが、否定的な材料の記録がない状態です）")
    elif unmeasured and not any(v["total"] for v in per.values()):
        message = ("保有銘柄について取り込んだ材料がまだありません。"
                   "**これは『批判ゼロ』ではなく『未測定』です。**")
    else:
        message = "読んだ記録のある保有には、批判的な材料も記録されています。"

    return {
        "available": True,
        "per_symbol": per,
        "ratios": ratios,
        "zero_critical": zero_critical,
        "unmeasured": unmeasured,
        "message": message,
    }


# --- (c) 情報源HHI ---------------------------------------------------------


def source_hhi(rows: list) -> dict:
    """発信源の集中度。**ポートフォリオHHIと同一モジュールを使う。**

    銘柄HHIと並べて表示することに意味がある。
    「銘柄HHI 0.14 / 情報源HHI 0.51」なら、
    **銘柄は分散しているが思考は分散していない**という状態が一目で分かる。
    """
    if len(rows) < MIN_SAMPLES["source_hhi"]:
        return _pending("source_hhi", len(rows))

    counts: dict = {}
    for r in rows:
        key = _source_key(r)
        counts[key] = counts.get(key, 0) + 1
    total = sum(counts.values())
    weights = [c / total for c in counts.values()]
    hhi = compute_hhi(weights)

    if hhi >= 0.25:
        label = "集中"
    elif hhi >= 0.15:
        label = "標準"
    else:
        label = "分散"
    top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:5]
    return {"available": True, "value": round(hhi, 3), "label": label,
            "distinct_sources": len(counts), "total": total,
            "top": [{"source": k, "count": v} for k, v in top]}


def _source_key(row: dict) -> str:
    url = row.get("source_url")
    if url:
        try:
            host = urlparse(str(url)).hostname or ""
            if host:
                return host.lower()
        except Exception:
            pass
    if row.get("author"):
        return str(row["author"])
    return f"（{row.get('provenance') or '不明'}・URLなし）"


# --- (d) 死蔵アイデアの成績 -------------------------------------------------


def dormant_ideas(rows: list, held: Optional[list] = None,
                  traded_symbols: Optional[list] = None) -> dict:
    """読んだが手を出さなかった銘柄の一覧。

    価格を要する成績算出はここでは行わない（打ち切り週でも動く層に留めるため）。
    **価格が必要な部分は呼び出し側で足す。**
    """
    held = set(held if held is not None else ent.held_symbols())
    traded = set(traded_symbols or [])
    mentioned: dict = {}
    for r in rows:
        ts = _dt(r.get("ingested_at"))
        for sym in r.get("entities") or []:
            if sym in held or sym in traded:
                continue
            first = mentioned.get(sym)
            if first is None or (ts and ts < first):
                mentioned[sym] = ts

    items = [{"symbol": s, "first_seen": t.isoformat() if t else None}
             for s, t in sorted(mentioned.items())]
    if len(items) < MIN_SAMPLES["dormant_ideas"]:
        return {**_pending("dormant_ideas", len(items)), "items": items}
    return {"available": True, "count": len(items), "items": items,
            "message": "読んだが手を出していない銘柄です。成績の算出には価格が要ります。"}


# --- (e) 着想→執行の遅延 ---------------------------------------------------


def idea_to_execution(rows: list, trades: Optional[list] = None) -> dict:
    """初めて読んでから買うまでの日数。

    `ingested_at_precision: retroactive_estimate` の raw は**除外する**。
    推定値で統計を汚さない。
    """
    trades = trades or []
    first_seen: dict = {}
    for r in rows:
        if r.get("ingested_at_precision") == schema.RETRO:
            continue
        ts = _dt(r.get("ingested_at"))
        if ts is None:
            continue
        for sym in r.get("entities") or []:
            if sym not in first_seen or ts < first_seen[sym]:
                first_seen[sym] = ts

    delays = []
    for t in trades:
        sym = ent.canonical(t.get("symbol") or "") or t.get("symbol")
        bought = _dt(t.get("date") or t.get("traded_at"))
        seen = first_seen.get(sym)
        if sym and bought and seen and bought >= seen:
            delays.append({"symbol": sym, "days": (bought - seen).days})

    if len(delays) < MIN_SAMPLES["idea_to_execution"]:
        return {**_pending("idea_to_execution", len(delays)), "items": delays}
    values = [d["days"] for d in delays]
    return {"available": True, "count": len(values),
            "median_days": statistics.median(values),
            "items": delays}


# --- 情報遅延（既知と公開の差）---------------------------------------------


def information_delay(rows: list) -> dict:
    """`ingested_at - published_at` の中央値。

    **これはエッジの構造的性質である。** 中央値が3日なら情報の早い側、
    45日なら市場に十分織り込まれてから触れている。
    逆張り適性・順張り適性を直接規定する。
    """
    deltas = []
    for r in rows:
        if r.get("ingested_at_precision") == schema.RETRO:
            continue      # 推定値で統計を汚さない
        ing = _dt(r.get("ingested_at"))
        pub = r.get("published_at")
        if not ing or not pub:
            continue
        try:
            pubd = datetime.fromisoformat(str(pub))
        except ValueError:
            continue
        if pubd.tzinfo is None:
            pubd = pubd.replace(tzinfo=ing.tzinfo)
        days = (ing - pubd).days
        if days >= 0:
            deltas.append(days)
    if len(deltas) < 5:
        return {"available": False, "status": "accumulating",
                "have": len(deltas), "need": 5,
                "message": f"蓄積中（現在 {len(deltas)}件 / 必要 5件）"}
    return {"available": True, "median_days": statistics.median(deltas),
            "count": len(deltas)}


# --- 全体 -----------------------------------------------------------------


def audit(config: Optional[dict] = None, days: int = DEFAULT_WINDOW_DAYS,
          trades: Optional[list] = None) -> dict:
    """5指標をまとめて返す。価格に依存しないので、打ち切り週でも動く。"""
    loaded = load_sources(config, days=days)
    if not loaded.get("available"):
        return {"available": False, "reason": loaded.get("reason"),
                "disclaimer": DISCLAIMER}

    rows = loaded["rows"]
    held = ent.held_symbols()
    return {
        "available": True,
        "window_days": days,
        "total_sources": len(rows),
        "holding_bias": holding_bias(rows, held),
        "stance_asymmetry": stance_asymmetry(rows, held),
        "source_hhi": source_hhi(rows),
        "dormant_ideas": dormant_ideas(rows, held),
        "idea_to_execution": idea_to_execution(rows, trades),
        "information_delay": information_delay(rows),
        "provenance_mix": _provenance_mix(rows),
        "disclaimer": DISCLAIMER,
    }


def _provenance_mix(rows: list) -> dict:
    counts: dict = {}
    for r in rows:
        p = r.get("provenance") or "不明"
        counts[p] = counts.get(p, 0) + 1
    total = sum(counts.values())
    primary = counts.get(schema.PRIMARY, 0)
    own = counts.get(schema.OWN, 0)
    return {
        "counts": counts,
        "total": total,
        "primary_pct": round(primary / total * 100, 1) if total else None,
        "own_pct": round(own / total * 100, 1) if total else None,
    }
