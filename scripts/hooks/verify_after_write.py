#!/usr/bin/env python3
"""PostToolUse 出力検証フック（upgrade v1.0 Phase 1-B / Phase 2）

output/ および Obsidian vault（投資記録）への Write/Edit 完了後に、
「本当に届いたか」を検証する:
  ① ファイルが存在するか
  ② サイズ > 0 か
  ③ Markdown の場合、frontmatter(---) と 見出し(#) の体裁があるか

検証対象は「レポート生成物」だけ（output/ か vault 配下の .md）。
それ以外のファイル（ソースコード・一時ファイル等）は素通り（exit 0）。

失敗時は exit 2 + stderr で Claude に知らせる（書き込み自体は取り消さない＝警告）。
エラー時は握り潰して exit 0。
"""
import json
import os
import sys

try:  # 日本語メッセージが文字化けしないよう stderr を UTF-8 に固定
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# 検証対象とみなすパスの目印（小文字化して部分一致）
TARGET_SUBSTRINGS = [
    os.sep + "output" + os.sep,
    os.sep + "投資記録" + os.sep,
    os.sep.join(["", "iclouddrive", "swender"]),
]


def _norm(path: str) -> str:
    try:
        return os.path.normcase(os.path.abspath(path))
    except Exception:
        return os.path.normcase(path or "")


def is_target(path: str) -> bool:
    if not path:
        return False
    p = _norm(path)
    return any(sub.lower() in p for sub in TARGET_SUBSTRINGS)


def warn(msg: str) -> None:
    sys.stderr.write("⚠️ 出力検証: " + msg + "\n")
    sys.exit(2)


def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        sys.exit(0)

    tool = data.get("tool_name", "")
    if tool not in ("Write", "Edit", "MultiEdit"):
        sys.exit(0)

    ti = data.get("tool_input", {}) or {}
    path = ti.get("file_path") or ""
    if not is_target(path):
        sys.exit(0)  # 検証対象外は素通り

    # ① 存在
    if not os.path.isfile(path):
        warn(f"ファイルが見つかりません: {path}")
    # ② サイズ
    try:
        size = os.path.getsize(path)
    except Exception:
        size = 0
    if size <= 0:
        warn(f"ファイルが空です（サイズ0）: {path}")

    # ③ Markdown 体裁
    if path.lower().endswith(".md"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except Exception:
            sys.exit(0)  # 読めなければ検証スキップ
        head = text.lstrip()
        has_frontmatter = head.startswith("---")
        has_heading = ("\n#" in text) or head.startswith("#")
        if not has_frontmatter and not has_heading:
            warn(f"Markdown の体裁（frontmatter か 見出し#）が見当たりません: {path}")

    sys.exit(0)


if __name__ == "__main__":
    main()
