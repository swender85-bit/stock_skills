#!/usr/bin/env python3
"""レポート保存＆検証 CLI（upgrade v1.0 Phase 2）

分析結果の Markdown を output/ に保存し、Obsidian vault へ同期して検証する。
「完了＝実物が届き、検証済み」を満たすための統一入口。

使い方:
    # 標準入力から本文を渡す
    python scripts/save_report.py --name "7203T_分析_20260718.md" --stdin < report.md

    # ファイルから
    python scripts/save_report.py --name "PF_ヘルスチェック_20260718.md" --file tmp.md

vault 未設定/不在なら output/ のみで完了（案内を表示）。検証失敗時は exit code 1。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

for _stream in ("stdout", "stderr", "stdin"):
    try:
        getattr(sys, _stream).reconfigure(encoding="utf-8")
    except Exception:
        pass

from src.output.sync import save_and_sync


def main() -> int:
    ap = argparse.ArgumentParser(description="分析レポートを保存＆Obsidian同期＆検証する")
    ap.add_argument("--name", required=True, help="ファイル名（例: 7203T_分析_20260718.md）")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", help="本文を読み込むファイルパス")
    src.add_argument("--stdin", action="store_true", help="標準入力から本文を読む")
    ap.add_argument("--config", help="config/output.yaml のパス（省略時は既定）")
    args = ap.parse_args()

    if args.stdin:
        content = sys.stdin.read()
    else:
        with open(args.file, "r", encoding="utf-8") as f:
            content = f.read()

    if not content.strip():
        print("❌ 本文が空です。保存を中止しました。", file=sys.stderr)
        return 1

    name = args.name
    if not name.lower().endswith(".md"):
        name += ".md"

    result = save_and_sync(content, name, config_path=args.config)

    print("=== 保存結果 ===")
    for m in result["messages"]:
        print(" -", m)

    v = result["verify"]
    print("")
    print("=== 検証 ===")
    print(f" 対象: {v['path']}")
    print(f" サイズ: {v['size']} bytes")
    print(f" 判定: {'✅ OK（届いた）' if v['ok'] else '❌ NG'}")
    for w in v.get("warnings", []):
        print(f"  ⚠️ {w}")
    for e in v.get("errors", []):
        print(f"  ❌ {e}")

    if result["degraded"]:
        print("")
        print("ℹ️ Obsidian vault へは同期していません（output/ のみ）。")
        print("   config/output.yaml の obsidian_vault_path を確認してください。")

    print("")
    final = result["synced_path"] or result["output_path"]
    print(f"📄 最終パス: {final}")

    return 0 if v["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
