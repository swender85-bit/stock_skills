"""読書台帳への取り込み CLI（読書台帳仕様 v2 V2）。

**1件30秒で終わらなければ、この仕組みは続かない。** 手順を増やさないこと。

    # URL を取り込む（本文は呼び出し側が渡す。この CLI は取得しない）
    python scripts/ingest_reading.py --title "QCOM 10-Q" --url https://www.sec.gov/... --stdin < body.txt

    # 貼り付けテキスト
    python scripts/ingest_reading.py --title "半導体サイクル解説" --stdin

    # 自分の考え（読後メモ）
    python scripts/ingest_reading.py --title "9843.Tの輸入比率" --own --text "..."

    # 状態確認
    python scripts/ingest_reading.py --status
    python scripts/ingest_reading.py --audit

🔴 **この CLI は Web を取得しない。** 取得は Claude 側（WebFetch）が行い、
本文を stdin で渡す。取得と保存を1プロセスに混ぜないのは、
取り込み経路に Bash とネットワークの両方を同時に持たせないため（仕様 6-2）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from src.core.reading import diet_audit, ingest, schema, vault  # noqa: E402


def cmd_status() -> int:
    h = vault.health()
    if not h.get("available"):
        print(f"⛔ {h.get('reason')}")
        return 1
    print(f"vault: {h['root']}")
    print(f"  構造: {'準備済み' if h['structure_ready'] else '未増設'}")
    print(f"  raw: {h['raw_count']}件 / concepts: {h['concept_count']}件")
    if h["raw_count"] == 0:
        print("\n  ℹ️ まだ1件も取り込まれていません。")
        print("     これは『読んでいない』ではなく『記録が始まっていない』です。")
        print("     ingested_at（それを知った時刻）は遡って記録できません。")
    return 0


def cmd_audit(days: int) -> int:
    r = diet_audit.audit(days=days)
    if not r.get("available"):
        print(f"⛔ {r.get('reason')}")
        return 1
    print(f"■ 偏食監査（直近{r['window_days']}日 / 取り込み {r['total_sources']}件）\n")

    def line(label: str, block: dict, fmt) -> None:
        if not block.get("available"):
            print(f"  {label:<22} {block.get('message', '—')}")
        else:
            print(f"  {label:<22} {fmt(block)}")

    line("保有偏重率", r["holding_bias"],
         lambda b: f"{b['value']}%（{b['hit']}/{b['total']}）  {b['message']}")
    line("情報源HHI", r["source_hhi"],
         lambda b: f"{b['value']}（{b['label']} / {b['distinct_sources']}源）")
    line("情報遅延（中央値）", r["information_delay"],
         lambda b: f"{b['median_days']}日（{b['count']}件）")
    line("着想→執行の遅延", r["idea_to_execution"],
         lambda b: f"中央値 {b['median_days']}日（{b['count']}件）")
    line("死蔵アイデア", r["dormant_ideas"],
         lambda b: f"{b['count']}銘柄")

    sa = r["stance_asymmetry"]
    print(f"\n  論調: {sa['message']}")
    mix = r["provenance_mix"]
    if mix["total"]:
        parts = " / ".join(f"{k} {v}" for k, v in sorted(mix["counts"].items()))
        print(f"  内訳: {parts}")
        print(f"  一次資料の比率: {mix['primary_pct']}% / 自分の考え: {mix['own_pct']}%")
    print(f"\n  {r['disclaimer']}")
    return 0


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description="読書台帳への取り込み")
    ap.add_argument("--title")
    ap.add_argument("--url", dest="source_url")
    ap.add_argument("--text", help="本文を直接渡す")
    ap.add_argument("--stdin", action="store_true", help="本文を標準入力から読む")
    ap.add_argument("--type", dest="source_type", default=None,
                    choices=list(schema.SOURCE_TYPES))
    ap.add_argument("--attachment", help="PDF等の原本パス（vault相対）")
    ap.add_argument("--own", action="store_true", help="provenance を『自分の考え』に固定")
    ap.add_argument("--provenance", choices=list(schema.PROVENANCES))
    ap.add_argument("--note", help="取り込み時の一言")
    ap.add_argument("--retroactive", action="store_true",
                    help="遡及登録（情報遅延の統計から除外される）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--audit", action="store_true")
    ap.add_argument("--days", type=int, default=diet_audit.DEFAULT_WINDOW_DAYS)
    args = ap.parse_args(argv)

    if args.status:
        return cmd_status()
    if args.audit:
        return cmd_audit(args.days)

    body = args.text
    if args.stdin or body is None:
        body = sys.stdin.read()
    if not (body or "").strip():
        print("⛔ 本文がありません。取得に失敗した可能性があります"
              "（『内容が無かった』と区別してください）。", file=sys.stderr)
        return 1
    if not args.title:
        print("⛔ --title は必須です。", file=sys.stderr)
        return 1

    source_type = args.source_type or ("url" if args.source_url else "text")
    provenance = args.provenance or (schema.OWN if args.own else None)

    try:
        result = ingest.ingest(
            body=body, title=args.title, source_url=args.source_url,
            source_type=source_type, attachment=args.attachment,
            note=args.note, provenance_override=provenance,
            precision=schema.RETRO if args.retroactive else schema.EXACT,
            dry_run=args.dry_run)
    except (ingest.IngestError, vault.VaultUnavailable) as exc:
        print(f"⛔ {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0

    icon = {"created": "✅", "duplicate": "ℹ️", "dry_run": "🔍"}.get(result["status"], "•")
    print(f"{icon} {result['status']}  id={result['id']}")
    for m in result.get("messages") or []:
        print(f"   {m}")
    if result.get("security"):
        print("\n   🔴 検出された危険パターン:")
        for f in result["security"]:
            print(f"     - [{f['severity']}] {f['label']}: {f['excerpt'][:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
