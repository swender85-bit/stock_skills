"""情報量比例レポートの出力整形 (土曜設計書 提案8-⑥)。

冒頭に必ず「今週の情報量」判定を置く。静穏週は数行で終わる —— それが正しい出力。
折り畳んだ項目は**削除しない**。常に展開できる形で残す。
"""

from __future__ import annotations

from typing import Any, Optional


def _fmt_delta(c: dict) -> str:
    d = c.get("delta")
    unit = c.get("unit") or ""
    if not isinstance(d, (int, float)):
        cur, prev = c.get("current"), c.get("previous")
        return f"{prev} → {cur}"
    return f"{d:+.1f}{unit}"


def format_verdict(assessment: dict, diff: dict,
                   cumulative: Optional[dict] = None) -> str:
    """レポート冒頭の判定ブロック。"""
    if not assessment:
        return "■ 今週の判定：（未計算）\n"

    n = assessment.get("actionable_count", 0)
    lines = [f"■ 今週の判定：{assessment.get('verdict')}（要対応 {n}件）", ""]

    if assessment.get("quiet"):
        lines += _quiet_body(assessment, diff, cumulative)
    else:
        lines += _busy_body(assessment)

    lines.append("  ─────────────────────────────")
    folded = diff.get("folded") or []
    lines.append(f"  [折り畳み] 閾値未満の変化 {len(folded)}件"
                 "（消えていません。必要なら展開できます）")
    if not assessment.get("quiet"):
        lines.append("  [折り畳み] 数値の詳細・スクリーニング結果")
    lines.append("")
    return "\n".join(lines)


def _quiet_body(assessment: dict, diff: dict, cumulative: Optional[dict]) -> list[str]:
    lines = [
        f"  先週（{diff.get('previous_date') or '—'}）からの実質的な変化はありません。",
        "  今週は何もしないことが正しい選択です。",
        "",
        # 「動いているか不安」を消すため、静穏週でも点検実績を明示する（提案8-⑧）
        f"  システムは正常に動作し、{assessment.get('checked_count', 0)}項目を点検しました。",
    ]
    slow = (cumulative or {}).get("slow_drift") or []
    if slow:
        lines.append("")
        lines.append("  ℹ️ 週次では閾値に届かない緩慢な変化があります:")
        for s in slow[:3]:
            lines.append(f"     {s.get('label')} — {s.get('window_weeks')}週で "
                         f"{_fmt_delta(s)}")
    lines.append("")
    return lines


def _busy_body(assessment: dict) -> list[str]:
    lines = []
    for i, a in enumerate(assessment.get("actionable") or [], 1):
        tag = {"belief": "信念", "reconciliation": "照合", "forward": "翌週",
               "holding": "保有", "slow_drift": "緩慢"}.get(a.get("kind"), "その他")
        lines.append(f"  {i}. 【{tag}】{a.get('title')}")
        if a.get("detail"):
            lines.append(f"     {a['detail']}")
    lines.append("")
    return lines


def format_belief_section(falsification: dict) -> str:
    """第2セクション「信念の変化」。価格ではなく信念を最初に見る。"""
    if not falsification:
        return "■ 信念の変化\n  点検できませんでした。\n"

    lines = ["■ 信念の変化（反証条件の点検）", ""]

    fal = falsification.get("falsified") or []
    if fal:
        lines.append(f"  🔴 反証条件が成立した保有 {len(fal)}件")
        for r in fal:
            lines.append(f"     {r.get('name') or r.get('symbol')} — テーゼ「"
                         f"{(r.get('content') or '')[:60]}」")
            lines.append(f"        {r.get('message')}")
        lines.append("  → これは売り推奨ではありません。"
                     "テーゼを書き直すか退出するかを決める議題です。")
        lines.append("")
    else:
        lines.append(f"  ✅ 反証条件に抵触した保有 0件（{falsification.get('intact', 0)}件が健在）")
        lines.append("")

    near = falsification.get("near") or []
    if near:
        lines.append(f"  ⚠️ 反証条件に接近 {len(near)}件")
        for r in near:
            lines.append(f"     {r.get('name') or r.get('symbol')} — {r.get('message')}")
        lines.append("")

    unchecked = falsification.get("unchecked") or []
    if unchecked:
        lines.append(f"  ℹ️ 指標が取れず**点検できなかった** {len(unchecked)}件")
        for r in unchecked:
            lines.append(f"     {r.get('name') or r.get('symbol')}")
        lines.append("  → これは「問題なし」ではありません。未点検です。")
        lines.append("")

    missing = falsification.get("missing") or []
    if missing:
        lines.append(f"  ⚠️ 反証条件が未定義の thesis {len(missing)}件")
        for r in missing:
            lines.append(f"     {r.get('name') or r.get('symbol')} — "
                         f"「{(r.get('content') or '')[:50]}」")
        lines.append("  → 『何が起きたらこのテーゼは間違いだったと認めるか』を1行足してください。")
        lines.append("     例: `operating_margin < 8` / `revenue_growth < 0`")
        lines.append("  → 反証条件の無いテーゼは、週次で点検する対象を持ちません。")
        lines.append("")

    return "\n".join(lines)


def format_changes(diff: dict, limit: int = 20) -> str:
    """有意な変化の一覧（折り畳み対象の外側）。"""
    if not diff.get("available"):
        return f"■ 前週比\n  {diff.get('reason') or '比較できません'}\n"
    changes = diff.get("changes") or []
    if not changes:
        return f"■ 前週比（{diff.get('previous_date')}）\n  閾値を超えた変化はありません。\n"

    lines = [f"■ 前週比（{diff.get('previous_date')}）— 有意な変化 {len(changes)}件", ""]
    for c in changes[:limit]:
        note = f" … {c['note']}" if c.get("note") else ""
        lines.append(f"  {c.get('label')}: {_fmt_delta(c)}{note}")
    if len(changes) > limit:
        lines.append(f"  … 他 {len(changes) - limit}件")
    lines.append("")
    return "\n".join(lines)


def format_cumulative(cumulative: dict) -> str:
    """4週・13週の累積差分。緩慢な悪化を見逃さないための第二の網。"""
    if not cumulative:
        return ""
    lines = ["■ 累積差分（緩慢な変化の監視）", ""]
    for name, w in (cumulative.get("windows") or {}).items():
        if not w.get("available"):
            lines.append(f"  {name}: {w.get('reason')}")
            continue
        drifts = w.get("drifts") or []
        lines.append(f"  {name}（{w.get('previous_date')} 比）: "
                     f"有意 {w.get('significant')}件 / 緩慢な変化 {len(drifts)}件")
        for d in drifts[:5]:
            lines.append(f"     {d.get('label')} — {_fmt_delta(d)}")
    lines.append("")
    return "\n".join(lines)
