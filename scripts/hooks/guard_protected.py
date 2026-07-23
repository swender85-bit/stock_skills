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


def _is_source_tree(path: str) -> bool:
    """`src/` `tests/` 配下はコードであってデータマスターではない。

    PROTECTED_SUBSTRINGS は `/data/` `/history/` `/notes/` を部分一致で見るため、
    素のままだと `src/data/history/...` や `tests/data/test_*.py` のような
    コードファイルまで保護対象に誤判定してしまう（実際に src/data/context/*.py と
    tests/data/*.py の編集がブロックされた）。
    秘密鍵・.env はこの例外より優先されるので、ここでは扱わない。
    """
    parts = _abs(path).split(os.sep)
    return "src" in parts or "tests" in parts


def is_protected(path: str) -> bool:
    if not path:
        return False
    p = _abs(path)
    if p.endswith(PROTECTED_SUFFIXES):
        return True
    if _is_source_tree(p):
        return False
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
    re.compile(r"\bmv\s+.*\s+/dev/null\b"),
    re.compile(r"\btruncate\b"),
]

# 単一 `>` による上書きリダイレクトの「書き込み先トークン」を抽出する。
# - `>>`（追記）は除外 / `2>` `&>`（stderr 系）は除外（先頭に数字や & がある）
# - `>/dev/null` のような無害な破棄先は _PROTECTED_IN_CMD 側で弾かれる
_REDIRECT_TARGET = re.compile(r"(?<![>\d&])>(?!>)\s*(\S+)")


# 保護対象を「パス境界」で判定（例: appdata/ の data/ を誤検知しない）
_PROTECTED_IN_CMD = [
    re.compile(r"(?<![\w.])\.env(?![\w])", re.IGNORECASE),   # .env トークン
    re.compile(r"portfolio\.csv", re.IGNORECASE),
    re.compile(r"[^\s\"']*\.pem(?![\w])", re.IGNORECASE),
    re.compile(r"[^\s\"']*\.key(?![\w])", re.IGNORECASE),
    re.compile(r"(?<![\w])data[\\/]", re.IGNORECASE),         # /data/ だが appdata/ は除外
    re.compile(r"(?<![\w])notes[\\/]", re.IGNORECASE),
    re.compile(r"(?<![\w])history[\\/]", re.IGNORECASE),
    re.compile(r"(?<![\w])watchlists?[\\/]", re.IGNORECASE),
    re.compile(r"(?<![\w])screening_results[\\/]", re.IGNORECASE),
    re.compile(r"iclouddrive", re.IGNORECASE),               # vault
    re.compile(r"swender", re.IGNORECASE),                   # vault（swend 単体は非対象）
    re.compile("投資記録"),                                    # vault サブフォルダ
]


def _token_is_protected(token: str) -> bool:
    return any(rx.search(token) for rx in _PROTECTED_IN_CMD)


def bash_targets_protected(command: str) -> bool:
    """Bash コマンドが保護領域を破壊しうるか。

    2通りで判定:
      1. 破壊的コマンド（rm -rf 等）＋ コマンド中に保護対象パスが出現
      2. 単一 `>` の上書きリダイレクトが、保護対象パスを *書き込み先* にしている
         （`2>/dev/null` や `>/dev/null` のような stderr抑制・破棄は誤検知しない）
    """
    if not command:
        return False

    # 1. 破壊的コマンド × 保護対象パスの共起
    destructive = any(rx.search(command) for rx in _DESTRUCTIVE_BASH)
    if destructive and _token_is_protected(command):
        return True

    # 2. 上書きリダイレクトの書き込み先が保護対象
    for m in _REDIRECT_TARGET.finditer(command):
        if _token_is_protected(m.group(1)):
            return True

    return False


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
