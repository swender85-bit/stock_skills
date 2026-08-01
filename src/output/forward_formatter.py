"""前方イベントセクションの出力整形 (土曜設計書 提案4-⑥)。

固定骨格の第3セクション。レポートを「読み物」から**来週の作戦書**に変える。

出力の原則:
- 日程は変更され得る。出典と取得時刻を添える。
- 先物・ADR は予測ではなく市場の織り込み。断定しない。
- 取得できなかった銘柄は「予定なし」ではなく「取得できなかった」と書く。
"""

from __future__ import annotations

from typing import Any, Optional


def _pct(v: Any, digits: int = 1) -> str:
    return f"{v:.{digits}f}%" if isinstance(v, (int, float)) else "—"


def _weight(v: Any) -> str:
    return f"評価額比 {v:.1f}%" if isinstance(v, (int, float)) else "評価額比 —"


_KIND_ORDER = {"fomc": 0, "economic": 1, "earnings": 2, "ex_dividend": 3}


def format_calendar(calendar: dict,
                    resolved_via_lookthrough: Optional[list] = None) -> str:
    if not calendar:
        return "■ 翌週の確定イベント\n  取得できませんでした。\n"

    rng = calendar.get("range") or {}
    lines = [f"■ 翌週の確定イベント（{rng.get('start')} - {rng.get('end')}）", ""]

    events = calendar.get("events") or []
    if not events:
        lines.append("  翌週に該当するイベントは検出されませんでした。")
    else:
        by_day: dict[str, list[dict]] = {}
        for e in events:
            by_day.setdefault(str(e.get("date")), []).append(e)
        for day in sorted(by_day):
            rows = sorted(by_day[day],
                          key=lambda e: _KIND_ORDER.get(e.get("kind"), 9))
            label = rows[0].get("day_label") or day
            for i, e in enumerate(rows):
                head = f"  {label}" if i == 0 else "  " + " " * len(label)
                lines.append(f"{head}  ─ {_event_line(e)}")
    lines.append("")

    folded = calendar.get("folded") or []
    if folded:
        lines.append(f"  [折り畳み] 評価額比が小さいイベント {len(folded)}件")
    missing = calendar.get("unavailable_symbols") or []
    if missing:
        expanded = set(resolved_via_lookthrough or [])
        etf = [s for s in missing if s in expanded]
        truly_missing = [s for s in missing if s not in expanded]
        if etf:
            # ETF に決算は「無い」。取得失敗と混同しない。中身に読み替えた旨を書く。
            lines.append(f"  ℹ️ {', '.join(etf)} は ETF/投信のため決算がありません。"
                         "中身の企業の決算に読み替えました（下記ルックスルー参照）。")
        if truly_missing:
            lines.append(f"  ⚠️ 日程を取得できなかった銘柄: {', '.join(truly_missing)}")
            lines.append("     → これは「予定なし」ではありません。**取得できませんでした。**")
    lines.append(f"  ℹ️ {calendar.get('note')}")
    lines.append("")
    return "\n".join(lines)


def format_lookthrough_events(bundle: dict) -> str:
    """ETF経由で曝されている翌週の決算（提案4の穴を埋める節）。"""
    lt = bundle.get("lookthrough") or {}
    ev = bundle.get("lookthrough_events") or {}
    if not lt.get("available"):
        return ""

    lines = ["  ETFルックスルー（中身の企業への実質エクスポージャー）", ""]

    top = (lt.get("effective") or [])[:8]
    if top:
        for r in top:
            via = "直接" if r.get("direct_pct") and not r.get("via_etf_pct") else \
                  "、".join(str(s) for s in (r.get("sources") or []))
            lines.append(f"     {r['symbol']:<8} 実質 {r['effective_pct']:>5.1f}%"
                         f"（{via}）")
        lines.append("")

    if ev.get("available") and ev.get("events"):
        lines.append(f"  ⚠️ {ev.get('message')}")
        for e in ev["events"]:
            lines.append(f"     {e.get('day_label')}  {e.get('symbol')} 決算 — "
                         f"実質 {e.get('effective_pct'):.1f}%")
        lines.append("")
    elif ev.get("available"):
        lines.append(f"  ✅ {ev.get('message')}")
        lines.append("")

    unresolved = lt.get("unresolved") or []
    if unresolved:
        lines.append(f"  ⚠️ 中身を展開できなかった保有 {len(unresolved)}件")
        for u in unresolved:
            lines.append(f"     {u.get('symbol') or u.get('name')} — {u.get('reason')}")
        lines.append("     → 中身が無いのではなく、構成を取得できていません。")
        lines.append("")

    lines.append(f"  ℹ️ {lt.get('note')}")
    if ev.get("caveat"):
        lines.append(f"  ℹ️ {ev['caveat']}")
    lines.append("")
    return "\n".join(lines)


