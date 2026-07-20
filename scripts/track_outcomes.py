#!/usr/bin/env python3
"""判断の答え合わせ CLI（学習ループ / upgrade v1.0 Phase 4）

過去の Screen / Report 判断を現在株価と突き合わせ、命中率を集計して
`docs/OUTCOMES.md` に書き出す。大きく外した買い判断は lesson ノートへ自動記録する。

使い方:
    python scripts/track_outcomes.py                # 集計してOUTCOMES.md更新
    python scripts/track_outcomes.py --no-lessons   # lesson自動記録をしない
    python scripts/track_outcomes.py --dry-run      # 書き込みせず結果だけ表示

ネット不通・株価取得不可・Neo4j未接続でも動作する（該当判断はスキップ）。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

for _stream in ("stdout", "stderr"):
    try:
        getattr(sys, _stream).reconfigure(encoding="utf-8")
    except Exception:
        pass

from src.core.research.outcome_tracker import (
    collect_judgments,
    evaluate,
    find_big_misses,
    render_markdown,
)

OUTCOMES_PATH = os.path.join(
    os.path.dirname(__file__), "..", "docs", "OUTCOMES.md"
)


def _price_fn(symbol: str):
    """現在株価を返す。取得不可なら None（graceful degradation）。"""
    try:
        from src.data import yahoo_client
        info = yahoo_client.get_stock_info(symbol)
        if info:
            return info.get("price")
    except Exception:
        return None
    return None


def _file_lessons(misses: list[dict]) -> int:
    """大きな外しを lesson ノートに記録（同銘柄で既存の外し lesson があればスキップ）。"""
    filed = 0
    try:
        from src.data.note_manager import save_note, load_notes
    except Exception:
        return 0

    for r in misses:
        sym = r["symbol"]
        trigger = f"{r['source']}で買い候補として挙げた {sym}"
        try:
            existing = load_notes(symbol=sym, note_type="lesson")
        except Exception:
            existing = []
        # 同一トリガーの重複を避ける
        if any("買い候補" in (n.get("trigger") or "") for n in existing):
            continue
        content = (
            f"{r['entry_date']} に {r['source']}（{r.get('label','')}）で {sym} を"
            f"買い候補として挙げたが、{r['days_elapsed']}日で {r['return_pct']*100:+.1f}% と大きく下落した。"
        )
        try:
            save_note(
                symbol=sym,
                note_type="lesson",
                content=content,
                source="track_outcomes",
                trigger=trigger,
                expected_action="同種のシグナルで飛びつく前に、下落継続リスク（業績悪化・バリュートラップ）を再確認する",
            )
            filed += 1
        except Exception:
            continue
    return filed


def main() -> int:
    ap = argparse.ArgumentParser(description="過去判断の答え合わせと命中率集計")
    ap.add_argument("--no-lessons", action="store_true", help="lesson自動記録をしない")
    ap.add_argument("--dry-run", action="store_true", help="ファイルへ書き込まず結果のみ表示")
    ap.add_argument("--miss-threshold", type=float, default=-0.20,
                    help="lesson化する下落率のしきい値（既定 -0.20 = -20%%）")
    ap.add_argument("--min-days", type=int, default=90,
                    help="lesson化する最低経過日数（既定 90）")
    args = ap.parse_args()

    judgments = collect_judgments()
    if not judgments:
        print("履歴に答え合わせできる判断がありません（data/history/ が空）。")
        return 0

    summary = evaluate(judgments, _price_fn)
    misses = find_big_misses(summary, threshold_pct=args.miss_threshold, min_days=args.min_days)
    md = render_markdown(summary, misses)

    print("=== 答え合わせ結果 ===")
    print(f" 評価: {summary['n_evaluated']} 件 / 除外: {summary['n_skipped']} 件")
    rate = summary["overall_hit_rate"]
    print(f" 総合命中率: {rate*100:.0f}%" if rate is not None else " 総合命中率: —（評価可能な判断なし）")
    for h, s in summary["horizons"].items():
        r = s["hit_rate"]
        print(f"  {h}日以上: {s['n']}件 命中率 {r*100:.0f}%" if r is not None else f"  {h}日以上: 0件")
    print(f" 学ぶべき外し: {len(misses)} 件")

    if args.dry_run:
        print("\n[dry-run] OUTCOMES.md への書き込みと lesson記録はスキップしました。")
        return 0

    os.makedirs(os.path.dirname(OUTCOMES_PATH), exist_ok=True)
    with open(OUTCOMES_PATH, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"\n📄 書き出し: {os.path.abspath(OUTCOMES_PATH)}")

    if not args.no_lessons and misses:
        filed = _file_lessons(misses)
        print(f"📝 lesson自動記録: {filed} 件")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
