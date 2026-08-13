"""保有の「記述状態」— なぜ持つか / どうするか / 何で否定されるか。

## なぜこれがあるか（読書台帳仕様 v2 第1部 1-2 の指摘）

週次レポートの節1と節2は、**内部で矛盾していた**。

- 節1: 「孤児ポジション（thesis も政策も無い）: 0」
- 節1本文: 「保有している全銘柄に『なぜ持っているか』の記述がある」
- 節2: 「thesis は 2件（QCOM / MDT）しか存在しない」

「全銘柄に理由がある」と「8銘柄に thesis が無い」は同時に成立しない。

原因は孤児の定義にある。`_find_orphans` は

    if intent["has_thesis"] or intent["has_policy"]:
        continue        # ← どちらか一方でもあれば孤児ではない

としていた。3xスリーブと FANG+ には保有方針（政策）が登録されているため、
thesis がゼロでも「非孤児」として通過していた。

**しかし意味論的には、政策があって thesis が無い状態こそ危険である。**

> 「下落したら積み増す」というルールはあるが、「なぜこれを持つのか」は書かれていない。

これは**根拠なき機械的執行**であり、孤児より悪い。孤児は「判断基準が無い」と
自覚できるが、政策があると「管理されている」という錯覚が生じる。

## 4状態

============== ================================ ==========================
状態           定義                             意味
============== ================================ ==========================
healthy        thesis + 政策 + 反証条件         なぜ持つか・どうするか・
                                                何で否定されるかが揃う
unruled        thesis あり / 政策なし           理由はあるが行動が未定義
ungrounded     政策あり / thesis なし           ルールはあるが理由がない
orphan         thesis も政策もなし              どちらも未定義
============== ================================ ==========================

**表示すべき数字は「孤児0件」ではなく「健全0件」である。**
この違いだけで節1の意味が反転する。
"""
from __future__ import annotations

from typing import Any, Optional

HEALTHY = "healthy"
UNRULED = "unruled"          # 無方針保有: thesis あり・政策なし
UNGROUNDED = "ungrounded"    # 根拠なき執行: 政策あり・thesis なし
ORPHAN = "orphan"            # 完全孤児

#: 表示順。**健全を先頭に置く**（0件であることが毎週の重要事実）。
ORDER = (HEALTHY, UNRULED, UNGROUNDED, ORPHAN)

LABELS = {
    HEALTHY: "健全（thesis + 政策 + 反証条件）",
    UNRULED: "無方針保有（thesisあり・政策なし）",
    UNGROUNDED: "根拠なき執行（政策あり・thesisなし）",
    ORPHAN: "完全孤児（どちらもなし）",
}

MEANINGS = {
    HEALTHY: "なぜ持つか・どうするか・何で否定されるかが揃っている",
    UNRULED: "理由はあるが、どうなったら動くかが決まっていない",
    UNGROUNDED: "ルールはあるが、なぜ持つかが書かれていない（機械的執行）",
    ORPHAN: "なぜ持つかも、どうするかも未定義",
}


def _has_falsification(theses: list[dict]) -> bool:
    """thesis のどれかに反証条件が付いているか。

    `falsification` が空文字・空配列のときを True にしない。
    **「登録した」と「書いた」を区別する。**
    """
    for t in theses or []:
        raw = t.get("falsification")
        if isinstance(raw, str) and raw.strip():
            return True
        if isinstance(raw, (list, tuple)) and any(
                str(x).strip() for x in raw if x is not None):
            return True
        if isinstance(raw, dict) and raw:
            return True
    return False


