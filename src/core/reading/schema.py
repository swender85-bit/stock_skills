"""raw の frontmatter スキーマと検証（読書台帳仕様 v2 第2部 2-2）。

## 中核の値は `ingested_at`（既知時刻）である

    情報遅延 = ingested_at - published_at

- 中央値が3日なら、あなたは情報の早い側にいる
- 中央値が45日なら、市場に十分織り込まれてから触れている

これは**エッジの構造的性質**であり、逆張り適性・順張り適性を直接規定する。

そして **`ingested_at` は外部データからは絶対に復元できない。**
記録するか、永久に失うかの二択しかない。

## 検証の原則

| フィールド | 規則 |
|---|---|
| `ingested_at` | システム時刻のみ。**ユーザー指定・本文からの抽出を認めない** |
| `provenance` | ドメイン判定で決める。**本文の自己申告を採用しない** |
| `content_hash` | 本文から計算。ユーザー入力を認めない |
| `published_at` | `ingested_at` より未来なら異常 → null + security_flags |

**自己申告を認めないのが要点である。** 「これは公式資料です」と書いてある
個人ブログを一次資料にすると、深度0の錨として扱われ、系譜監査が無意味になる。
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Optional

# --- provenance -----------------------------------------------------------

PRIMARY = "一次資料"
VENDOR = "業者資料"
PRESS = "報道"
PERSONAL = "個人発信"
BOOK = "書籍・教科書"
OWN = "自分の考え"

PROVENANCES = (PRIMARY, VENDOR, PRESS, PERSONAL, BOOK, OWN)

#: 原典からの距離。**信頼度ではない。**
#: 個人発信の深度2は「信用できない」ではなく「原典から2ステップ離れている」。
DEPTH = {PRIMARY: 0, VENDOR: 1, PRESS: 1, PERSONAL: 2, BOOK: 1, OWN: None}

# --- stance ---------------------------------------------------------------

SUPPORT = "支持"
NEUTRAL = "中立"
CRITICAL = "批判"
IRRELEVANT = "無関係"
STANCES = (SUPPORT, NEUTRAL, CRITICAL, IRRELEVANT)

# --- precision ------------------------------------------------------------

EXACT = "exact"
DAY = "day"
RETRO = "retroactive_estimate"
PRECISIONS = (EXACT, DAY, RETRO)

SOURCE_TYPES = ("url", "pdf", "text", "image", "audio_memo")


def content_hash(text: str) -> str:
    """本文のハッシュ。**ユーザー入力を受け付けない。**"""
    digest = hashlib.sha256((text or "").encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def make_id(ingested_at: datetime, hash_value: str) -> str:
    """`src_<YYYYMMDD>_<HHMM>_<hash4>`

    **内容が同一なら id も同一になる** → 重複取り込みの自動検出に使う。
    ただし同一内容でも取り込み時刻が違えば id は変わるので、
    重複検査は id ではなく `content_hash` で行うこと。
    """
    h = hash_value.split(":")[-1][:4]
    return f"src_{ingested_at.strftime('%Y%m%d')}_{ingested_at.strftime('%H%M')}_{h}"


def now() -> datetime:
    """システム時刻（ローカルタイムゾーン付き）。

    **これ以外を `ingested_at` にしてはならない。**
    """
    return datetime.now().astimezone()


_DATE_RE = re.compile(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})")


def parse_published(text: Optional[str]) -> Optional[str]:
    """本文から公開日を拾う。**見つからなければ推測しない（None）。**"""
    if not text:
        return None
    m = _DATE_RE.search(str(text))
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    try:
        return datetime(y, mo, d).date().isoformat()
    except ValueError:
        return None


def build_frontmatter(
    *,
    title: str,
    body: str,
    provenance: str,
    source_type: str,
    ingested_at: Optional[datetime] = None,
    source_url: Optional[str] = None,
    attachment: Optional[str] = None,
    published_at: Optional[str] = None,
    entities: Optional[list] = None,
    entity_names_raw: Optional[list] = None,
    entities_unresolved: Optional[list] = None,
    stance: str = NEUTRAL,
    language: str = "ja",
    author: Optional[str] = None,
    tags: Optional[list] = None,
    note: Optional[str] = None,
    related_thesis: Optional[list] = None,
    security_flags: Optional[list] = None,
    precision: str = EXACT,
    ingest_version: str = "1.0",
) -> dict:
    """検証済みの frontmatter を組み立てる。

    呼び出し側が何を渡しても、**システム管理フィールドは上書きされる**。
    """
    ts = ingested_at or now()
    h = content_hash(body)

    if provenance not in PROVENANCES:
        provenance = PERSONAL      # 不明は最も深い側に倒す（2-4 の原則）
    if stance not in STANCES:
        stance = NEUTRAL
    if source_type not in SOURCE_TYPES:
        source_type = "text"
    if precision not in PRECISIONS:
        precision = EXACT

    flags = list(security_flags or [])

    # published_at が ingested_at より未来なら異常。**捨てて記録する。**
    if published_at:
        try:
            if datetime.fromisoformat(str(published_at)).date() > ts.date():
                flags.append({
                    "kind": "future_published_at",
                    "detail": f"公開日 {published_at} が取り込み日 {ts.date()} より未来です",
                })
                published_at = None
        except ValueError:
            published_at = None

    return {
        "id": make_id(ts, h),
        "ingested_at": ts.isoformat(timespec="seconds"),
        "ingested_at_precision": precision,
        "provenance": provenance,
        "depth": DEPTH.get(provenance),
        "title": str(title or "").strip() or "(無題)",
        "source_type": source_type,
        "source_url": source_url,
        "attachment": attachment,
        "published_at": published_at,
        "entities": list(entities or []),
        "entity_names_raw": list(entity_names_raw or []),
        "entities_unresolved": list(entities_unresolved or []),
        "concepts": [],
        "stance": stance,
        "language": language,
        "author": author,
        "tags": list(tags or []),
        "note": note,
        "related_thesis": list(related_thesis or []),
        "content_hash": h,
        "ingest_version": ingest_version,
        "security_flags": flags,
    }


def validate(fm: dict, body: str) -> list:
    """検証違反を列挙する。**空リストなら合格。**"""
    problems = []
    fm = fm or {}

    if not fm.get("ingested_at"):
        problems.append("ingested_at がありません（既知時刻は復元不能な中核データです）")
    if fm.get("provenance") not in PROVENANCES:
        problems.append(f"provenance が不正です: {fm.get('provenance')}")
    if fm.get("stance") not in STANCES:
        problems.append(f"stance が不正です: {fm.get('stance')}")
    if fm.get("source_type") == "url" and not fm.get("source_url"):
        problems.append("source_type が url なのに source_url がありません")
    if fm.get("source_type") in ("pdf", "image") and not fm.get("attachment"):
        problems.append(f"source_type が {fm.get('source_type')} なのに attachment がありません")
    if fm.get("content_hash") != content_hash(body):
        problems.append("content_hash が本文と一致しません（原本が編集された可能性）")
    return problems


def to_markdown(fm: dict, body: str) -> str:
    """frontmatter + 本文の Markdown。改行は LF に統一する。"""
    import yaml

    dumped = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False,
                            default_flow_style=False)
    text = f"---\n{dumped}---\n\n{body or ''}\n"
    return text.replace("\r\n", "\n")


def parse_markdown(text: str) -> tuple:
    """`(frontmatter, body)` に分解する。frontmatter が無ければ `({}, text)`。"""
    import yaml

    raw = (text or "").replace("\r\n", "\n")
    if not raw.startswith("---\n"):
        return {}, raw
    end = raw.find("\n---\n", 4)
    if end == -1:
        return {}, raw
    head = raw[4:end]
    body = raw[end + 5:]
    try:
        fm = yaml.safe_load(head) or {}
    except Exception:
        return {}, raw
    return (fm if isinstance(fm, dict) else {}), body.lstrip("\n")
