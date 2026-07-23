#!/usr/bin/env python3
"""moomoo OpenD の各APIが実際に取れるかを実測する。

契約が要るもの・空振りするものを、憶測ではなく実行結果で仕分ける。
OpenD が起動している状態で実行すること。

    python scripts/probe_moomoo.py
    python scripts/probe_moomoo.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

US = "US.SOXL"
JP = "JP.2802"

#: (ラベル, 呼び出し) — 引数は保有銘柄に寄せる
PROBES: list[tuple[str, str, str]] = [
    ("FedWatch 目標金利確率", "get_fed_watch_target_rate", "()"),
    ("FedWatch ドットプロット", "get_fed_watch_dot_plot", "()"),
    ("資金流向(個別)", "get_capital_flow", f"('{US}')"),
    ("資金分布(個別)", "get_capital_distribution", f"('{US}')"),
    ("オプションIV分析", "get_option_volatility", f"('{US}')"),
    ("原資産ヒストリカルVol", "get_option_underlying_his_volatility", f"('{US}')"),
    ("米株プレマーケット", "get_us_pre_market_rank", "()"),
    ("米株アフターアワー", "get_us_after_hours_rank", "()"),
    ("米株オーバーナイト", "get_us_overnight_rank", "()"),
    ("ニュース検索", "get_search_news", "('semiconductor')"),
    ("空売り残(米/香)", "get_short_interest", f"('{US}')"),
    ("日次空売り高", "get_daily_short_volume", f"('{US}')"),
    ("決算カレンダー", "get_earnings_calendar", "()"),
    ("経済イベントカレンダー", "get_economic_calendar", "()"),
    ("配当カレンダー", "get_dividend_calendar", "()"),
    ("機関保有変動", "get_institution_holding_change", f"('{US}')"),
    ("インサイダー取引", "get_insider_trade_list", f"('{US}')"),
    ("アナリストコンセンサス", "get_research_analyst_consensus", f"('{US}')"),
    ("レーティング変更", "get_rating_change", f"('{US}')"),
    ("Morningstarレポート", "get_research_morningstar_report", f"('{US}')"),
    ("ヒートマップ", "get_heat_map_data", "()"),
    ("騰落分布", "get_rise_fall_distribution", "()"),
    ("バリュエーション詳細", "get_valuation_detail", f"('{US}')"),
    ("ARK基金保有", "get_ark_fund_holding", "()"),
    # 参照系（既に使っているものの確認）
    ("市場スナップショット(米)", "get_market_snapshot", f"(['{US}'])"),
    ("市場スナップショット(日)", "get_market_snapshot", f"(['{JP}'])"),
]


def _summarize(data) -> str:
    """返ってきたデータの形をひと目で分かる形に。"""
    try:
        import pandas as pd

        if isinstance(data, pd.DataFrame):
            cols = list(data.columns)[:8]
            return f"DataFrame {data.shape} cols={cols}"
    except ImportError:
        pass
    if isinstance(data, (list, tuple)):
        return f"{type(data).__name__} len={len(data)} head={str(data[:1])[:120]}"
    if isinstance(data, dict):
        return f"dict keys={list(data)[:8]}"
    return str(data)[:160]


def probe(ctx, sdk, label: str, method: str, args_repr: str) -> dict:
    fn = getattr(ctx, method, None)
    if fn is None:
        return {"label": label, "method": method, "status": "missing",
                "detail": "SDKにメソッドが無い"}
    try:
        ret, data = eval(f"fn{args_repr}")  # noqa: S307 - 引数は本ファイル内の定数のみ
    except Exception as e:
        return {"label": label, "method": method, "status": "exception",
                "detail": f"{type(e).__name__}: {e}"[:200]}

    if ret != getattr(sdk, "RET_OK", 0):
        msg = str(data)[:200]
        权限 = any(k in msg for k in ("权限", "permission", "Permission",
                                      "无权限", "not subscribed", "订阅"))
        return {"label": label, "method": method,
                "status": "no_permission" if 权限 else "error",
                "detail": msg}

    empty = False
    try:
        import pandas as pd

        if isinstance(data, pd.DataFrame):
            empty = data.empty
    except ImportError:
        pass
    if isinstance(data, (list, tuple, dict)) and len(data) == 0:
        empty = True

    return {"label": label, "method": method,
            "status": "empty" if empty else "ok",
            "detail": _summarize(data)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", help="結果をJSONで書き出す")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=11111)
    args = parser.parse_args()

    try:
        import moomoo as sdk
    except ImportError:
        print("[error] moomoo-api が未インストールです: pip install moomoo-api")
        return 1

    try:
        ctx = sdk.OpenQuoteContext(host=args.host, port=args.port)
    except Exception as e:
        print(f"[error] OpenD に接続できません ({args.host}:{args.port}): {e}")
        print("        OpenD.exe を起動してから実行してください。")
        return 1

    results = []
    try:
        for label, method, args_repr in PROBES:
            r = probe(ctx, sdk, label, method, args_repr)
            results.append(r)
            mark = {"ok": "✅", "empty": "⬜", "no_permission": "🔒",
                    "error": "❌", "exception": "💥", "missing": "❓"}[r["status"]]
            print(f"{mark} {label:26} {r['detail'][:96]}")
    finally:
        try:
            ctx.close()
        except Exception:
            pass

    counts: dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("\n--- 集計 ---")
    for k, v in sorted(counts.items()):
        print(f"  {k}: {v}")

    if args.json:
        Path(args.json).write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n書き出し: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
