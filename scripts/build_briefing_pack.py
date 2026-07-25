#!/usr/bin/env python3
"""ブリーフィングパック生成 CLI（Python・トークン0）。

Claude の深掘り synthesis に渡す「全材料」を1つの JSON に束ねて保存する。
週次レポート（保有全体）と個別質問（単一銘柄）の両方の入口。

Usage:
    # 保有全体（週次レポート用）
    python scripts/build_briefing_pack.py
    python scripts/build_briefing_pack.py --snapshot "C:/path/保有.xlsx"

    # 単一銘柄（個別深掘り用）
    python scripts/build_briefing_pack.py --symbol SOXL
    python scripts/build_briefing_pack.py --symbol 7203.T

    # 保存せず標準出力に JSON
    python scripts/build_briefing_pack.py --symbol AAPL --stdout

出力: output/briefing/PF_YYYYMMDD.json または <SYMBOL>_YYYYMMDD.json
      パスは stdout 最終行に絶対パスで出す（駆動スクリプトが読む）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


def _json_default(o):
    """numpy / pandas / datetime を JSON 化する。"""
    import math

    # numpy scalar
    if hasattr(o, "item"):
        try:
            return o.item()
        except Exception:
            pass
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    # pandas NaT / NaN 等
    try:
        if isinstance(o, float) and math.isnan(o):
            return None
    except Exception:
        pass
    return str(o)


def _sanitize(obj):
    """DataFrame 等の非直列化オブジェクトを再帰的に落とす/変換する。"""
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()
                if not _is_dataframe(v)}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj if not _is_dataframe(v)]
    return obj


def _is_dataframe(v) -> bool:
    return type(v).__name__ in ("DataFrame", "Series")


def main() -> int:
    parser = argparse.ArgumentParser(description="ブリーフィングパック生成")
    parser.add_argument("--symbol", help="単一銘柄モード（指定なしは保有全体）")
    parser.add_argument("--snapshot", help="楽天RSSスナップショット（保有全体モード）")
    parser.add_argument("--rakuten-config", default="config/rakuten.yaml")
    parser.add_argument("--holdings-config", default="config/weekly_holdings.yaml")
    parser.add_argument("--out-dir", default="output/briefing")
    parser.add_argument("--stdout", action="store_true", help="保存せず標準出力へ")
    parser.add_argument("--no-moomoo", action="store_true")
    parser.add_argument("--no-context", action="store_true")
    parser.add_argument("--monthly-contribution", type=float, default=50000.0)
    args = parser.parse_args()

    from src.core.research import briefing_pack as bp

    if args.symbol:
        print(f"[info] {args.symbol} のパックを構築中...", file=sys.stderr)
        pack = bp.build_symbol_briefing(
            args.symbol,
            include_moomoo=not args.no_moomoo,
            include_context=not args.no_context,
        )
        name = f"{args.symbol.replace('.', '_')}_{date.today():%Y%m%d}.json"
    else:
        print("[info] 保有全体のパックを構築中...", file=sys.stderr)
        from src.core.portfolio.weekly import load_holdings_config

        snapshot = _load_snapshot(args.snapshot, args.rakuten_config)
        config = load_holdings_config(args.holdings_config)
        pack = bp.build_portfolio_briefing(
            config,
            rss_snapshot=snapshot,
            monthly_contribution=args.monthly_contribution,
            include_moomoo=not args.no_moomoo,
            include_context=not args.no_context,
        )
        name = f"PF_{date.today():%Y%m%d}.json"

    for w in (pack.get("meta") or {}).get("warnings", []):
        print(f"[warn] {w}", file=sys.stderr)

    payload = json.dumps(_sanitize(pack), ensure_ascii=False,
                         indent=2, default=_json_default)

    if args.stdout:
        print(payload)
        return 0

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / name
    out_path.write_text(payload, encoding="utf-8")
    print(f"✅ パック保存: {out_path}", file=sys.stderr)
    # 駆動スクリプトが拾えるよう、絶対パスを stdout 最終行に出す
    print(str(out_path.resolve()))
    return 0


def _load_snapshot(snapshot_arg, rakuten_config):
    try:
        import yaml

        conf = {}
        p = Path(rakuten_config)
        if p.exists():
            conf = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        path = snapshot_arg or conf.get("snapshot_path")
        if not path:
            return None
        from src.data.rakuten_rss import read_snapshot, snapshot_freshness

        snap = read_snapshot(path)
        snap["_freshness"] = snapshot_freshness(
            snap, max_age_hours=float(conf.get("max_age_hours") or 72.0)
        )
        return snap
    except Exception as e:
        print(f"[info] RSSスナップショットは使用しません: {e}", file=sys.stderr)
        return None


if __name__ == "__main__":
    sys.exit(main())
