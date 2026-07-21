#!/usr/bin/env python3
"""政策台帳 CLI -- 判断と執行の時間分離 (Fable5 第2弾 案A).

急変時の質問には、分析を再実行せずまずこれを引く。

Usage:
    # 登録: 撤退条件と失効期限は必須
    python scripts/manage_policy.py add --symbol 7203.T \
        --response "全株売却" --trigger "drawdown_pct<=-25" \
        --expires 2027-01-31 --rationale "営業CF悪化が本命。価格は補助条件"

    # 一覧
    python scripts/manage_policy.py list [--symbol 7203.T] [--all]

    # 照会: 現在市況を渡して政策上の応答を得る
    python scripts/manage_policy.py ask --symbol 7203.T --state "drawdown_pct=-6"

    # 失効
    python scripts/manage_policy.py expire --id pol_... --reason "テーゼ変更"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows の既定コードページ(cp932)では絵文字・一部記号が出力できず落ちる。
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from src.core.policy import (  # noqa: E402
    AmbiguousTriggerError,
    build_policy,
    evaluate_policy,
    expire_policy,
    list_policies,
    load_policy,
    policy_response,
    save_policy,
)


_OPS = ("<=", ">=", "<", ">", "==")


def parse_trigger(text: str) -> dict:
    """"drawdown_pct<=-25" 形式を dict に分解する。"""
    for op in _OPS:
        if op in text:
            metric, _, value = text.partition(op)
            return {"metric": metric.strip(), "op": op, "value": value.strip()}
    raise AmbiguousTriggerError(
        f"トリガーの書式が不正です: 「{text}」。例: drawdown_pct<=-25"
    )


def parse_state(pairs: list[str]) -> dict:
    """"drawdown_pct=-6" 形式のリストを市況 dict にする。"""
    state: dict = {}
    for item in pairs:
        for chunk in item.split(","):
            if not chunk.strip():
                continue
            key, _, value = chunk.partition("=")
            try:
                state[key.strip()] = float(value)
            except ValueError:
                state[key.strip()] = None
    return state


def cmd_add(args) -> int:
    triggers = [parse_trigger(t) for t in args.trigger]
    policy = build_policy(
        symbol=args.symbol,
        response=args.response,
        triggers=triggers,
        expires_on=args.expires,
        rationale=args.rationale or "",
        intent=args.intent,
    )
    path = save_policy(policy)
    print(f"✅ 政策を登録しました: {policy['id']}")
    print(f"   対象: {policy['symbol']} / 応答: {policy['response']}")
    print(f"   条件: " + ", ".join(
        f"{t['metric']} {t['op']} {t['value']}" for t in policy["triggers"]
    ))
    print(f"   失効期限: {policy['expires_on']}")
    print(f"   保存先: {path}")
    return 0


def cmd_list(args) -> int:
    policies = list_policies(symbol=args.symbol, active_only=not args.all)
    if not policies:
        print("政策は登録されていません。")
        return 0
    for p in policies:
        status = p.get("status", "active")
        print(f"[{p['id']}] {p['symbol']} — {p['response']} ({status}, 期限 {p['expires_on']})")
        for t in p["triggers"]:
            print(f"    {t['metric']} {t['op']} {t['value']}")
    return 0


def cmd_ask(args) -> int:
    state = parse_state(args.state or [])
    result = policy_response(args.symbol, state)
    print(f"## 政策照会: {args.symbol}")
    print()
    print(result["answer"])
    if result["requires_cooling"]:
        print()
        print("⚠️ 改訂には冷却期間が適用されます。")
    return 0


def cmd_expire(args) -> int:
    policy = load_policy(args.id)
    if policy is None:
        print(f"政策が見つかりません: {args.id}", file=sys.stderr)
        return 1
    expire_policy(policy, reason=args.reason or "")
    save_policy(policy)
    print(f"✅ 失効しました: {args.id}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="政策台帳の管理 (案A)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="政策を登録")
    p_add.add_argument("--symbol", required=True)
    p_add.add_argument("--response", required=True, help="トリガー成立時の応答")
    p_add.add_argument("--trigger", action="append", required=True,
                       help="例: drawdown_pct<=-25 (複数可)")
    p_add.add_argument("--expires", required=True, help="失効期限 YYYY-MM-DD (必須)")
    p_add.add_argument("--rationale", default="")
    p_add.add_argument("--intent", default="conditional_commit",
                       choices=["conditional_commit", "awaiting_trigger", "deliberate_inaction"])
    p_add.set_defaults(func=cmd_add)

    p_list = sub.add_parser("list", help="政策を一覧")
    p_list.add_argument("--symbol")
    p_list.add_argument("--all", action="store_true", help="失効済みも表示")
    p_list.set_defaults(func=cmd_list)

    p_ask = sub.add_parser("ask", help="現在市況に対する政策上の応答を得る")
    p_ask.add_argument("--symbol", required=True)
    p_ask.add_argument("--state", action="append", help="例: drawdown_pct=-6")
    p_ask.set_defaults(func=cmd_ask)

    p_exp = sub.add_parser("expire", help="政策を失効させる")
    p_exp.add_argument("--id", required=True)
    p_exp.add_argument("--reason", default="")
    p_exp.set_defaults(func=cmd_expire)

    args = parser.parse_args()
    try:
        return args.func(args)
    except (AmbiguousTriggerError, ValueError) as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
