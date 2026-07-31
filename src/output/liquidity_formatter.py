"""流動性セクションの出力整形 (土曜設計書 提案6-⑥)。

ストレステストを「怖い数字を出す装置」から
**「実行可能な備えを事前に作らせる装置」**に変える。

「閉じ込め資本」と「暴落時に買う金がない」は、暴落が来る前の平常時（＝土曜）に
しか対処できない問題であり、土曜レポートの存在意義そのものに直結する。
"""

from __future__ import annotations

from typing import Any, Optional

from src.core.risk.liquidity import TIER_LABELS, format_days


def _pct(v: Any) -> str:
    return f"{v:.1f}%" if isinstance(v, (int, float)) else "—"


def format_profile(liquidity: dict) -> str:
    if not liquidity:
        return "■ 流動性プロファイル\n  取得できませんでした。\n"

    tiers = liquidity.get("tiers_pct") or {}
    lines = ["■ 流動性プロファイル", ""]
    for key in ("immediate", "several_days", "trapped", "unknown"):
        value = tiers.get(key)
        if not value:
            continue
        mark = " ⚠️" if key in ("trapped", "unknown") else ""
        lines.append(f"  {TIER_LABELS[key]:<26} 評価額比 {_pct(value)}{mark}")

    unmeasurable = liquidity.get("unmeasurable_pct")
    if unmeasurable:
        lines.append(f"  {'出来高で測れない（投信等）':<26} 評価額比 {_pct(unmeasurable)} ⚠️")
    lines.append("")

    trapped = liquidity.get("trapped") or []
    if trapped:
        lines.append("  閉じ込め資本の内訳:")
        for p in trapped:
            stress = p.get("days_stress") or {}
            lines.append(
                f"    {p.get('name') or p.get('symbol')} — "
                f"解消所要 {format_days(p.get('days_normal'))}（平常時）／ "
                f"推定 {format_days(stress.get('moderate'))}（ストレス時・中庸）")
            lines.append(
                f"        保守シナリオでは {format_days(stress.get('conservative'))}")
        lines.append("")

    unknown = liquidity.get("unknown") or []
    if unknown:
        lines.append(f"  ⚠️ 出来高が取れず判定不能: "
                     f"{', '.join(str(p.get('symbol')) for p in unknown)}")
        lines.append("     → これは「流動性が高い」という意味ではありません。")
        lines.append("")

    rate = liquidity.get("participation_rate")
    if rate:
        lines.append(f"  ℹ️ 出来高の {rate:.0%} までしか売らない前提で計算しています"
                     "（保守的な仮定）。")
    if liquidity.get("message"):
        lines.append(f"  ⚠️ {liquidity['message']}")
    lines.append("")
    return "\n".join(lines)


def format_feasibility(feasibility: dict) -> str:
    """推奨アクションの実行可能性。売れない推奨は推奨ではなく雑音。"""
    if not feasibility or not feasibility.get("checked"):
        return ""

    infeasible = feasibility.get("infeasible") or []
    unknown = feasibility.get("unknown") or []
    if not infeasible and not unknown:
        return ("  ✅ ストレステストの推奨アクションは、すべてストレス時でも"
                "実行可能な範囲です。\n\n")

    lines = ["  推奨アクションの実行可能性", ""]
    for r in infeasible:
        lines.append(f"    ⚠️ {r.get('symbol')}: {r.get('reason')}")
        for alt in r.get("alternatives") or []:
            lines.append(f"       - {alt}")
        lines.append("")
    for r in unknown:
        lines.append(f"    ℹ️ {r.get('symbol')}: {r.get('reason')}")
    if unknown:
        lines.append("")
    return "\n".join(lines)


def format_buying_power(power: dict) -> str:
    """暴落時に買う金があるか。無ければ「計画」ではない。"""
    if not power:
        return ""
    lines = ["■ ストレス時の資金余力", ""]
    lines.append(f"  現金比率           {_pct(power.get('cash_pct'))}")
    if power.get("buy_candidates"):
        lines.append(f"  暴落シナリオ下で「買い」推奨が出る銘柄数  "
                     f"{power['buy_candidates']}銘柄")
        if power.get("affordable_count") is not None:
            lines.append(f"  実際に買える銘柄数  {power['affordable_count']}銘柄"
                         f"（1銘柄あたり {power.get('assumed_ticket_jpy'):,}円 想定）")
    lines.append("")
    if power.get("message"):
        lines.append(f"  ⚠️ {power['message']}")
        lines.append("")
    lines.append(f"  ℹ️ {power.get('note')}")
    lines.append("")
    return "\n".join(lines)


def format_account_asymmetry(asymmetry: dict) -> str:
    """NISA の下方非対称。日本の個人投資家にとって極めて重要。"""
    if not asymmetry:
        return ""
    if not asymmetry.get("available"):
        return f"  ℹ️ 口座区分の非対称: {asymmetry.get('reason')}\n\n"
    if not asymmetry.get("message"):
        return ""

    lines = ["■ 口座区分によるストレス時の非対称", ""]
    lines.append(f"  NISA口座保有分     評価額比 {_pct(asymmetry.get('tax_free_pct'))}")
    lines.append(f"  課税口座保有分     評価額比 {_pct(asymmetry.get('taxable_pct'))}")
    if asymmetry.get("unknown_pct"):
        lines.append(f"  口座区分不明       評価額比 "
                     f"{_pct(asymmetry.get('unknown_pct'))}")
    lines.append("")
    lines.append(f"  ⚠️ {asymmetry['message']}")
    lines.append("")
    return "\n".join(lines)


def format_liquidity_section(bundle: dict) -> str:
    """流動性セクション全体。"""
    if not bundle:
        return ""
    parts = [
        format_profile(bundle.get("liquidity") or {}),
        format_feasibility(bundle.get("feasibility") or {}),
        format_buying_power(bundle.get("buying_power") or {}),
        format_account_asymmetry(bundle.get("account_asymmetry") or {}),
    ]
    text = "\n".join(p for p in parts if p)
    errors = bundle.get("errors") or []
    if errors:
        text += "\n  ⚠️ 一部の流動性材料を取得できませんでした:\n"
        for e in errors:
            text += f"     - {e}\n"
    return text


def format_compact(bundle: dict) -> str:
    """静穏週用の1行版。"""
    liq = (bundle or {}).get("liquidity") or {}
    tiers = liq.get("tiers_pct") or {}
    bits = []
    if tiers.get("trapped"):
        bits.append(f"閉じ込め {_pct(tiers['trapped'])}")
    if tiers.get("unknown"):
        bits.append(f"判定不能 {_pct(tiers['unknown'])}")
    power = (bundle or {}).get("buying_power") or {}
    if power.get("message"):
        bits.append("暴落時の買い余力なし")
    return "流動性  " + ("・".join(bits) if bits else "制約なし")