def classify_position(intent: dict) -> str:
    """1ポジションの記述状態を返す。

    Parameters
    ----------
    intent:
        `reconciliation._load_intent()` の戻り値。
        `has_thesis` / `has_policy` / `theses` を見る。
    """
    intent = intent or {}
    has_thesis = bool(intent.get("has_thesis"))
    has_policy = bool(intent.get("has_policy"))
    has_fals = _has_falsification(intent.get("theses") or [])

    if has_thesis and has_policy and has_fals:
        return HEALTHY
    if has_thesis and not has_policy:
        return UNRULED
    if has_policy and not has_thesis:
        return UNGROUNDED
    if not has_thesis and not has_policy:
        return ORPHAN
    # thesis + 政策 はあるが反証条件が無い。**健全ではない。**
    # 「何があったら間違いだったと分かるか」が無い状態を健全と呼ばない。
    return UNRULED


def describe(rows: list[dict]) -> dict:
    """保有全体の記述状態を集計する。

    Parameters
    ----------
    rows:
        `{"symbol", "name", "weight_pct", "intent"}` を持つ行の配列。

    Returns
    -------
    dict
        `{"counts", "by_state", "total", "healthy_count", "weight_pct", "messages"}`
    """
    rows = list(rows or [])
    by_state: dict[str, list] = {s: [] for s in ORDER}

    for r in rows:
        state = classify_position(r.get("intent") or {})
        by_state[state].append({
            "symbol": r.get("symbol"),
            "name": r.get("name"),
            "label": r.get("symbol") or r.get("name") or "?",
            "weight_pct": r.get("weight_pct"),
            "account": r.get("account"),
        })

    counts = {s: len(by_state[s]) for s in ORDER}
    weights = {}
    for s in ORDER:
        vals = [x.get("weight_pct") for x in by_state[s]
                if isinstance(x.get("weight_pct"), (int, float))]
        weights[s] = round(sum(vals), 1) if vals else None

    return {
        "total": len(rows),
        "counts": counts,
        "weight_pct": weights,
        "by_state": by_state,
        "healthy_count": counts[HEALTHY],
        "messages": _messages(counts, by_state, len(rows)),
    }


def _names(items: list[dict], limit: int = 8) -> str:
    labels = [str(x.get("label")) for x in items[:limit]]
    more = len(items) - len(labels)
    return ", ".join(labels) + (f" 他{more}件" if more > 0 else "")


def _messages(counts: dict, by_state: dict, total: int) -> list[str]:
    out: list[str] = []
    if not total:
        return ["保有がありません。"]

    if counts[HEALTHY] == 0:
        out.append(
            f"健全なポジションは 0 / {total} です。"
            "**なぜ持つか・どうするか・何で否定されるかが揃った保有は1つもありません。**")
    if counts[UNGROUNDED]:
        out.append(
            f"根拠なき執行が {counts[UNGROUNDED]}件（{_names(by_state[UNGROUNDED])}）。"
            "行動ルールはありますが、なぜ持つかが書かれていません。"
            "**孤児より危険です** — 管理されているという錯覚が生じます。")
    if counts[ORPHAN]:
        out.append(
            f"完全孤児が {counts[ORPHAN]}件（{_names(by_state[ORPHAN])}）。")
    if counts[UNRULED]:
        out.append(
            f"無方針保有が {counts[UNRULED]}件（{_names(by_state[UNRULED])}）。"
            "理由はありますが、反証条件または政策が未定義です。")

    out.append(
        "⚠ 以前「孤児0件」と表示していたのは、判定が「thesis も政策も無い」という"
        "AND条件だったためです。政策があって thesis が無い状態は孤児と数えていませんでした。")
    return out


def format_block(desc: dict) -> str:
    """レポート節1に差し込む表示ブロック。"""
    if not desc or not desc.get("total"):
        return ""
    total = desc["total"]
    lines = ["■ 保有の記述状態", ""]
    for s in ORDER:
        items = desc["by_state"][s]
        n = len(items)
        w = desc["weight_pct"].get(s)
        wtxt = f" / 評価額比 {w}%" if w is not None else ""
        names = f"  {_names(items)}" if items else ""
        lines.append(f"  {LABELS[s]:<34} {n} / {total}{wtxt}{names}")
    lines.append("")
    for m in desc.get("messages") or []:
        lines.append(f"  {m}")
    return "\n".join(lines)
