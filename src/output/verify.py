"""レポート完了検証（upgrade v1.0 Phase 2）

「生成した」ではなく「届いた」をゴールにするための共通検証関数。
保存済みファイルが以下を満たすか点検する:

  ① ファイルが存在する
  ② サイズ > 0
  ③ Markdown 構造（frontmatter または 見出し #）
  ④ Obsidian 用 frontmatter（tags / created）の付与 … ソート警告

戻り値は dict:
  {
    "ok": bool,          # ①〜③（ハード条件）を全て満たすか
    "path": str,
    "size": int,
    "checks": {"exists": bool, "non_empty": bool, "markdown": bool},
    "warnings": [str, ...],   # ④など、致命的でない指摘
    "errors": [str, ...],
  }
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict


def verify_report(path: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "ok": False,
        "path": path,
        "size": 0,
        "checks": {"exists": False, "non_empty": False, "markdown": True},
        "warnings": [],
        "errors": [],
    }

    # ① 存在
    if not path or not os.path.isfile(path):
        result["errors"].append(f"ファイルが存在しません: {path}")
        return result
    result["checks"]["exists"] = True

    # ② サイズ
    try:
        size = os.path.getsize(path)
    except OSError as e:
        result["errors"].append(f"サイズ取得に失敗: {e}")
        return result
    result["size"] = size
    if size <= 0:
        result["errors"].append("ファイルが空です（サイズ0）")
        return result
    result["checks"]["non_empty"] = True

    # Markdown 以外はここまでで OK
    if not path.lower().endswith(".md"):
        result["ok"] = True
        return result

    # ③ Markdown 構造
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        result["errors"].append(f"読み取りに失敗: {e}")
        return result

    head = text.lstrip()
    has_frontmatter = head.startswith("---")
    has_heading = head.startswith("#") or ("\n#" in text)
    if not has_frontmatter and not has_heading:
        result["checks"]["markdown"] = False
        result["errors"].append("Markdown の体裁（frontmatter か 見出し #）が見当たりません")
        return result

    # ④ Obsidian 用 frontmatter（ソフト警告）
    if has_frontmatter:
        fm = _extract_frontmatter(text)
        if "tags" not in fm:
            result["warnings"].append("frontmatter に tags がありません（Obsidian 推奨）")
        if "created" not in fm:
            result["warnings"].append("frontmatter に created がありません（Obsidian 推奨）")
    else:
        result["warnings"].append("frontmatter がありません（Obsidian の tags/created を推奨）")

    result["ok"] = True
    return result


def _extract_frontmatter(text: str) -> Dict[str, str]:
    """先頭の --- ... --- ブロックから key を抽出（簡易パーサ）。"""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return {}
    keys: Dict[str, str] = {}
    for line in m.group(1).splitlines():
        km = re.match(r"^([A-Za-z_][\w-]*)\s*:", line)
        if km:
            keys[km.group(1)] = line
    return keys
