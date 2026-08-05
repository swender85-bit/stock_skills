#!/usr/bin/env python3
"""検証期限の来た批評家の言明を実測で採点する (改善5 の出口).

## なぜこれが要るか

取得だけでは台帳は**永遠に `pending` のまま**で、重みは出ない。
重みが出なければ「9回当てた人」と「初出の人」を区別できず、改善5 は機能しない。
**採点の経路を作って初めて、蓄積が意味を持つ。**

## 何を自動採点できるか

**銘柄が特定できて、価格ターゲットか方向がある言明だけ。**

    「NVDAは$250まで行く」        → 自動採点できる
    「7203.T は来週上がる」        → 自動採点できる
    「今の地合いは良くない」        → **自動採点できない**（手で採点する）

自動採点できないものは `pending` のまま残す。**無理に採点しない。**
価格が取れなかった場合も `refuted` にしない（取得失敗を「外れた」と記録すると
台帳そのものが汚染される）。

## 使い方

    python scripts/score_critics.py                # プレビュー
    python scripts/score_critics.py --apply        # 採点を書き込む
    python scripts/score_critics.py --show-manual  # 手で採点すべきものを一覧
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from src.core.critic_calibration import (  # noqa: E402
    DOMAINS,
    due_for_scoring,
    load_critic,
    profile,
    save_critic,
    score_verifiable,
    unscorable_count,
)
from src.data.critic_feed import enabled_critics, load_critics_config  # noqa: E402


def _price_on(symbol: str, day: str) -> Optional[float]:
    """指定日（以前の直近営業日）の終値。取れなければ None。

    None を 0 や現値で埋めない。**取れなかったことを値で塗り潰すと、
    採点が捏造になる。**
    """
    try:
        from src.data import yahoo_client

        hist = yahoo_client.get_price_history(symbol, period="2y")
        if hist is None or hist.empty:
            return None
        closes = hist["Close"].dropna()
        target = date.fromisoformat(str(day)[:10])
        upto = [c for ts, c in closes.items()
                if getattr(ts, "date", lambda: None)() and ts.date() <= target]
        if upto:
            return float(upto[-1])
        return None
    except Exception:
        return None


def _price_now(symbol: str) -> Optional[float]:
    try:
        from src.data import yahoo_client

        info = yahoo_client.get_stock_info(symbol)
        price = (info or {}).get("price")
        return float(price) if isinstance(price, (int, float)) else None
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="批評家の言明を実測で採点する (改善5)")
    ap.add_argument("--apply", action="store_true", help="採点を書き込む（既定はプレビュー）")
    ap.add_argument("--source", action="append", default=[], help="この source_id だけ")
    ap.add_argument("--show-manual", action="store_true",
                    help="自動採点できない pending を一覧する")
    ap.add_argument("--config", help="critics.yaml のパス")
    args = ap.parse_args()

    config = load_critics_config(args.config)
    sources = [c["source_id"] for c in enabled_critics(config)]
    if args.source:
        sources = [s for s in sources if s in set(args.source)]
    if not sources:
        print("対象がありません（config/critics.yaml を確認してください）。")
        return 1

    print("■ 批評家の言明の採点")
    print()

    total_scored = total_undecidable = 0
    price_cache: dict[tuple[str, str], Optional[float]] = {}

    for source_id in sources:
        critic = load_critic(source_id)
        if not critic.get("exists"):
            print(f"ℹ️ {source_id}: 台帳がまだありません（fetch_critics.py で取得してください）")
            continue

        due = due_for_scoring(critic)
        manual = unscorable_count(critic)
        label = critic.get("name") or source_id

        if not due:
            print(f"ℹ️ {label}: 検証期限の来た言明はありません"
                  f"（自動採点できない未検証 {manual}件は残っています）")
            continue

        scored = undecidable = 0
        for thesis in due:
            v = thesis["verifiable"]
            symbol = v["symbol"]

            key_then = (symbol, str(thesis["date"]))
            if key_then not in price_cache:
                price_cache[key_then] = _price_on(symbol, thesis["date"])
            key_now = (symbol, "now")
            if key_now not in price_cache:
                price_cache[key_now] = _price_now(symbol)

            result = score_verifiable(thesis, price_cache[key_now], price_cache[key_then])
            if result is None:
                # 価格が取れなかった。**「外れた」ではない。**
                undecidable += 1
                thesis["verify_after"] = date.today().isoformat()
                thesis.setdefault("verify_attempts", 0)
                thesis["verify_attempts"] += 1
                thesis["verify_note"] = (
                    f"{symbol} の価格を取得できず採点できませんでした。"
                    "これは『外れた』ではありません。")
                continue

            thesis["score"] = result["score"]
            thesis["verified_on"] = result["verified_on"]
            thesis["evidence"] = result["evidence"]
            scored += 1
            head = thesis["claim"].replace("\n", " ")[:52]
            print(f"   [{result['score']:14}] {label} {thesis['date']} {head}")
            print(f"                    {result['evidence']}")

        total_scored += scored
        total_undecidable += undecidable
        print(f"✅ {label}: {scored}件を採点"
              + (f" / {undecidable}件は価格が取れず**採点不能**（外れではない）"
                 if undecidable else "")
              + f" / 自動採点できない未検証 {manual}件")

        if args.apply and (scored or undecidable):
            save_critic(critic)

    print()
    print(f"合計: 採点 {total_scored}件 / 採点不能 {total_undecidable}件")

    if args.show_manual:
        print()
        print("## 手で採点すべき言明（定性的で機械では判定できない）")
        for source_id in sources:
            critic = load_critic(source_id)
            rows = [t for t in critic.get("theses") or []
                    if t.get("score") == "pending" and not t.get("verifiable")]
            if not rows:
                continue
            print(f"\n### {critic.get('name') or source_id}（{len(rows)}件）")
            for t in rows[:10]:
                print(f"- [{DOMAINS.get(t['domain'], t['domain'])}] {t['date']} "
                      f"{t['claim'].replace(chr(10), ' ')[:80]}")
            if len(rows) > 10:
                print(f"- … 他 {len(rows) - 10}件")
        print()
        print("採点方法: data/critics/<source_id>.json の該当主張に")
        print('  "score": "hit_exact"|"hit_direction"|"partial"|"refuted",')
        print('  "verified_on": "YYYY-MM-DD"')
        print("を入れてください（検証日の無い採点は後知恵と区別できないため必須）。")

    if not args.apply:
        print()
        print("——— これはプレビューです。1バイトも書いていません。———")
        print("採点を保存するには: python scripts/score_critics.py --apply")
        return 0

    print()
    print("## 現在の重み")
    any_usable = False
    for source_id in sources:
        p = profile(source_id)
        if not p["exists"]:
            continue
        print(f"\n### {p['name'] or source_id}"
              f"（主張 {p['total']} / 採点済み {p['scored']} / 未検証 {p['pending']}）")
        if not p["domains"]:
            print("  分野データなし")
            continue
        for domain, w in sorted(p["domains"].items()):
            name = DOMAINS.get(domain, domain)
            if not w["available"]:
                print(f"  - {name:16} 未測定（採点済み {w['samples']}件・"
                      f"重みを出すには5件必要）")
            else:
                mark = "✅ 根拠に使える" if w["usable"] else "△ 引用のみ"
                print(f"  - {name:16} {w['weight']:.2f}（{w['samples']}件）{mark}")
                any_usable = any_usable or w["usable"]

    print()
    if not any_usable:
        print("⚠️ まだ本文の根拠に使える情報源・分野はありません。")
        print("   これは『当たらない』ではなく『まだ測れていない』です。")
        print("   全員の発言は「◯◯氏の見解（的中率 未測定）」として引用形式で出ます。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
