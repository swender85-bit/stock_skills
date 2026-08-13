"""読書台帳の vault 層 — パス解決と構造の増設。

## 原則（読書台帳仕様 v2 第2部・第5部）

1. **新しい vault を作らない。** 既存の Obsidian vault に増設する
2. `raw/` は**追記のみ**。Claude は編集も削除もできない
3. `.index/` は機械用。Obsidian が自動で無視するようドットで始める
4. `reports/`（システム出力）と `raw/` `concepts/`（人間の入力）は
   **意味論的に完全に分離する**。自己参照汚染の防止

## 所有権

    vault/raw/       ──[一方向索引]──▶  Neo4j: Source
    vault/concepts/  ──[一方向索引]──▶  Neo4j: Concept
    Neo4j            ──────✕ 逆流禁止 ─────  vault/raw/, vault/concepts/

## Windows / iCloud の実務制約

- パス長 260 文字。階層は `raw/YYYY/MM/` の3段までに固定し、ファイル名は80文字で切る
- `\\ / : * ? " < > |` はファイル名に使えない。`_` へ置換する
- iCloud の「クラウドのみ」状態のファイルは読めない。読む前に実在とサイズを確認する
- 書き込み失敗はリトライせず**明示的にエラー**にする（無音の失敗を作らない）
"""
from __future__ import annotations

import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Optional

#: vault 直下に増設するディレクトリ
RAW_DIR = "raw"
ATTACHMENT_DIR = "raw/_attachments"
CONCEPT_DIR = "concepts"
CONCEPT_ARCHIVE_DIR = "concepts/_archive"
INDEX_DIR = ".index"

#: Windows のファイル名に使えない文字
_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]')
#: ファイル名の最大長（拡張子を除く）。パス長 260 の余裕を取る
MAX_STEM = 80


class VaultUnavailable(RuntimeError):
    """vault が設定されていない、または実在しない。

    **握り潰さない。** 「保存できなかった」を「保存した」と区別するため、
    呼び出し側に必ず伝播させる。
    """


def vault_root(config: Optional[dict] = None) -> Optional[Path]:
    """`config/output.yaml` の `obsidian_vault_path` を返す。

    環境変数 `STOCK_SKILLS_VAULT` が設定されていればそちらを優先する
    （テスト用。本番では未設定）。
    """
    env = os.environ.get("STOCK_SKILLS_VAULT")
    if env:
        return Path(env)
    if config is None:
        try:
            import yaml

            root = Path(__file__).resolve().parents[3]
            config = yaml.safe_load(
                (root / "config" / "output.yaml").read_text(encoding="utf-8")) or {}
        except Exception:
            return None
    path = (config or {}).get("obsidian_vault_path")
    return Path(str(path)) if path else None


def require_vault(config: Optional[dict] = None) -> Path:
    """vault を返す。無ければ例外。**None を返して黙らせない。**"""
    root = vault_root(config)
    if root is None:
        raise VaultUnavailable(
            "vault のパスが設定されていません（config/output.yaml の obsidian_vault_path）。")
    if not root.exists():
        raise VaultUnavailable(
            f"vault が実在しません: {root}\n"
            "iCloud の同期が完了していないか、パスが誤っています。"
            "これは『記録が無い』ではなく『保管庫に届いていない』です。")
    return root


