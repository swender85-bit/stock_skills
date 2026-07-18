#!/usr/bin/env python3
"""PreToolUse ガードフック（KIK / upgrade v1.0 Phase 1-B）

保護領域（.env / 秘密鍵 / data マスター / Obsidian vault の既存ファイル）に対する
「破壊的操作（削除・全上書き）」を物理的にブロックする。

- Write/Edit: 保護対象の *既存* ファイルへの上書き・編集をブロック（新規作成は許可＝追記型運用）
- Bash: rm -rf / リダイレクト(>) / git push --force が保護対象を狙う場合をブロック

クロスプラットフォーム: 標準ライブラリのみ、stdin の JSON を読む。cwd やシェルに依存しない。
ブロック時は exit code 2 + stderr に理由（Claude Code がユーザー/モデルに伝える）。
エラー時は握り潰して exit 0（フック自身が作業を止めない = graceful degradation）。
"""
import json
import os
import re
import sys

try:  # 日本語メッセージが文字化けしないよう stderr を UTF-8 に固定
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# --- 保護対象パスの判定 ---------------------------------------------------

# 絶対パス化した際に、この文字列を含むパスを「保護対象」とみなす（小文字化して比較）
PROTECTED_SUBSTRINGS = [
    os.path.normcase(os.sep + ".env"),                    # .env ファイル
    os.path.normcase(os.sep + "data" + os.sep),           # リポジトリ内 data/
    os.path.normcase(os.sep + "notes" + os.sep),          # 投資メモ
    os.path.normcase(os.sep + "history" + os.sep),        # 売買・分析履歴
    os.path.normcase(os.sep + "watchlists" + os.sep),     # ウォッチリスト
    os.path.normcase("portfolio.csv"),                    # ポートフォリオマスター
    os.path.normcase(os.sep + "iclouddrive" + os.sep + "swender"),  # Obsidian vault（本物）
]

PROTECTED_SUFFIXES = (".env", ".pem", ".key")


def _abs(path: str) -> str:
    try:
        return os.path.normcase(os.path.abspath(path))
    except Exception:
        return os.path.normcase(path or "")


def is_protected(path: str) -> bool:
    if not path:
        return False
    p = _abs(path)
    if p.endswith(PROTECTED_SUFFIXES):
        return True
    return any(sub in p for sub in PROTECTED_SUBSTRINGS)


def file_exists(path: str) -> bool:
    try:
        return os.path.isfile(path)
    except Exception:
        return False


# --- Bash コマンドの破壊性判定 --------------------------------------------

_DESTRUCTIVE_BASH = [
    re.compile(r"\brm\s+-[rf]{1,2}\b"),      # rm -rf / rm -r / rm -f
    re.compile(r"\brmdir\b"),
    re.compile(r"\bgit\s+push\s+.*(--force|-f)\b"),
    re.compile(r">\s*[^>]"),                   # 単一 > によるリダイレクト（>> 追記は除外）
    re.compile(r"\bmv\s+.*\s+/dev/null\b"),
    re.compile(r"\btruncate\b"),
]


def bash_targets_protected(command: str) -> bool:
    """Bash コマンドが破壊的で、かつ保護対象パスを含むか。"""
    if not command:
        return False
    lc = command.lower()
    destructive = any(rx.search(command) for rx in _DESTRUCTIVE_BASH)
    if not destructive:
        return False
    # コマンド文字列中に保護対象を示す語が含まれるか（破壊的コマンド前提なので広めに判定）
    protected_words = [".env", "portfolio.csv", "data/", "data\\",
                       "notes", "history", "watchlist",
                       "iclouddrive", "swender", ".pem", ".key"]
    return any(w in lc for w in protected_words)


def block(reason: str) -> None:
    sys.stderr.write("⛔ ガードフックがブロックしました: " + reason + "\n")
    sys.stderr.write("（保護領域の削除・全上書きは禁止。追記や新規ファイル作成は可能です）\n")
    sys.exit(2)


def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        sys.exit(0)  # 入力が読めなければ何もしない

    tool = data.get("tool_name", "")
    ti = data.get("tool_input", {}) or {}

    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        path = ti.get("file_path") or ti.get("notebook_path") or ""
        if is_protected(path):
            # 新規作成（まだ存在しない）は許可＝追記型運用。既存の上書き/編集はブロック。
            if tool == "Write" and file_exists(path):
                block(f"保護ファイルの全上書き: {path}")
            if tool in ("Edit", "MultiEdit", "NotebookEdit"):
                block(f"保護ファイルの編集: {path}")
        sys.exit(0)

    if tool == "Bash":
        command = ti.get("command", "")
        if bash_targets_protected(command):
            block(f"保護対象への破壊的コマンド: {command[:120]}")
        sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
