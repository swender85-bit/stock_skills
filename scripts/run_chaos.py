#!/usr/bin/env python3
"""カオステスト実行 -- わざと壊して、システムが気づくか試す (改善7).

## 発想

cwc の SRE 教材は「**壊れたエージェントを渡されて、自分で直す**」形式だった。
これを逆向きに使う —— **わざと壊して、システムが気づくか試す。**

`docs/STRUCTURE.md` §16 の8原則は「破ると設計が死ぬ性質」として書かれているが、
**それを守れているかを攻撃側から検証していない**。特に:

> 8. 単一の取得元に依存しない。
>    2026-08-02 に直した穴9件のうち **6件が同じ形**だった。個別のバグではなく設計の癖。

同じ形が再発したとき、次は自動で捕まるようにする。

## 使い方

    python scripts/run_chaos.py           # 全部（月1回）
    python scripts/run_chaos.py --list    # 何をどう壊すかだけ見る
    python scripts/run_chaos.py -k vault  # 一部だけ

CI には入れない。`pytest tests/ -q` が20秒という速さを保つほうが日々の価値が高い。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

#: 何をどう壊し、何を検出できるべきか。--list で出す「攻撃計画」。
ATTACKS: tuple[tuple[str, str, str], ...] = (
    ("模型を3ヶ月前の版にする", "TestStaleHoldings",
     "売却済み銘柄を**幽霊ポジション**として検出する。"
     "検出できないと、存在しない資産のリスクを計算し続ける"),
    ("楽天CSVを模型の生成元と同一にする", "TestCircularOnly",
     "`circular=true` が立ち、差分0でも『独立検証ではない』と書く (§16-3)"),
    ("楽天CSVを0件にする", "TestEmptyCsv",
     "「保有なし」ではなく**「取得できなかった」**。"
     "混同すると全保有が幽霊になり『全部売却済み』という嘘が出る"),
    ("futu-api 未導入で moomoo を落とす", "TestMoomooDown",
     "マクロが**退避キャッシュ**に切り替わり、`cached_age_hours` を明示する。"
     "落ちた週に FOMC が黙って消えない"),
    ("全項目を available=false にする", "TestAllUnavailable",
     "「問題なし」ではなく**「判定不能」**と書く (§16-1)"),
    ("決算日を空リストにする", "TestEarningsNone",
     "`no_earnings`（ETFに決算は無い）と `unavailable`（取れなかった）を区別する"),
    ("同期後に vault からファイルを消す", "TestVaultDeleted",
     "`resync_missing()` が翌回に検出して復元する"),
)


def print_plan() -> None:
    print("## カオステストの攻撃計画")
    print()
    print(f"{'壊し方':38} {'テスト':22} 期待される検出")
    print("-" * 110)
    for how, test, expect in ATTACKS:
        print(f"{how:38} {test:22} {expect}")
    print()
    print(f"計 {len(ATTACKS)} 種類。CI には入れず、月1回このスクリプトで回します。")


def main() -> int:
    ap = argparse.ArgumentParser(description="カオステスト実行 (改善7)")
    ap.add_argument("--list", action="store_true", help="攻撃計画を表示して終了")
    ap.add_argument("-k", help="pytest の -k フィルタ")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if args.list:
        print_plan()
        return 0

    print(f"■ カオステスト {date.today().isoformat()}")
    print(f"  {len(ATTACKS)} 種類の壊し方で、検出できるかを試します。")
    print()

    cmd = [sys.executable, "-m", "pytest", "tests/chaos",
           "-p", "no:cacheprovider", "--no-header",
           "-v" if args.verbose else "-q"]
    if args.k:
        cmd += ["-k", args.k]

    env = dict(os.environ)
    env["RUN_CHAOS"] = "1"   # tests/chaos/conftest.py のスキップを解除する

    proc = subprocess.run(cmd, cwd=str(REPO), env=env)
    print()
    if proc.returncode == 0:
        print("✅ すべての壊し方をシステムが検出しました。")
        print("   ただしこれは『8原則を守れている証拠』であって、"
              "『穴が無い証拠』ではありません。攻撃の種類が7つしかありません。")
    else:
        print("❌ 検出できなかった壊し方があります。")
        print("   §16 の原則が1つ以上破れています。上の失敗内容を読んでください。")
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
