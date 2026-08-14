#!/usr/bin/env python3
"""X の批評家発言を取得して台帳に積む (改善5 の入力経路).

## 使い方

    python scripts/fetch_critics.py                    # プレビュー（1バイトも書かない）
    python scripts/fetch_critics.py --apply            # 台帳に追記
    python scripts/fetch_critics.py --days 14 --apply  # 14日分
    python scripts/fetch_critics.py --source noirinvestor --apply  # 1人だけ

## 何が起きるか

`config/critics.yaml` のアカウントの直近発言を Grok の X Search で取り、
`data/critics/<source_id>.json` に **`pending`（未検証）** として積む。

- **要約しない。** 原文をそのまま入れる（要約した時点で自己推論が混ざり、
  後から「本人が何と言ったか」を検証できなくなる）
- 分野は**発言ごとに**分類する。「この人は需給の人」と決め打ちしない
- 銘柄と価格ターゲット/方向が取れた発言には検証期限を付ける
  → `scripts/score_critics.py` が期限到来分を実測で自動採点する

## 取得できなかったとき

`XAI_API_KEY` 未設定・レート制限・アカウント非公開などで取れなかった場合、
**「発言が無かった」とは報告しない。** 取得失敗として明示する（§16-1）。

## 週次との関係

取り込んだ発言は外部言説（深度1）として週次レポートに出るが、
**その情報源のそのドメインの重みが 0.6 以上になるまで本文の根拠には使われない**
（`.claude/rules/provenance.md`）。それまでは「◯◯氏の見解（的中率 未測定）」。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from src.core.critic_calibration import (  # noqa: E402
    DOMAINS,
    ingest_posts,
    load_critic,
    profile,
)
from src.data.critic_feed import (  # noqa: E402
    enabled_critics,
    fetch_all,
    load_critics_config,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="X の批評家発言を取得する (改善5)")
    ap.add_argument("--days", type=int, help="遡る日数（既定は config/critics.yaml）")
    ap.add_argument("--source", action="append", default=[],
                    help="この source_id だけ取得（複数可）")
    ap.add_argument("--apply", action="store_true", help="台帳に書き込む（既定はプレビュー）")
    ap.add_argument("--config", help="critics.yaml のパス")
    args = ap.parse_args()

    config = load_critics_config(args.config)
    if args.source:
        wanted = set(args.source)
        config = {**config,
                  "critics": [c for c in config.get("critics") or []
                              if c.get("source_id") in wanted]}

    critics = enabled_critics(config)
    if not critics:
        print("取得対象のアカウントがありません（config/critics.yaml を確認してください）。")
        return 1

    print(f"■ X 批評家フィード取得（{len(critics)}アカウント）")
    for c in critics:
        print(f"   @{c['handle']}  {c.get('url', '')}")
    print()

    result = fetch_all(days=args.days, config=config)
    print(result["summary"])
    print()

    meta = config.get("meta") or {}
    horizon = int(meta.get("default_horizon_days") or 30)
    name_by_id = {c["source_id"]: c.get("name") or c["handle"] for c in critics}

    total_added = total_verifiable = 0
    for source_id, feed in result["results"].items():
        label = name_by_id.get(source_id, source_id)
        if not feed["available"]:
            # 取得失敗を「発言なし」と書かない
            print(f"⚠️ {label}: **取得できませんでした** — {feed['error']}")
            continue
        if not feed["posts"]:
            print(f"ℹ️ {label}: この期間に該当する発言はありませんでした"
                  f"（取得は成功しています）。")
            continue

        ingest = ingest_posts(source_id, feed["posts"],
                              default_horizon_days=horizon,
                              name=label, apply=args.apply)
        total_added += ingest["added"]
        total_verifiable += ingest["verifiable"]
        print(f"✅ {label}: 新規 {ingest['added']}件 "
              f"（重複スキップ {ingest['skipped']}件 / 自動採点可 {ingest['verifiable']}件）")
        for thesis in ingest["theses"][:5]:
            mark = "📈" if thesis.get("verifiable") else "  "
            head = thesis["claim"].replace("\n", " ")[:70]
            print(f"     {mark} [{DOMAINS.get(thesis['domain'], thesis['domain'])}] "
                  f"{thesis['date']} {head}")
        if ingest["added"] > 5:
            print(f"     … 他 {ingest['added'] - 5}件")

    print()
    print(f"合計: 新規 {total_added}件 / うち自動採点可 {total_verifiable}件")

    if not args.apply:
        print()
        print("——— これはプレビューです。1バイトも書いていません。———")
        print("台帳に積むには: python scripts/fetch_critics.py --apply")
        return 0

    print()
    print("## 現在の台帳")
    for source_id in result["results"]:
        p = profile(source_id)
        if not p["exists"]:
            continue
        usable = [d for d, w in p["domains"].items() if w.get("usable")]
        print(f"- {p['name'] or source_id}: 主張 {p['total']}件"
              f"（採点済み {p['scored']} / 未検証 {p['pending']}）"
              + (f" / 根拠に使える分野: {', '.join(usable)}" if usable
                 else " / 根拠に使える分野: **まだありません（未測定）**"))
    print()
    print("次: python scripts/score_critics.py    # 検証期限の来た言明を実測で採点")
    return 0


if __name__ == "__main__":
    sys.exit(main())
