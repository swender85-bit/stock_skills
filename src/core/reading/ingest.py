"""取り込みパイプライン（読書台帳仕様 v2 第2部 2-6）— 10ステージ。

| # | ステージ | 失敗時 |
|---|---|---|
| 1 | 受理 | **取得失敗を明示して中断。無音でスキップしない** |
| 2 | 重複検査 | 既存なら「取り込み済み」を返し `ingested_at` を更新しない |
| 3 | 安全走査 | 旗を立てて続行（誤検出で正当な資料が入らない方が損失が大きい） |
| 4 | 本文抽出 | 抽出失敗時は原本のみ保存し `extraction_failed: true` |
| 5 | provenance判定 | 判定不能は「個人発信」 |
| 6 | エンティティ抽出 | 未解決は `entities_unresolved` へ |
| 7 | 日付抽出 | 不明は null（**推測しない**） |
| 8 | stance判定 | 判定不能は「中立」 |
| 9 | 書き込み | 明示的に失敗。**部分書き込みを残さない** |
| 10 | 索引更新 | Neo4j 停止時もファイルは成立。索引は次回同期 |

## 冪等性 — 最も重要な不変条件

同一内容を2回投げた場合、2回目は何も書き込まず
「既に id=src_XXX として取り込み済み（ingested_at: YYYY-MM-DD）」を返す。

> **既知時刻の上書きは、この設計における最も重大なデータ破壊である。**
> 初回に触れた時刻こそが記録すべき値であり、再訪時刻ではない。

## 安全走査は本文抽出より前

抽出処理そのものが攻撃対象になり得るため、走査は**生テキスト**に対して行う。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.core.reading import entities as ent
from src.core.reading import provenance as prov
from src.core.reading import safety, schema, vault

INDEX_FILE = "sources.jsonl"

#: stance 判定の手がかり。**確信が無ければ中立に倒す。**
_SUPPORT_WORDS = ("買い", "強気", "上方修正", "好調", "拡大", "成長加速", "アップグレード",
                  "buy", "outperform", "bullish", "upgrade", "beat")
_CRITICAL_WORDS = ("売り", "弱気", "下方修正", "減益", "不振", "懸念", "リスク", "減配",
                   "ダウングレード", "sell", "underperform", "bearish", "downgrade", "miss")


class IngestError(RuntimeError):
    """取り込みの失敗。**無音で握り潰さない。**"""


def _load_index(root: Path) -> list:
    path = vault.index_path(root, INDEX_FILE)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def find_duplicate(root: Path, hash_value: str) -> Optional[dict]:
    """同一内容が既に取り込まれているか。**id ではなく content_hash で見る。**"""
    for row in _load_index(root):
        if row.get("content_hash") == hash_value:
            return row
    return None


def detect_stance(body: str, entities: list, provenance: str) -> dict:
    """論調を4値で。**一次資料は既定で中立**（原本は論じない）。"""
    if provenance == schema.PRIMARY:
        return {"stance": schema.NEUTRAL, "reason": "一次資料（原本は論じない）"}
    if not entities:
        return {"stance": schema.NEUTRAL, "reason": "保有銘柄への言及なし"}

    text = str(body or "")
    pos = sum(text.count(w) for w in _SUPPORT_WORDS)
    neg = sum(text.count(w) for w in _CRITICAL_WORDS)
    if pos == 0 and neg == 0:
        return {"stance": schema.NEUTRAL, "reason": "論調の手がかりなし"}
    if pos >= neg * 2 and pos >= 2:
        return {"stance": schema.SUPPORT, "reason": f"支持語 {pos} / 批判語 {neg}"}
    if neg >= pos * 2 and neg >= 2:
        return {"stance": schema.CRITICAL, "reason": f"批判語 {neg} / 支持語 {pos}"}
    return {"stance": schema.NEUTRAL, "reason": f"拮抗（支持 {pos} / 批判 {neg}）"}


def ingest(
    *,
    body: str,
    title: str,
    source_url: Optional[str] = None,
    source_type: str = "text",
    attachment: Optional[str] = None,
    note: Optional[str] = None,
    precision: str = schema.EXACT,
    provenance_override: Optional[str] = None,
    config: Optional[dict] = None,
    ingested_at: Optional[datetime] = None,
    dry_run: bool = False,
) -> dict:
    """1件を取り込む。

    Returns
    -------
    dict
        `{"status": "created"|"duplicate"|"dry_run", "id", "path", "frontmatter",
          "security", "messages"}`
    """
    messages: list = []

    # --- 1. 受理 ---------------------------------------------------------
    if not str(body or "").strip():
        raise IngestError("本文が空です。取得に失敗した可能性があります"
                          "（『内容が無かった』と区別してください）。")
    root = vault.require_vault(config)
    vault.ensure_structure(config)

    # --- 2. 重複検査 -----------------------------------------------------
    h = schema.content_hash(body)
    dup = find_duplicate(root, h)
    if dup:
        return {
            "status": "duplicate",
            "id": dup.get("id"),
            "path": dup.get("vault_path"),
            "frontmatter": dup,
            "security": [],
            "messages": [
                f"既に取り込み済みです（id={dup.get('id')} / "
                f"ingested_at={str(dup.get('ingested_at'))[:19]}）。",
                "**既知時刻は上書きしません。** 初回に触れた時刻こそが記録すべき値です。",
            ],
        }

    # --- 3. 安全走査（本文抽出より前・生テキストに対して）------------------
    flags = safety.scan(body, source_url=source_url)
    if flags:
        messages.append(safety.summarize(flags))

    # --- 4. 本文抽出（この実装では受け取った時点で済んでいる）--------------
    text = body

    # --- 5. provenance 判定（自己申告を採用しない）------------------------
    if provenance_override in schema.PROVENANCES:
        pv = {"provenance": provenance_override, "reason": "人間による明示指定",
              "matched": "manual"}
    else:
        pv = prov.classify(source_url=source_url, source_type=source_type, body=text)
    messages.append(f"provenance: {pv['provenance']}（{pv['reason']}）")

    # --- 6. エンティティ抽出 ---------------------------------------------
    ex = ent.extract(text)
    if ex["unresolved"]:
        messages.append(
            f"未解決の記号: {', '.join(ex['unresolved'][:6])}"
            "（捨てずに残しました。名寄せ表に追加すると次回から解決されます）")

    # --- 7. 日付抽出（不明は推測しない）-----------------------------------
    published = schema.parse_published(text)

    # --- 8. stance 判定 ---------------------------------------------------
    st = detect_stance(text, ex["entities"], pv["provenance"])

    fm = schema.build_frontmatter(
        title=title, body=text, provenance=pv["provenance"], source_type=source_type,
        ingested_at=ingested_at, source_url=source_url, attachment=attachment,
        published_at=published, entities=ex["entities"], entity_names_raw=ex["raw"],
        entities_unresolved=ex["unresolved"], stance=st["stance"],
        note=note, security_flags=flags, precision=precision)

    problems = schema.validate(fm, text)
    if problems:
        raise IngestError("検証に失敗しました: " + " / ".join(problems))

    ts = datetime.fromisoformat(fm["ingested_at"])
    path = vault.raw_path(root, ts, fm["provenance"], title)

    if dry_run:
        return {"status": "dry_run", "id": fm["id"], "path": str(path),
                "frontmatter": fm, "security": flags,
                "messages": messages + ["（--dry-run のため書き込んでいません）"]}

    # --- 9. 書き込み（部分書き込みを残さない）------------------------------
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".md.tmp")
    try:
        tmp.write_text(schema.to_markdown(fm, text), encoding="utf-8", newline="\n")
        tmp.replace(path)
    except Exception as exc:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise IngestError(
            f"書き込みに失敗しました: {type(exc).__name__}: {exc}\n"
            "iCloud の同期中ロックの可能性があります。**リトライではなく明示的な失敗**"
            "として扱います（無音の失敗を作らないため）。") from exc

    # --- 10. 索引更新（失敗しても取り込みは成功）---------------------------
    rel = str(path.relative_to(root)).replace("\\", "/")
    index_row = {**{k: fm[k] for k in (
        "id", "ingested_at", "ingested_at_precision", "published_at", "provenance",
        "depth", "title", "source_url", "stance", "entities", "content_hash")},
        "vault_path": rel}
    try:
        idx = vault.index_path(root, INDEX_FILE)
        idx.parent.mkdir(parents=True, exist_ok=True)
        with idx.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(index_row, ensure_ascii=False) + "\n")
    except Exception as exc:
        messages.append(
            f"索引の更新に失敗しました（{type(exc).__name__}）。"
            "**原本は保存されています。** 索引は次回同期で復旧します。")

    messages.append(f"保存しました: {rel}")

    # 保有しているが thesis の無い銘柄に触れたら、書く動線をここで作る。
    # 「机に向かって書くもの」から「何かを読んだ流れで書くもの」へ変える。
    prompt = thesis_prompt(ex["entities"])
    if prompt:
        messages.append(prompt)

    return {"status": "created", "id": fm["id"], "path": rel,
            "frontmatter": fm, "security": flags, "messages": messages}


def thesis_prompt(entities: list) -> Optional[str]:
    """thesis の無い保有銘柄に触れたら、草稿作成を促す（仕様 2-8）。

    **書く動機が発生するのは、読んだ直後だけである。**
    """
    if not entities:
        return None
    try:
        from src.core.portfolio.reconciliation import _load_intent

        missing = []
        held = set(ent.held_symbols())
        for sym in entities:
            if sym not in held:
                continue
            intent = _load_intent(sym, None)
            if not intent.get("has_thesis"):
                missing.append(sym)
    except Exception:
        return None
    if not missing:
        return None
    return (f"💡 {', '.join(missing)} は保有していますが thesis がありません。"
            "いま読んだ流れで書けます: "
            f"`/investment-note save --symbol {missing[0]} --type thesis`")