def ensure_structure(config: Optional[dict] = None) -> dict:
    """raw / concepts / .index を増設する。既存には触れない。

    Returns
    -------
    dict
        `{"root", "created": [...], "existing": [...]}`
    """
    root = require_vault(config)
    created, existing = [], []
    for rel in (RAW_DIR, ATTACHMENT_DIR, CONCEPT_DIR, CONCEPT_ARCHIVE_DIR, INDEX_DIR):
        p = root / rel
        if p.exists():
            existing.append(rel)
            continue
        p.mkdir(parents=True, exist_ok=True)
        created.append(rel)

    # `.index/` は人間が見る場所ではない。Obsidian にも無視させる。
    readme = root / INDEX_DIR / "README.md"
    if not readme.exists():
        readme.write_text(
            "# .index\n\n"
            "機械用の中間表現です。人間が編集する場所ではありません。\n"
            "vault/raw/ と vault/concepts/ から一方向に生成され、Neo4j へ同期されます。\n"
            "**ここを編集しても原本は変わりません。** 原本は raw/ と concepts/ です。\n",
            encoding="utf-8")
        created.append(f"{INDEX_DIR}/README.md")

    guide = root / RAW_DIR / "README.md"
    if not guide.exists():
        guide.write_text(
            "# raw — 読んだものの原本\n\n"
            "**追記のみ。** Claude はここのファイルを編集も削除もできません"
            "（`.claude/settings.json` の deny と PreToolUse フックで二重に禁止）。\n\n"
            "- `ingested_at` は「あなたがそれを知った時刻」であり、**遡って記録できません**。\n"
            "- 同じ内容を2回取り込んでも `ingested_at` は上書きされません。\n"
            "  初回に触れた時刻こそが記録すべき値だからです。\n"
            "- 人間の編集は禁止しませんが、`content_hash` の不一致を週次で報告します。\n",
            encoding="utf-8")
        created.append(f"{RAW_DIR}/README.md")

    return {"root": str(root), "created": created, "existing": existing}


def safe_stem(text: str, max_len: int = MAX_STEM) -> str:
    """ファイル名に使える形へ。日本語は残す（Windows で安全）。"""
    s = unicodedata.normalize("NFC", str(text or "")).strip()
    s = _ILLEGAL.sub("_", s)
    s = re.sub(r"\s+", "_", s)
    s = s.strip("._")
    if len(s) > max_len:
        s = s[:max_len].rstrip("._")
    return s or "untitled"


def raw_path(root: Path, ingested_at: datetime, provenance: str, title: str) -> Path:
    """`raw/YYYY/MM/YYYYMMDD_HHMM_<provenance>_<title>.md`"""
    stem = (f"{ingested_at.strftime('%Y%m%d_%H%M')}_"
            f"{safe_stem(provenance, 12)}_{safe_stem(title, 50)}")
    return root / RAW_DIR / ingested_at.strftime("%Y") / ingested_at.strftime("%m") / f"{stem}.md"


def concept_path(root: Path, name: str) -> Path:
    return root / CONCEPT_DIR / f"{safe_stem(name)}.md"


def index_path(root: Path, name: str) -> Path:
    return root / INDEX_DIR / name


def check_readable(path: Path) -> dict:
    """iCloud の「クラウドのみ」状態を検出する。

    ファイルが存在してもサイズ0で内容が落ちてきていない場合があり、
    そのまま読むと**「中身が空だった」と誤読する**。
    """
    if not path.exists():
        return {"ok": False, "reason": f"ファイルがありません: {path}"}
    try:
        size = path.stat().st_size
    except OSError as exc:
        return {"ok": False, "reason": f"サイズを取得できません（{type(exc).__name__}）"}
    if size == 0:
        return {"ok": False,
                "reason": "サイズが0です。iCloud の実体がまだ落ちてきていない可能性があります"
                          "（『中身が空』ではありません）。"}
    return {"ok": True, "size": size}


def health(config: Optional[dict] = None) -> dict:
    """vault 自体の健全性（仕様 10-3）。"""
    try:
        root = require_vault(config)
    except VaultUnavailable as exc:
        return {"available": False, "reason": str(exc)}

    raw_files = list((root / RAW_DIR).rglob("*.md")) if (root / RAW_DIR).exists() else []
    raw_files = [p for p in raw_files if p.name != "README.md"]
    concepts = list((root / CONCEPT_DIR).glob("*.md")) if (root / CONCEPT_DIR).exists() else []
    return {
        "available": True,
        "root": str(root),
        "raw_count": len(raw_files),
        "concept_count": len(concepts),
        "structure_ready": all((root / d).exists()
                               for d in (RAW_DIR, CONCEPT_DIR, INDEX_DIR)),
    }
