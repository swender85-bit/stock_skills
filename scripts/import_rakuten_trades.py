#!/usr/bin/env python3
"""楽天証券の取引履歴CSVを読み、執行監査（決定生存率）の入力にする。

2026-08-01 の週次レポートはこう書いていた:

    決定生存率 — 測定できていない（執行率0%ではない）
    約定履歴が取れず、決定生存率は90日間測定不能。

約定履歴の取得元が moomoo だけだったのが原因。実際の売買は全て楽天にあるので、
**取得先が実態と食い違っていて原理的に測定できなかった。**

使い方:

    楽天証券Web → マイメニュー → 取引履歴 → 期間指定 → CSVで保存

    python scripts/import_rakuten_trades.py              # Downloads の最新を自動検出
    python scripts/import_rakuten_trades.py --path X.csv  # ファイル指定
    python scripts/import_rakuten_trades.py --days 90     # 直近90日だけ見る
    python scripts/import_rakuten_trades.py --audit       # 執行監査まで通して確認

保有CSV（assetbalance）とは別物。あちらは「いま何を持っているか」、
こちらは「いつ何を売買したか」。両方あって初めて
「判断したことを実際に実行できたか」が測れる。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def main() -> int:
    ap = argparse.ArgumentParser(description="楽天の取引履歴CSVを取り込む")
    ap.add_argument("--path", help="取引履歴CSVのパス（省略時は Downloads から自動検出）")
    ap.add_argument("--days", type=int, help="直近N日だけ対象にする")
    ap.add_argument("--audit", action="store_true", help="執行監査まで通して表示")
    args = ap.parse_args()

    from src.data import rakuten_trades as rt

    result = rt.load_trades(path=args.path, days=args.days)

    if not result.get("available"):
        print("❌ 取引履歴を読めませんでした。")
        print(f"   {result.get('error')}")
        print()
        print("   楽天証券Web → マイメニュー → 取引履歴 → 期間指定 → CSVで保存")
        print("   保存後、もう一度このコマンドを実行してください。")
        return 1

    rows = result.get("executions") or []
    print(f"✅ 取引履歴を読み込みました: {result.get('path')}")
    print(f"   約定 {len(rows)}件"
          + (f"（直近{args.days}日）" if args.days else "")
          + f" / 検出した列: {', '.join(result.get('detected_fields') or [])}")

    skipped = result.get("skipped") or []
    if skipped:
        print(f"   ℹ️ 読み飛ばした行 {len(skipped)}件（合計行・注記行など）")
    unknown = result.get("unknown_side") or 0
    if unknown:
        print(f"   ⚠️ 売買区分を判定できなかった行 {unknown}件"
              "（捨てずに残しています。突合には使われません）")

    if rows:
        print()
        print("   直近の約定:")
        for e in sorted(rows, key=lambda r: r.get("executed_at") or "")[-10:]:
            side = {"buy": "買", "sell": "売"}.get(e.get("side"), "?")
            qty = e.get("shares")
            px = e.get("price")
            print(f"     {e.get('executed_at')}  {side}  {e.get('symbol'):<10}"
                  f" {qty if qty is not None else '—'} @ {px if px is not None else '—'}")
    else:
        print("   ℹ️ 対象期間に約定はありませんでした。"
              "**これは「取得できなかった」ではありません。**")

    if args.audit:
        print()
        _print_audit(rows, args.days or 90)
    return 0


def _print_audit(rows: list, days: int) -> None:
    from src.core.portfolio.execution_audit import build_execution_audit

    audit = build_execution_audit(executions=rows, days=days)
    survival = audit.get("survival") or {}
    print(f"■ 執行監査（直近{days}日）")
    if survival.get("available"):
        print(f"   決定生存率: {survival.get('rate_pct')}% "
              f"（判断 {survival.get('decisions')}件 / 執行 {survival.get('executed')}件）")
    else:
        print(f"   決定生存率: 測定できず — {survival.get('reason')}")
    shortfall = audit.get("shortfall") or {}
    if shortfall.get("available"):
        print(f"   執行ショートフォール: {shortfall.get('mean_pct')}%")
    for e in audit.get("errors") or []:
        print(f"   ⚠️ {e}")


if __name__ == "__main__":
    raise SystemExit(main())
