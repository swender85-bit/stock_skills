#!/usr/bin/env python3
"""週次ポートフォリオ分析レポートの生成 (毎週土曜朝 JST).

保有と株価は楽天証券 MarketSpeed II RSS のスナップショットを最優先で使う。
証券口座への自動ログインはしない（資格情報を保存しない設計）。

Usage:
    # 通常実行（config/rakuten.yaml の snapshot_path を読む）
    python scripts/weekly_report.py

    # スナップショットを明示
    python scripts/weekly_report.py --snapshot "C:/path/to/保有.xlsx"

    # vault に書かず標準出力だけ
    python scripts/weekly_report.py --dry-run

    # RSSが無くても設定ファイルの保有で続行（既定は警告して続行）
    python scripts/weekly_report.py --require-rss   # RSSが無ければ異常終了
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from src.core.portfolio.weekly import (  # noqa: E402
    DEFAULT_HOLDINGS_CONFIG,
    build_report_data,
    load_holdings_config,
)
from src.output.weekly_formatter import format_weekly_report  # noqa: E402


DEFAULT_RAKUTEN_CONFIG = "config/rakuten.yaml"


def load_rakuten_config(path: str = DEFAULT_RAKUTEN_CONFIG) -> dict:
    import yaml

    p = Path(path)
    if not p.exists():
        return {}
    with p.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_snapshot(snapshot_path: str | None, max_age_hours: float) -> tuple[dict | None, list[str]]:
    """RSS スナップショットを読む。失敗理由はメッセージとして返す。"""
    messages: list[str] = []
    if not snapshot_path:
        messages.append(
            "楽天RSSスナップショットのパスが未設定です "
            "(config/rakuten.yaml の snapshot_path、または --snapshot)。"
        )
        return None, messages

    try:
        from src.data.rakuten_rss import read_snapshot, snapshot_freshness

        snapshot = read_snapshot(snapshot_path)
        freshness = snapshot_freshness(snapshot, max_age_hours=max_age_hours)
        snapshot["_freshness"] = freshness
        messages.append(freshness["message"])
        return snapshot, messages
    except Exception as e:
        messages.append(f"楽天RSSスナップショットを読めませんでした: {e}")
        return None, messages


def build_moomoo_section(config: dict) -> str:
    """moomoo(OpenD) から週次インサイトを集めて markdown セクションを返す。

    無人実行のため、OpenD が起動していなければ ensure_opend が自動起動→終了する。
    無効化・未起動・SDK無し・例外時は空文字（レポート本体には影響しない）。
    """
    try:
        from src.data import moomoo_client
        from src.core.research import moomoo_insights
    except Exception:
        return ""

    symbols = [
        h.get("quote_symbol")
        for h in (config.get("holdings") or [])
        if h.get("quote_symbol")
    ]
    if not symbols:
        return ""

    try:
        with moomoo_client.ensure_opend() as up:
            if not up:
                status = moomoo_client.get_error_status()
                print(f"[info] moomoo インサイトはスキップ: {status.get('message')}",
                      file=sys.stderr)
                return ""
            data = moomoo_insights.collect_weekly_insights(symbols)
            return moomoo_insights.format_weekly_section(data)
    except Exception as e:
        print(f"[info] moomoo インサイト取得に失敗: {e}", file=sys.stderr)
        return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="週次ポートフォリオ分析レポート")
    parser.add_argument("--snapshot", help="楽天RSSスナップショット(.xlsx/.csv)のパス")
    parser.add_argument("--holdings-config", default=DEFAULT_HOLDINGS_CONFIG)
    parser.add_argument("--rakuten-config", default=DEFAULT_RAKUTEN_CONFIG)
    parser.add_argument("--dry-run", action="store_true", help="保存せず標準出力のみ")
    parser.add_argument(
        "--require-rss",
        action="store_true",
        help="RSSスナップショットが読めない場合に異常終了する",
    )
    parser.add_argument(
        "--monthly-contribution", type=float, default=50000.0,
        help="毎月の積立額(既定 5万円 = FANG+ のDCA)",
    )
    parser.add_argument("--max-age-hours", type=float, default=72.0)
    args = parser.parse_args()

    rakuten_conf = load_rakuten_config(args.rakuten_config)
    snapshot_path = args.snapshot or rakuten_conf.get("snapshot_path")
    max_age = args.max_age_hours or float(rakuten_conf.get("max_age_hours") or 72.0)

    snapshot, messages = load_snapshot(snapshot_path, max_age)
    for m in messages:
        print(f"[info] {m}", file=sys.stderr)

    if snapshot is None and args.require_rss:
        print(
            "❌ 楽天RSSスナップショットが必須指定ですが読めませんでした。中止します。",
            file=sys.stderr,
        )
        return 1

    config = load_holdings_config(args.holdings_config)

    print("[info] 株価・業績を取得中...", file=sys.stderr)
    data = build_report_data(
        config,
        rss_snapshot=snapshot,
        monthly_contribution=args.monthly_contribution,
    )

    report = format_weekly_report(data)

    print("[info] moomoo インサイトを取得中...", file=sys.stderr)
    moomoo_md = build_moomoo_section(config)
    if moomoo_md:
        report = f"{report.rstrip()}\n\n{moomoo_md}"

    if args.dry_run:
        print(report)
        return 0

    filename = f"週次PF分析_{datetime.now().strftime('%Y%m%d')}.md"
    try:
        from src.output.sync import save_and_sync

        result = save_and_sync(report, filename)
        for m in result.get("messages", []):
            print(f"[save] {m}", file=sys.stderr)
        print(f"✅ 週次レポートを保存しました: {result.get('output_path')}")
        if result.get("synced_path"):
            print(f"   vault: {result['synced_path']}")
        elif result.get("degraded"):
            print("   ⚠️ vault へ同期できず output/ のみ保存しました。")
        verify = result.get("verify") or {}
        if verify and verify.get("ok") is False:
            print(f"   ❌ 検証NG: {verify}", file=sys.stderr)
            return 1
    except Exception as e:
        print(f"❌ 保存に失敗しました: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
