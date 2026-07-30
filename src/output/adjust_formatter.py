"""Markdown formatter for adjustment plans (KIK-496)."""

from __future__ import annotations

from src.core.portfolio.adjustment_advisor import (
    AdjustmentPlan,
    Action,
    Urgency,
)
from src.core.ticker_utils import get_lot_size


_URGENCY_EMOJI = {
    Urgency.HIGH: "\U0001f6a8",   # red rotating light
    Urgency.MEDIUM: "\u26a0\ufe0f",  # warning sign
    Urgency.LOW: "\u2139\ufe0f",   # info
}

_URGENCY_LABEL = {
    Urgency.HIGH: "HIGH",
    Urgency.MEDIUM: "MEDIUM",
    Urgency.LOW: "LOW",
}


def format_adjustment_plan(plan: AdjustmentPlan) -> str:
    """Format an adjustment plan as Markdown.

    Parameters
    ----------
    plan : AdjustmentPlan
        Plan from ``generate_adjustment_plan()``.

    Returns
    -------
    str
        Markdown-formatted report.
    """
    lines: list[str] = []
    lines.append("## Portfolio Adjustment Plan\n")

    # Regime info
    regime = plan.regime
    regime_parts = [f"**{regime.regime.upper()}**"]
    if regime.sma50_above_200:
        regime_parts.append("SMA50 > SMA200")
    else:
        regime_parts.append("SMA50 < SMA200")
    if regime.rsi is not None:
        regime_parts.append(f"RSI {regime.rsi}")
    if regime.drawdown is not None:
        regime_parts.append(f"DD {regime.drawdown*100:.1f}%")
    lines.append(f"Market Regime: {', '.join(regime_parts)}\n")

    if not plan.actions:
        lines.append("**調整不要** — 全ポジション健全です。\n")
        return "\n".join(lines)

    # Group by urgency
    by_urgency: dict[Urgency, list[Action]] = {
        Urgency.HIGH: [],
        Urgency.MEDIUM: [],
        Urgency.LOW: [],
    }
    for a in plan.actions:
        by_urgency[a.urgency].append(a)

    for urg in (Urgency.HIGH, Urgency.MEDIUM, Urgency.LOW):
        group = by_urgency[urg]
        if not group:
            continue

        emoji = _URGENCY_EMOJI[urg]
        label = _URGENCY_LABEL[urg]
        lines.append(f"### {emoji} {label} Priority\n")
        lines.append("| Action | Target | Lot | Reasons | Rules |")
        lines.append("|:-------|:-------|----:|:--------|:------|")

        for a in group:
            action_str = a.type.value
            reasons_str = "; ".join(a.reasons)
            rules_str = ", ".join(a.rule_ids)
            lot = get_lot_size(a.target)
            lot_str = f"{lot}株" if lot > 1 else "1"
            lines.append(f"| {action_str} | {a.target} | {lot_str} | {reasons_str} | {rules_str} |")

        lines.append("")

    # 土曜設計書 提案3/9: 売却系の提案は税引後で見なければ判断できない。
    lines.extend(_format_tax_section(plan.actions))

    # Summary
    lines.append("---")
    lines.append(f"**Summary:** {plan.summary}")

    return "\n".join(lines)


def _yen(v) -> str:
    return f"¥{v:,.0f}" if isinstance(v, (int, float)) else "—"


def _format_tax_section(actions: list[Action]) -> list[str]:
    """SELL / SWAP の税引後の手取り・損益分岐・入金代替案。

    ここを出さないと、含み益に対する 20.315% のハンデを隠したまま
    売却を提案することになる（乗り換え提案の構造的な過剰）。
    """
    relevant = [a for a in actions if getattr(a, "tax_view", None) is not None]
    if not relevant:
        return []

    lines = ["### 税引後の再評価（売却系の提案）", ""]
    lines.append("| Target | 売却額(税引前) | 手取り(税引後) | 摩擦 | 損益分岐 | 口座 |")
    lines.append("|:-------|-------------:|-------------:|-----:|--------:|:-----|")

    unavailable: list[str] = []
    for a in relevant:
        t = a.tax_view or {}
        if not t.get("available"):
            unavailable.append(a.target)
            continue
        h = t.get("switching_hurdle_pct")
        lines.append(
            f"| {a.target} | {_yen(t.get('gross_jpy'))} | {_yen(t.get('net_jpy'))} "
            f"| {_yen(t.get('friction_jpy'))} "
            f"| {f'+{h:.1f}%' if isinstance(h, (int, float)) else '—'} "
            f"| {t.get('account') or '—'} |")
    lines.append("")

    for a in relevant:
        f = getattr(a, "funding_alternative", None) or {}
        if f.get("note"):
            mark = "★" if f.get("realistic") else "ℹ️"
            lines.append(f"- {mark} **{a.target}** 入金代替: {f['note']}")
    if any(getattr(a, "funding_alternative", None) for a in relevant):
        lines.append("")

    if unavailable:
        lines.append(f"⚠️ {', '.join(unavailable)} は税引後の手取りを算出できませんでした。"
                     "税引前の金額を手取りとして扱わないでください。")
        lines.append("")

    lines.append("ℹ️ 乗り換え先は上記の損益分岐を上回って初めて意味があります。"
                 "概算であり税務助言ではありません。")
    lines.append("")
    return lines
