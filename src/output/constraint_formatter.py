"""制約セクションの出力整形 — 税・現金・入金・注意 (土曜設計書 提案3/9)。

固定骨格の第4セクション「制約」。設計書 第3章:

> 4. 制約 — 政策・税・現金・流動性・注意
>    ※**行動可能な空間を先に確定させる**

機会（買い候補）より先に制約を出す。順序を逆にすると、実行できない推奨を
先に読ませることになる。
"""

from __future__ import annotations

from typing import Any, Optional


def _money(v: Any) -> str:
    return f"¥{v:,.0f}" if isinstance(v, (int, float)) else "—"


def _pct(v: Any, digits: int = 1) -> str:
    return f"{v:.{digits}f}%" if isinstance(v, (int, float)) else "—"


# ---------------------------------------------------------------------------
# 手取り状態
# ---------------------------------------------------------------------------


def format_tax_state(state: dict) -> str:
    """当年の税務状態と NISA 枠。"""
    if not state:
        return "■ 手取り状態\n  税務状態を取得できませんでした。\n"

    lines = [f"■ 手取り状態（{state.get('year')}年分）", ""]

    realized = state.get("realized_gain_ytd_jpy")
    if realized is None:
        lines.append("  当年実現損益   未記録（売買履歴から自動集計できませんでした）")
        lines.append("                 → 実現損益が不明なため、損益通算の試算はできません。")
    else:
        est = state.get("estimated_tax_jpy")
        lines.append(f"  当年実現損益   {_money(realized)}"
                     f"（課税見込 約 {_money(est)}）")
    lines.append(f"  繰越損失残     {_money(state.get('loss_carryforward_jpy'))}")

    nisa = state.get("nisa") or {}
    # 使用額が推定なら、残枠を確定値のように書かない。
    # 過小に見せると枠を余らせ、過大に見せると入らない買いを計画させる。
    reliable = bool((state.get("nisa_used_estimate") or {}).get("reliable", True))
    tag = "" if reliable else "（推定）"
    for key, label in (("growth", "NISA成長投資枠"), ("tsumitate", "NISAつみたて枠")):
        b = (nisa.get("buckets") or {}).get(key) or {}
        if not b.get("limit_jpy"):
            continue
        lines.append(f"  {label}{tag} 残 {_money(b.get('remaining_jpy'))} / "
                     f"{_money(b.get('limit_jpy'))}（使用率 {_pct(b.get('used_pct'))}）")
    if nisa.get("message"):
        prefix = "→ " if reliable else "→ （推定ベース）"
        lines.append(f"  {prefix}{nisa['message']}")
    if not reliable:
        lines.append("  ⚠️ 上の NISA 使用額は保有の取得額からの推定で、"
                     "**当年に使った枠ではありません**。"
                     "実際の残枠は証券会社の記録で確認してください。")

    for w in state.get("warnings") or []:
        lines.append(f"  ⚠️ {w}")

    lines.append("")
    lines.append("  ℹ️ すべて概算であり、税務助言ではありません。"
                 "税率・枠は config/tax.yaml の値を使っています。")
    lines.append("")
    return "\n".join(lines)


def format_switch_evaluation(symbol: str, hurdle: dict,
                             evaluation: Optional[dict] = None,
                             expected_edge_pct: Optional[float] = None) -> str:
    """乗り換え提案の税引後再評価。**乗り換え提案には必ずこれを添える。**"""
    if not hurdle or not hurdle.get("available"):
        return (f"  {symbol}: 乗り換え損益分岐を計算できませんでした"
                f"（{hurdle.get('reason') if hurdle else '入力不足'}）。\n")

    lines = [f"  提案: {symbol} を売却して乗り換え", ""]
    if expected_edge_pct is not None:
        lines.append(f"   税引前:  乗り換え先の期待優位  {expected_edge_pct:+.1f}%")
    lines.append(f"   税引後:  売却で確定する税・手数料・為替 "
                 f"{_money(hurdle.get('friction_jpy'))}")
    lines.append(f"            乗り換え損益分岐  "
                 f"**{_pct(hurdle.get('hurdle_pct'))}** 上回る必要")
    if evaluation:
        lines.append(f"   判定:   {evaluation.get('message')}")
    lines.append("")
    return "\n".join(lines)


def format_loss_harvest(items: list[dict]) -> str:
    """含み損の税務価値。**売却推奨ではない**ことを必ず添える。"""
    usable = [i for i in items or [] if i.get("available")]
    if not usable:
        return ""

    lines = ["■ 含み損の資産価値（情報提供）", ""]
    for i in usable:
        lines.append(f"  {i.get('label') or i.get('symbol')} "
                     f"含み損 {_money(i.get('unrealized_loss_jpy'))}")
        lines.append(f"  → {i.get('message')}")
        lines.append(f"     {i.get('caveat')}")
        lines.append("")
    lines.append("  ℹ️ 節税を理由に売る提案はしません。売買は投資理由に基づいて決めてください。")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 資金ランウェイ
