"""限界寄与スクリーニングの出力整形 (土曜設計書 提案2-⑥)。

出力の型を「一般的に良い銘柄」から **「あなたにとって良い銘柄」** へ変える。
90点を却下し65点を推す、という従来出力し得なかった結論をここで表示する。
"""

from __future__ import annotations

from typing import Any, Optional

_FACTOR_LABELS = {
    "market": "市場ベータ",
    "usdjpy": "USDJPY感応度",
    "rates": "金利感応度",
    "oil": "原油感応度",
    "semis": "半導体感応度",
}


def _fmt(v: Any, digits: int = 2) -> str:
    return f"{v:+.{digits}f}" if isinstance(v, (int, float)) else "—"


def format_portfolio_tilt(pf_exposure: dict, tilt_lines: Optional[list] = None) -> str:
    """PF の因子偏り。セクターHHIでは絶対に見えない軸。"""
    if not pf_exposure or not pf_exposure.get("available"):
        reason = (pf_exposure or {}).get("reason") or "因子を推定できませんでした"
        return (f"### あなたのポートフォリオの因子偏り\n\n"
                f"  取得できませんでした — {reason}\n"
                f"  ※ これは「偏りが無い」という意味ではありません。**測れていません。**\n")

    lines = ["### あなたのポートフォリオの因子偏り", ""]
    for name, beta in (pf_exposure.get("betas") or {}).items():
        label = _FACTOR_LABELS.get(name, name)
        lines.append(f"  {label:<14} {_fmt(beta)}")
    lines.append("")

    for line in tilt_lines or []:
        lines.append(f"  ⚠️ {line}")
    if tilt_lines:
        lines.append("")

    coverage = pf_exposure.get("coverage_pct")
    if isinstance(coverage, (int, float)) and coverage < 100:
        lines.append(f"  ℹ️ 因子を推定できた保有は {coverage}% です。"
                     "残りはこの数字に含まれていません。")
    if pf_exposure.get("note"):
        lines.append(f"  ℹ️ {pf_exposure['note']}")
    lines.append("")
    return "\n".join(lines)


def format_ranked(result: dict, limit: int = 10) -> str:
    """限界スコア順の候補一覧。"""
    if not result:
        return ""
    ranked = result.get("ranked") or []
    if not ranked:
        return "### 候補（保有考慮後）\n\n  候補がありません。\n"

    lines = ["### 候補（限界スコア順）", ""]
    lines.append("| # | 銘柄 | 単独 | 限界 | 係数 | 判定 |")
    lines.append("|--:|:-----|-----:|-----:|-----:|:-----|")

    for i, r in enumerate(ranked[:limit], 1):
        verdict = _verdict(r)
        lines.append(
            f"| {i} | {r.get('name') or r.get('symbol')}"
            f"（{r.get('symbol')}） | {r.get('standalone_score')} "
            f"| **{r.get('marginal_score')}** | {r.get('complement_factor')} "
            f"| {verdict} |")
    lines.append("")

    for r in ranked[:limit]:
        detail = _detail(r)
        if detail:
            lines.extend(detail)

    if result.get("degraded"):
        lines.append(f"  ⚠️ {result.get('note')}")
    else:
        lines.append(f"  ℹ️ {result.get('note')}")
    lines.append("")
    return "\n".join(lines)


def _verdict(r: dict) -> str:
    if r.get("below_quality_floor"):
        return "❌ 品質下限未満"
    if r.get("twins"):
        return "⚠️ 因子双子"
    factor = r.get("complement_factor")
    if isinstance(factor, (int, float)):
        if factor >= 1.1:
            return "✅ 補完的"
        if factor <= 0.8:
            return "⚠️ 集中を強める"
    return "— 中立"


def _detail(r: dict) -> list[str]:
    out: list[str] = []
    label = f"{r.get('name') or r.get('symbol')}（{r.get('symbol')}）"

    contributions = [c for c in (r.get("contributions") or [])
                     if abs(c.get("delta") or 0) >= 0.05]
    if contributions or r.get("warnings") or r.get("floor_note"):
        out.append(f"  **{label}**")

    for c in contributions[:3]:
        mark = "＋" if c["effect"] == "補完" else "－"
        tail = "（推定不安定のため割引）" if c.get("unstable") else ""
        out.append(f"    {mark} {_FACTOR_LABELS.get(c['factor'], c['factor'])}: "
                   f"PF {_fmt(c['pf_beta'])} vs 候補 {_fmt(c['candidate_beta'])}"
                   f" → {c['effect']}{tail}")

    for w in r.get("warnings") or []:
        out.append(f"    ⚠️ {w}")
    if r.get("floor_note"):
        out.append(f"    ❌ {r['floor_note']}")
    if r.get("note"):
        out.append(f"    ℹ️ {r['note']}")
    if out:
        out.append("")
    return out


def format_lookthrough(lookthrough: dict) -> str:
    """ETF ルックスルー警告。ETF比率が高いほど表示上の分散は虚構になる。"""
    if not lookthrough:
        return ""
    if not lookthrough.get("available"):
        return (f"### ルックスルー\n\n  {lookthrough.get('reason')}\n\n")

    hidden = lookthrough.get("hidden_amplification") or {}
    if not hidden:
        return ""
    lines = ["### ルックスルー警告", ""]
    for sym, v in hidden.items():
        lines.append(f"  {sym}: 表示上 {v['direct_pct']}% だが、ETF経由分を含めた"
                     f"実質比率は **{v['effective_pct']}%**")
    lines.append("")
    return "\n".join(lines)


def format_marginal_section(pf_exposure: dict, ranked: dict,
                            tilt_lines: Optional[list] = None,
                            lookthrough: Optional[dict] = None,
                            limit: int = 10) -> str:
    """限界寄与セクション全体。"""
    parts = [
        "## 今週のスクリーニング（保有考慮後）",
        "",
        format_portfolio_tilt(pf_exposure, tilt_lines),
        format_ranked(ranked, limit=limit),
        format_lookthrough(lookthrough or {}),
    ]
    return "\n".join(p for p in parts if p)
