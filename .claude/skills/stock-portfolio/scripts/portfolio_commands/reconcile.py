"""`reconcile` サブコマンド — 三点照合 (土曜設計書 提案1)。

土曜レポートの第1セクション。**照合が通るまで分析しない**という順序を守るため、
週次パイプラインからも同じ `run_reconciliation()` を呼ぶ。

    python .../run_portfolio.py reconcile
    python .../run_portfolio.py reconcile --no-prices        # 価格取得を省く（高速）
    python .../run_portfolio.py reconcile --apply-corporate-actions
    python .../run_portfolio.py reconcile --json
"""

from __future__ import annotations

import json
from typing import Optional


def cmd_reconcile(
    holdings_config_path: str = "config/weekly_holdings.yaml",
    csv_path: Optional[str] = None,
    with_prices: bool = True,
    apply_ca: bool = False,
    as_json: bool = False,
    sources: Optional[list[str]] = None,
    no_opend: bool = False,
) -> dict:
    """三点照合を実行して表示する。戻り値は照合結果 dict。"""
    from src.core.portfolio.reconciliation import (
        apply_corporate_actions,
        run_reconciliation,
    )
    from src.core.portfolio.weekly import load_holdings_config

    config = load_holdings_config(holdings_config_path)

    report_data = None
    if with_prices:
        # 評価額が無いと孤児の重要度も差分の金額換算も出せない。
        # 取れなくても照合自体は成立するので、失敗は握り潰す。
        try:
            from src.core.portfolio.weekly import build_report_data

            report_data = build_report_data(config)
        except Exception as e:
            print(f"[info] 価格取得に失敗しました（照合は続行）: {type(e).__name__}: {e}")

    result = run_reconciliation(
        config,
        report_data=report_data,
        sources=sources,
        csv_path=csv_path,
        autostart_opend=not no_opend,
    )

    if apply_ca:
        _apply_and_save(config, result, holdings_config_path, apply_corporate_actions)

    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return result

    from src.output.reconcile_formatter import format_reconciliation

    print(format_reconciliation(result))

    if result.get("blocking"):
        print("  ⚠️ 照合が完全には通っていません。以降の分析結果は未照合フラグ付きで"
              "読んでください（設計書: 照合前に分析してはならない）。")
    return result


def _apply_and_save(config: dict, result: dict, path: str, apply_fn) -> None:
    """分割/併合だけ模型に反映して保存する。売買の記録漏れは触らない。"""
    updated, applied = apply_fn(config, result)
    if not applied:
        print("  ℹ️ 自動補正できるコーポレートアクション差分はありませんでした。")
        return
    try:
        import yaml

        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(updated, f, allow_unicode=True, sort_keys=False)
    except Exception as e:
        print(f"  ❌ 模型の保存に失敗しました: {e}")
        return
    print("  ✅ コーポレートアクション由来の差分を模型に反映しました:")
    for line in applied:
        print(f"     - {line}")