def _event_line(e: dict) -> str:
    kind = e.get("kind")
    if kind == "earnings":
        return f"保有 {e.get('name') or e.get('symbol')} 決算  {_weight(e.get('weight_pct'))}"
    if kind == "ex_dividend":
        tail = "（祝日でずれる可能性あり）" if e.get("holiday_caveat") else ""
        return (f"保有 {e.get('name') or e.get('symbol')} "
                f"{'権利付最終日' if e.get('holiday_caveat') else '配当落ち日'}"
                f"{tail}  {_weight(e.get('weight_pct'))}")
    if kind == "fomc":
        prob = e.get("top_prob")
        tail = (f"（市場織り込み: {e.get('top_range')} {prob}）"
                if e.get("top_range") else "")
        return f"FOMC 政策金利発表{tail}"
    if kind == "economic":
        star = e.get("importance")
        mark = f"[重要度{star}] " if star else ""
        return f"{mark}{e.get('country') or ''} {e.get('title') or ''}".strip()
    return str(e.get("title") or kind)


def format_concentration(conc: dict) -> str:
    if not conc or not conc.get("message"):
        return ""
    icon = {"danger": "🔴", "warning": "⚠️", "ok": "ℹ️"}.get(conc.get("level"), "ℹ️")
    lines = [f"  {icon} イベント集中度: {_pct(conc.get('pct'))}", f"     {conc['message']}"]
    if conc.get("unweighted_note"):
        lines.append(f"     ℹ️ {conc['unweighted_note']}")
    lines.append("")
    return "\n".join(lines)


def format_policy_gaps(gaps: dict) -> str:
    """政策カバレッジの穴。設計書と第2弾・案Aの合流点。"""
    if not gaps:
        return ""
    rows = gaps.get("gaps") or []
    if not rows:
        note = gaps.get("note")
        covered = len(gaps.get("covered") or [])
        if covered:
            return f"  ✅ 決算を迎える{covered}銘柄すべてに対応政策があります。\n\n"
        return f"  ℹ️ {note}\n\n" if note else ""

    lines = [f"  ⚠️ 政策カバレッジの穴 {len(rows)}件", ""]
    for g in rows:
        lines.append(f"     {g.get('name') or g.get('symbol')}"
                     f"（{g.get('date')} 決算 / {_weight(g.get('weight_pct'))}）")
    lines.append(f"     {gaps.get('message')}")
    lines.append("")
    lines.append("     登録コマンド:")
    lines.append(f"       {gaps.get('how_to')}")
    lines.append("")
    return "\n".join(lines)


def format_triggers(triggers: dict) -> str:
    if not triggers or not triggers.get("available"):
        return ""
    met = triggers.get("met") or []
    near = triggers.get("approaching") or []
    if not met and not near:
        return f"  ✅ {triggers.get('message')}\n\n"

    lines = []
    if met:
        lines.append(f"  🔴 政策トリガー成立 {len(met)}件")
        for r in met:
            lines.append(f"     {r.get('symbol')}: {r.get('metric')} "
                         f"{r.get('op')} {r.get('value')}（実測 {r.get('actual')}）")
            lines.append(f"        既定行動: {r.get('response')}")
        lines.append("     → 政策に従ってください。**ここで新たに判断しないこと。**")
        lines.append("")
    if near:
        lines.append(f"  ⚠️ 政策トリガー接近中 {len(near)}件")
        for r in near:
            lines.append(f"     {r.get('symbol')}: 残り {r.get('distance')} "
                         f"（{r.get('metric')} {r.get('op')} {r.get('value')}）")
        lines.append("")
    return "\n".join(lines)


