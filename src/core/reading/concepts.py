"""概念層（読書台帳仕様 v2 第3部）— 銘柄の死を超えて残る知識。

## 何が概念で、何が概念でないか

概念は3条件を**すべて**満たす。

1. **銘柄を売っても価値が残る** — 特定銘柄への依存がない
2. **複数の対象に適用できる** — 少なくとも2つの銘柄・状況に当てはまる
3. **反証できる** — 「これが当てはまらないケース」を書ける

| 例 | 判定 | 正しい置き場 |
|---|---|---|
| 「QCOM は割安」 | ✗ 銘柄依存・時点依存 | thesis |
| 「9/1 に MDT が決算」 | ✗ 日程 | Event |
| 「下落したら積み増す」 | ✗ 行動ルール | 政策 |
| 「レバレッジETFはボラティリティで逓減する」 | ✓ | **概念** |

## 「反証事例」を空のまま登録できない理由

反証事例を書けない概念は、**反証不能な信念**である。信念は概念層ではなく
thesis に置くべきであり、概念として plan-check の制約に使ってはならない。

**この必須化が、概念層が教条の倉庫になることを防ぐ唯一の装置である。**

## confidence のライフサイクル

    仮説 ──(適用2回以上 かつ 機能率60%以上)──▶ 検証済
    検証済 ──(適用3回以上 かつ 機能率40%未満)──▶ 疑わしい
    疑わしい ──(直近3回の機能率60%以上)──▶ 検証済
    任意 ──(人間の明示的決定のみ)──▶ 廃止

🔴 **廃止は自動化しない。** 概念の廃止は知識の放棄であり、統計が悪いだけで
捨てるのは早計である（サンプルが小さい）。自動化するのは降格までとする。

制約として使えるのは `検証済` のみ。第2弾・案C（深度による使用資格の制御）と同じ原理。
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Optional

from src.core.reading import schema, vault

# --- confidence -----------------------------------------------------------

HYPOTHESIS = "仮説"
VERIFIED = "検証済"
DOUBTFUL = "疑わしい"
RETIRED = "廃止"
CONFIDENCES = (HYPOTHESIS, VERIFIED, DOUBTFUL, RETIRED)

#: plan-check の制約になれる confidence
USABLE_AS_CONSTRAINT = (VERIFIED,)

# --- カテゴリ -------------------------------------------------------------

MECHANISM = "業界メカニズム"
INSTRUMENT = "商品構造"
TECHNIQUE = "会計・分析技法"
LAW = "一般法則"
CATEGORIES = (MECHANISM, INSTRUMENT, TECHNIQUE, LAW)

#: 概念数の目安（仕様 3-5）
COUNT_GUIDANCE = ((10, "立ち上げ期"), (30, "健全"), (50, "要整理"))
COUNT_WARN = 50

#: 昇格・降格の閾値
PROMOTE_MIN_APPLICATIONS = 2
PROMOTE_MIN_RATE = 0.60
DEMOTE_MIN_APPLICATIONS = 3
DEMOTE_MAX_RATE = 0.40


class ConceptError(ValueError):
    """概念の登録・更新の失敗。**握り潰さない。**"""


def slug(name: str) -> str:
    s = re.sub(r"[^\w぀-ヿ一-鿿]+", "_", str(name or "").strip())
    return f"cpt_{s.strip('_').lower()}" or "cpt_unnamed"


def build(
    *,
    name: str,
    category: str,
    body: str,
    counterexample: str,
    aliases: Optional[list] = None,
    applies_to: Optional[list] = None,
    sources: Optional[list] = None,
    related_concepts: Optional[list] = None,
    is_assumption: bool = False,
    open_questions: Optional[str] = None,
) -> dict:
    """概念ページの frontmatter + 本文を組み立てる。

    Raises
    ------
    ConceptError
        反証事例が空のとき。**これは仕様であって不便ではない。**
    """
    if not str(name or "").strip():
        raise ConceptError("概念名がありません。")
    if category not in CATEGORIES:
        raise ConceptError(f"カテゴリが不正です: {category}（{'/'.join(CATEGORIES)}）")
    if not str(body or "").strip():
        raise ConceptError(
            "内容が空です。**自分の言葉で説明できないものを概念にしない**"
            "（取り込んだ本文の貼り付けでもなく、要約でもなく、説明の再構成）。")
    if not str(counterexample or "").strip():
        raise ConceptError(
            "反証事例が空です。**反証事例を書けない概念は反証不能な信念であり、"
            "概念層ではなく thesis に置くべきものです。** "
            "『この概念が当てはまらなかったケース』を1つ書いてください。")

    today = date.today().isoformat()
    fm = {
        "type": "concept",
        "id": slug(name),
        "name": str(name).strip(),
        "aliases": list(aliases or []),
        "category": category,
        "confidence": HYPOTHESIS,      # 新規は必ず仮説から始まる
        "is_assumption": bool(is_assumption),
        "application_count": 0,
        "success_count": 0,
        "created_at": today,
        "updated_at": today,
        "applies_to": list(applies_to or []),
        "sources": list(sources or []),
        "related_concepts": list(related_concepts or []),
        "supersedes": [],
    }
    text = (
        "## 内容\n\n" + body.strip() + "\n\n"
        "## 適用実績\n\n"
        "| 日付 | 対象 | 使い方 | 結果 |\n|---|---|---|---|\n\n"
        "## 反証事例\n\n" + counterexample.strip() + "\n\n"
        "## 未解決の問い\n\n" + (open_questions or "（まだ分かっていないこと）").strip() + "\n\n"
        "## 根拠\n\n"
        + ("\n".join(f"- [[{s}]]" for s in (sources or [])) or "（未登録）") + "\n"
    )
    return {"frontmatter": fm, "body": text}


def save(concept: dict, config: Optional[dict] = None) -> str:
    """概念ページを vault へ書く。既存があれば内容を差し替える（概念は編集可）。"""
    root = vault.require_vault(config)
    vault.ensure_structure(config)
    fm = concept["frontmatter"]
    path = vault.concept_path(root, fm["name"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(schema.to_markdown(fm, concept["body"]),
                    encoding="utf-8", newline="\n")
    return str(path.relative_to(root)).replace("\\", "/")


def load_all(config: Optional[dict] = None) -> list:
    """vault 上の全概念。`_archive/` は含めない（廃止済みは検索対象外）。"""
    try:
        root = vault.require_vault(config)
    except vault.VaultUnavailable:
        return []
    d = root / vault.CONCEPT_DIR
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.md")):
        try:
            fm, body = schema.parse_markdown(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if fm.get("type") == "concept":
            fm["_path"] = str(p.relative_to(root)).replace("\\", "/")
            fm["_body"] = body
            out.append(fm)
    return out


# --- 重複防止 -------------------------------------------------------------


def find_similar(name: str, existing: Optional[list] = None,
                 config: Optional[dict] = None) -> list:
    """近い概念を探す。**作成前に必ず呼ぶ。**

    概念層の最大の失敗モードは、同じことを別名で何度も作ることである。
    """
    rows = existing if existing is not None else load_all(config)
    target = str(name or "").strip()
    if not target:
        return []
    hits = []
    for c in rows:
        names = [str(c.get("name") or "")] + [str(a) for a in c.get("aliases") or []]
        for n in names:
            if not n:
                continue
            if n == target:
                hits.append({"concept": c, "reason": "完全一致", "score": 1.0})
                break
            if n in target or target in n:
                hits.append({"concept": c, "reason": f"部分一致（{n}）", "score": 0.7})
                break
            common = set(n) & set(target)
            if len(common) >= max(2, min(len(n), len(target)) * 0.6):
                hits.append({"concept": c, "reason": f"字面が近い（{n}）", "score": 0.4})
                break
    return sorted(hits, key=lambda h: h["score"], reverse=True)


def count_guidance(n: int) -> dict:
    label = "粒度が細かすぎる可能性。統合を検討"
    for threshold, text in COUNT_GUIDANCE:
        if n <= threshold:
            label = text
            break
    return {"count": n, "label": label, "warn": n > COUNT_WARN}


# --- confidence の遷移 -----------------------------------------------------


def success_rate(concept: dict) -> Optional[float]:
    n = concept.get("application_count") or 0
    if not n:
        return None
    return (concept.get("success_count") or 0) / n


def next_confidence(concept: dict) -> dict:
    """週次評価での自動遷移を決める。**廃止は返さない。**

    Returns
    -------
    dict
        `{"from", "to", "changed", "reason"}`
    """
    current = concept.get("confidence") or HYPOTHESIS
    n = concept.get("application_count") or 0
    rate = success_rate(concept)

    if current == RETIRED:
        return {"from": current, "to": current, "changed": False,
                "reason": "廃止は人間の決定であり、自動では戻さない"}

    if rate is None:
        return {"from": current, "to": current, "changed": False,
                "reason": f"適用実績なし（{n}回）。判定しない"}

    if current == HYPOTHESIS and n >= PROMOTE_MIN_APPLICATIONS and rate >= PROMOTE_MIN_RATE:
        return {"from": current, "to": VERIFIED, "changed": True,
                "reason": f"適用{n}回・機能率{rate:.0%} → 昇格"}
    if current == VERIFIED and n >= DEMOTE_MIN_APPLICATIONS and rate < DEMOTE_MAX_RATE:
        return {"from": current, "to": DOUBTFUL, "changed": True,
                "reason": f"適用{n}回・機能率{rate:.0%} → 降格（制約から外れる）"}
    if current == DOUBTFUL and n >= DEMOTE_MIN_APPLICATIONS and rate >= PROMOTE_MIN_RATE:
        return {"from": current, "to": VERIFIED, "changed": True,
                "reason": f"機能率{rate:.0%}に回復 → 復帰"}
    return {"from": current, "to": current, "changed": False,
            "reason": f"適用{n}回・機能率{rate:.0%}。遷移条件を満たさない"}


def usable_as_constraint(concept: dict) -> dict:
    """plan-check の制約として使えるか。

    単一 raw のみを根拠とする概念は、**検証済であっても仮説扱いに固定する**
    （仕様 6-3-3: 緩慢な汚染への構造的防御）。
    """
    conf = concept.get("confidence")
    sources = concept.get("sources") or []
    if conf not in USABLE_AS_CONSTRAINT:
        return {"usable": False,
                "reason": f"confidence が {conf} です（制約に使えるのは {VERIFIED} のみ）"}
    if len(sources) <= 1 and not (concept.get("application_count") or 0):
        return {"usable": False,
                "reason": ("根拠が単一の取り込みのみで、適用実績もありません。"
                           "外部から注入された知識をそのまま制約にしないため保留します。")}
    return {"usable": True, "reason": None}


def audit(config: Optional[dict] = None) -> dict:
    """概念層の健全性。週次レポート節2で使う。"""
    rows = load_all(config)
    transitions = []
    for c in rows:
        t = next_confidence(c)
        if t["changed"]:
            transitions.append({"name": c.get("name"), **t})

    by_conf: dict = {}
    for c in rows:
        k = c.get("confidence") or HYPOTHESIS
        by_conf[k] = by_conf.get(k, 0) + 1

    single_source = [c.get("name") for c in rows
                     if len(c.get("sources") or []) <= 1]
    personal_only = [c.get("name") for c in rows if c.get("_depth2_only")]

    return {
        "available": True,
        "count": len(rows),
        "guidance": count_guidance(len(rows)),
        "by_confidence": by_conf,
        "transitions": transitions,
        "single_source": single_source,
        "personal_only": personal_only,
        "constraint_ready": [c.get("name") for c in rows
                             if usable_as_constraint(c)["usable"]],
    }
