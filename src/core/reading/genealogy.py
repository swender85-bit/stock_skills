"""系譜の根（読書台帳仕様 v2 V5）— thesis を地面に接続する。

## 問題

QCOM の thesis 本文には「AIデータセンター参入（FY29売上 $15B超ガイド）」という、
明らかに**何かを読んで得た具体的な数値**が書かれている。

この数値の出所——決算資料か、アナリストレポートか、報道か——は
**どこにも記録されていない。** 系譜を遡ろうとすると、地中で切れている。

案C（系譜会計）は四系譜と深度を持っていたが、**根が地面に刺さっていなかった。**
読書台帳がその接地面になる。

## 何を検出するか

| 検出 | 意味 |
|---|---|
| `no_source` | 根拠として登録された raw が0件。系譜を遡れない |
| `no_primary` | 一次資料（深度0）による裏取りが無い |
| `personal_only` | 個人発信（深度2）のみに依拠している |

**これは信頼度の問題ではなく、独立性の問題である。**
優れた個人発信は劣った業者資料より有用だが、
深度0の裏取りがゼロのテーゼは「独立した確認をしていない」という別の事実を持つ。

## 前提空間HHI との統合

第1弾の Assumption は **Concept の `is_assumption: true`** として実装する
（二重実装を避ける）。複数の thesis が同じ概念に依拠していれば、それが前提の集中である。

**あなたのPFでの具体例**: SOXL・QCOM が「半導体サイクル」に依拠しているなら、
前提空間HHI はセクターHHIより遥かに高く出る。これが「実効半導体207%」の正体を、
価格相関ではなく**信念の共有**として表現したものになる。
"""
from __future__ import annotations

from typing import Optional

from src.core.portfolio.concentration import compute_hhi
from src.core.reading import concepts as cpt
from src.core.reading import diet_audit, schema

NO_SOURCE = "no_source"
NO_PRIMARY = "no_primary"
PERSONAL_ONLY = "personal_only"
GROUNDED = "grounded"

LABELS = {
    NO_SOURCE: "根拠として登録された取り込みが0件（系譜を遡れません）",
    NO_PRIMARY: "一次資料（深度0）による裏取りがありません",
    PERSONAL_ONLY: "個人発信（深度2）のみに依拠しています",
    GROUNDED: "一次資料まで根が届いています",
}


def _sources_by_id(rows: list) -> dict:
    return {str(r.get("id")): r for r in rows or [] if r.get("id")}


def classify_thesis(thesis: dict, sources_by_id: dict) -> dict:
    """1つの thesis の系譜状態。

    thesis 側は `sources: [src_...]` または `related_sources` で raw を指す。
    """
    ids = []
    for key in ("sources", "related_sources", "derived_from"):
        v = thesis.get(key)
        if isinstance(v, (list, tuple)):
            ids.extend(str(x) for x in v)
        elif isinstance(v, str) and v:
            ids.append(v)

    linked = [sources_by_id[i] for i in ids if i in sources_by_id]
    depths = [s.get("depth") for s in linked if s.get("depth") is not None]

    if not linked:
        state = NO_SOURCE
    elif any(d == 0 for d in depths):
        state = GROUNDED
    elif depths and all(d >= 2 for d in depths):
        state = PERSONAL_ONLY
    else:
        state = NO_PRIMARY

    return {
        "symbol": thesis.get("symbol"),
        "created_at": thesis.get("created_at") or thesis.get("date"),
        "state": state,
        "label": LABELS[state],
        "linked_count": len(linked),
        "missing_ids": [i for i in ids if i not in sources_by_id],
        "sources": [{"id": s.get("id"), "title": s.get("title"),
                     "provenance": s.get("provenance"), "depth": s.get("depth")}
                    for s in linked],
    }


def audit_theses(theses: Optional[list] = None,
                 config: Optional[dict] = None) -> dict:
    """全 thesis の系譜状態。

    thesis が0件のとき、**「根が届いている」とは書かない。**
    書く材料が無いのと、根が無いのは別の事実である。
    """
    if theses is None:
        theses = _load_theses()
    loaded = diet_audit.load_sources(config)
    by_id = _sources_by_id(loaded.get("rows") or [])

    rows = [classify_thesis(t, by_id) for t in theses or []]
    counts: dict = {}
    for r in rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1

    if not rows:
        message = ("thesis が1件も登録されていません。"
                   "**これは『系譜が健全』ではなく『根を張る対象が無い』です。**")
    elif counts.get(GROUNDED):
        message = (f"一次資料まで根が届いている thesis: "
                   f"{counts[GROUNDED]} / {len(rows)}")
    else:
        message = (f"{len(rows)}件の thesis のうち、一次資料まで根が届いているものは"
                   "**0件**です。系譜は地中で切れています。")

    return {
        "available": True,
        "total": len(rows),
        "counts": counts,
        "items": rows,
        "grounded_ratio": (counts.get(GROUNDED, 0) / len(rows)) if rows else None,
        "message": message,
        "sources_indexed": len(by_id),
    }


def _load_theses() -> list:
    try:
        from src.data.note_manager import load_notes

        return list(load_notes(note_type="thesis") or [])
    except Exception:
        return []


# --- 前提空間HHI ----------------------------------------------------------


def assumption_concentration(theses: Optional[list] = None,
                             config: Optional[dict] = None) -> dict:
    """thesis が依拠する概念の集中度（前提空間HHI）。

    **セクターが分散していても、前提が1つなら分散していない。**
    価格相関は前提相関の遅行指標にすぎない。
    """
    if theses is None:
        theses = _load_theses()
    concept_rows = [c for c in cpt.load_all(config) if c.get("is_assumption")]
    by_name = {str(c.get("name")): c for c in concept_rows}

    usage: dict = {}
    for t in theses or []:
        for key in ("concepts", "relies_on", "assumptions"):
            v = t.get(key)
            names = v if isinstance(v, (list, tuple)) else ([v] if v else [])
            for n in names:
                n = str(n)
                if n in by_name or not concept_rows:
                    usage.setdefault(n, []).append(t.get("symbol"))

    if not usage:
        return {"available": False,
                "reason": ("thesis が依拠する前提（概念）が記録されていません。"
                           "**これは『前提が分散している』という意味ではありません。**"),
                "concepts_registered": len(concept_rows)}

    total = sum(len(v) for v in usage.values())
    weights = [len(v) / total for v in usage.values()]
    hhi = compute_hhi(weights)
    level = "danger" if hhi >= 0.50 else ("warning" if hhi >= 0.35 else "ok")
    return {
        "available": True,
        "hhi": round(hhi, 3),
        "level": level,
        "by_concept": {k: sorted({s for s in v if s}) for k, v in usage.items()},
        "message": {
            "danger": "一つの物語に全張りです。セクターが分散していてもこれは分散していません。",
            "warning": "特定の前提への依存が大きい状態です。",
            "ok": "前提は分散しています。",
        }[level],
    }


def report(config: Optional[dict] = None) -> dict:
    """節2に載せる系譜サマリ。"""
    theses = _load_theses()
    return {
        "theses": audit_theses(theses, config),
        "assumptions": assumption_concentration(theses, config),
        "concepts": cpt.audit(config),
    }