def format_dividend_drops(drops: dict) -> str:
    if not drops or not drops.get("items"):
        return ""
    lines = ["  ℹ️ 配当落ちの分離", ""]
    for d in drops["items"]:
        y = d.get("dividend_yield_pct")
        lines.append(f"     {d.get('name') or d.get('symbol')}"
                     f" — {d.get('ex_date')} に権利落ち"
                     f"（{d.get('days_ago')}日前"
                     + (f" / 配当利回り {y}%" if y else "") + "）")
    lines.append(f"     {drops.get('message')}")
    lines.append("")
    return "\n".join(lines)


def format_monday_outlook(outlook: dict) -> str:
    if not outlook:
        return ""
    lines = ["■ 月曜寄付の見通し（市場の見解）", ""]
    close = outlook.get("nikkei_close")
    fut = (outlook.get("futures") or {}).get("price")
    if isinstance(fut, (int, float)):
        lines.append(f"  日経225先物（週末値）  {fut:,.0f}")
    if isinstance(close, (int, float)):
        lines.append(f"  金曜 東証終値          {close:,.0f}")
    lines.append(f"  → {outlook.get('message')}")
    lines.append("")
    lines.append(f"  ℹ️ {outlook.get('disclaimer')}")
    lines.append("")
    return "\n".join(lines)


def format_schedule_changes(changes: dict) -> str:
    if not changes:
        return ""
    if not changes.get("available"):
        return f"  ℹ️ 日程変更の検出: {changes.get('reason')}\n\n"
    rows = changes.get("changes") or []
    if not rows:
        return "  ✅ 前週から日程の変更はありません。\n\n"
    lines = [f"  ⚠️ 日程変更 {len(rows)}件（決算日の変更はしばしば重要なシグナル）", ""]
    for c in rows:
        lines.append(f"     {c.get('symbol')} {c.get('kind')}: "
                     f"{c.get('previous_date')} → {c.get('current_date') or '削除'}")
    lines.append("")
    return "\n".join(lines)


def format_forward_section(bundle: dict) -> str:
    """第3セクション全体。"""
    if not bundle:
        return "■ 翌週の確定イベント\n  取得できませんでした。\n"

    lt = bundle.get("lookthrough") or {}
    expanded = [r.get("symbol") for r in (lt.get("resolved_etfs") or [])
                if r.get("symbol")]
    parts = [
        format_calendar(bundle.get("calendar") or {}, expanded),
        format_concentration(bundle.get("concentration") or {}),
        format_lookthrough_events(bundle),
        format_policy_gaps(bundle.get("policy_gaps") or {}),
        format_triggers(bundle.get("triggers") or {}),
        format_dividend_drops(bundle.get("dividend_drops") or {}),
        format_schedule_changes(bundle.get("schedule_changes") or {}),
        format_monday_outlook(bundle.get("monday_outlook") or {}),
    ]
    text = "\n".join(p for p in parts if p)
    errors = bundle.get("errors") or []
    if errors:
        text += "\n  ⚠️ 一部の前方イベント材料を取得できませんでした:\n"
        for e in errors:
            text += f"     - {e}\n"
    return text


def format_compact(bundle: dict) -> str:
    """静穏週用の1行版（提案8の折り畳み対象）。"""
    if not bundle:
        return "翌週    未取得"
    cal = bundle.get("calendar") or {}
    earnings = [e for e in (cal.get("events") or []) + (cal.get("folded") or [])
                if e.get("kind") == "earnings"]
    gaps = (bundle.get("policy_gaps") or {}).get("gaps") or []
    bits = [f"保有の決算 {len(earnings)}件"]
    if gaps:
        bits.append(f"政策未定義 {len(gaps)}件")
    conc = bundle.get("concentration") or {}
    if conc.get("level") in ("warning", "danger"):
        bits.append(f"集中度 {conc.get('pct')}%")
    return "翌週    " + "・".join(bits)
