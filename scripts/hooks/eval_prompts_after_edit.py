#!/usr/bin/env python3
"""PostToolUse: `.claude/prompts/*.md` を触ったら synthesis の質を見る (改善1).

## 何をするか

1. **無料の回帰**（常に実行）— `pytest tests/synthesis -q`。
   評価軸そのもの（assertions.py と fixture）が壊れていないかを4秒で見る。
2. **有料の eval**（既定では実行しない）— `eval_synthesis.py --changed`。
   実際に `claude -p` を呼んで文章を書かせ、8原則の検査をかける。

## なぜ有料側を既定で走らせないか

**PostToolUse hook の予算は 10 秒。`claude -p` は1節あたり数十秒かかる。**
どう書いても hook の中では完走できない。加えて、プロンプトを保存するたびに
黙って課金が発生するのは既定として悪い。

そこで既定は「無料の回帰を回し、有料の eval は**コマンドを提示して予約する**」。
実際に走らせたいときだけ環境変数で切り替える:

    SYNTHESIS_EVAL_ON_EDIT=off     … 何もしない
    SYNTHESIS_EVAL_ON_EDIT=smoke   … 既定。無料回帰＋予約マーカー
    SYNTHESIS_EVAL_ON_EDIT=async   … 有料 eval をデタッチして起動（結果は後でファイルに出る）

予約マーカー: `data/synthesis_evals/pending.json`
（次の週次実行・手動確認で「プロンプトを触ったのに eval していない」が見える）

失敗しても編集は取り消さない（exit 0 か 2 の警告のみ）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

REPO = Path(__file__).resolve().parent.parent.parent
PENDING = REPO / "data" / "synthesis_evals" / "pending.json"

def is_prompt(path: str) -> bool:
    """`.claude/prompts/*.md` か。相対パスで渡されても拾う。"""
    if not path or not path.lower().endswith(".md"):
        return False
    normalized = os.path.normpath(path).replace("\\", "/").lower()
    return normalized.startswith(".claude/prompts/") or "/.claude/prompts/" in normalized


def record_pending(path: str, mode: str) -> None:
    """『プロンプトを触ったのに eval していない』を残す。

    ここを黙って捨てると、質の回帰が誰にも見えないまま週次が回り続ける。
    """
    try:
        PENDING.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if PENDING.exists():
            try:
                data = json.loads(PENDING.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        entries = data.get("entries") or []
        entries.append({
            "prompt": os.path.basename(path),
            "edited_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "mode": mode,
        })
        data["entries"] = entries[-50:]
        data["note"] = ("プロンプト編集後の synthesis eval が未実施です。"
                        "`python scripts/eval_synthesis.py --changed` で解消できます。")
        PENDING.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def run_free_regression() -> tuple[bool, str]:
    """assertions.py と fixture の回帰。API を叩かないので毎回回せる。"""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/synthesis", "-q",
             "-p", "no:cacheprovider", "--no-header"],
            cwd=str(REPO), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
    except Exception as exc:
        return True, f"（回帰テストを実行できませんでした: {type(exc).__name__}）"
    tail = [l for l in (proc.stdout or "").splitlines() if l.strip()][-1:]
    return proc.returncode == 0, (tail[0] if tail else "")


def fire_async_eval() -> None:
    """有料 eval をデタッチ起動する。hook は待たない。"""
    cmd = [sys.executable, str(REPO / "scripts" / "eval_synthesis.py"), "--changed"]
    kwargs: dict = {"cwd": str(REPO), "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL, "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | \
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(cmd, **kwargs)
    except Exception:
        pass


def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        sys.exit(0)

    if data.get("tool_name", "") not in ("Write", "Edit", "MultiEdit"):
        sys.exit(0)
    path = (data.get("tool_input") or {}).get("file_path") or ""
    if not is_prompt(path):
        sys.exit(0)

    mode = (os.environ.get("SYNTHESIS_EVAL_ON_EDIT") or "smoke").strip().lower()
    if mode == "off":
        sys.exit(0)

    ok, tail = run_free_regression()
    name = os.path.basename(path)

    if mode == "async":
        fire_async_eval()
        sys.stderr.write(
            f"🧪 synthesis eval: {name} を編集しました。有料 eval を非同期で起動しました"
            f"（結果は data/synthesis_evals/ に出ます）。\n")
    else:
        record_pending(path, mode)
        sys.stderr.write(
            f"🧪 synthesis eval: {name} を編集しました。\n"
            f"   評価軸の回帰: {tail or '実行済み'}\n"
            f"   文章の質は未評価です。確認するには:\n"
            f"     python scripts/eval_synthesis.py --changed\n"
            f"   （`claude -p` を2回呼びます。hook の10秒予算では完走できないため予約のみ）\n")

    if not ok:
        sys.stderr.write(
            "⚠️ tests/synthesis が失敗しています。**評価軸そのものが壊れています。**\n")
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