# ---------------------------------------------------------------------------


def format_runway(bundle: dict) -> str:
    """現金・週次投資可能額・ランウェイ・現金の目的。"""
    if not bundle:
        return "■ 資金ランウェイ\n  取得できませんでした。\n"

    est = bundle.get("estimate") or {}
    rway = bundle.get("runway") or {}
    cash = bundle.get("cash") or {}

    lines = ["■ 資金ランウェイ", ""]
    lines.append(f"  現金残高            {_money(cash.get('cash_jpy'))}"
                 f"（評価額比 {_pct(cash.get('cash_pct'))}）")
    if est.get("available"):
        lines.append(f"  週次投資可能額      約 {_money(est.get('weekly_jpy'))}")
        lines.append(f"                      （{est.get('note')}）")
    else:
        lines.append(f"  週次投資可能額      推定できず — {est.get('note')}")

    if rway.get("available"):
        lines.append(f"  {rway.get('weeks')}週後の累積投資可能額  "
                     f"約 {_money(rway.get('cumulative_jpy'))}")
    lines.append("")

    if cash.get("warning"):
        lines.append("  現金の目的:")
        lines.append(f"    ⚠️ {cash['warning']}")
        lines.append("")
    elif cash.get("purposes"):
        lines.append("  現金の目的:")
        for p in cash["purposes"]:
            lines.append(f"    - {p.get('label')}: {_money(p.get('amount_jpy'))}")
        lines.append("")

    if rway.get("caveat"):
        lines.append(f"  ℹ️ {rway['caveat']}")
        lines.append("")
    return "\n".join(lines)


def format_funding_options(result: dict, target_label: str = "") -> str:
    """(a)売却 /(b)現金 /(c)入金待ち /(d)規模縮小 の比較。

    設計書 提案9-⑦ の核心 —— 従来のシステムが構造的に出力し得なかった
    「今回は何もしない。N週後の入金で埋める」を出せるようにする節。
    """
    if not result or not result.get("options"):
        return ""

    lines = ["■ 実行方法の比較（入金代替の検討）", ""]
    if target_label:
        lines.append(f"  対象: {target_label}"
                     f"（想定投資額 {_money(result.get('target_jpy'))}）")
        lines.append("")

    letters = "abcdefgh"
    for i, o in enumerate(result["options"]):
        mark = " ★推奨" if o.get("recommended") else ""
        skip = "" if o.get("viable") else "（不可）"
        lines.append(f"   ({letters[i]}) {o.get('label')}{skip}{mark}")
        lines.append(f"       → {o.get('detail')}")
    lines.append("")
    lines.append(f"  ℹ️ {result.get('note')}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 注意予算
# ---------------------------------------------------------------------------


def format_attention(budget: dict) -> str:
    if not budget:
        return ""
    lines = ["■ 注意予算", ""]
    lines.append(f"  保有銘柄数        {budget.get('holdings')}")
    lines.append(f"  週次レビュー時間  推定 {budget.get('weekly_minutes'):.0f}分")
    lines.append(f"  1銘柄あたり       約 "
                 f"{budget.get('minutes_per_holding') or '—'}分")
    lines.append("")
    if budget.get("warning"):
        lines.append(f"  ⚠️ {budget['warning']}")
    if budget.get("orphan_note"):
        lines.append(f"  ⚠️ {budget['orphan_note']}")
    if budget.get("guidance"):
        lines.append(f"  参考: {budget['guidance']}")
    lines.append("")
    return "\n".join(lines)


def format_constraints(bundle: dict) -> str:
    """制約セクション全体（第4セクション）をまとめて出す。"""
    parts = [
        format_tax_state(bundle.get("tax_state") or {}),
        format_runway(bundle.get("runway_bundle") or {}),
        format_loss_harvest(bundle.get("loss_harvest") or []),
        _format_liquidity(bundle.get("liquidity")),
        format_attention(bundle.get("attention") or {}),
    ]
    return "\n".join(p for p in parts if p)


def _format_liquidity(liquidity: Optional[dict]) -> str:
    """流動性（提案6）。行動可能な空間の一部なので制約側に置く。"""
    if not liquidity:
        return ""
    try:
        from src.output.liquidity_formatter import format_liquidity_section

        return format_liquidity_section(liquidity)
    except Exception:
        return ""
