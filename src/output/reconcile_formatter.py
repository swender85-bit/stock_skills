"""三点照合の出力整形 (土曜設計書 提案1-⑥)。

レポート第1セクション。**ここが通らなければ以降を実行しない**という位置づけなので、
「一致した」よりも「一致を確認できなかった」を目立たせる。
"""

from __future__ import annotations

from typing import Optional

_STATUS_LABEL = {
    "ok": "✅ 一致",
    "differences_explained": "🟡 差分あり（説明済み）",
    "differences": "🔴 要対応の差分あり",
    "circular": "🟡 独立検証なし",
    "unreconciled": "⛔ 照合不能",
}


def _money(v: Optional[float]) -> str:
    return f"¥{v:,.0f}" if isinstance(v, (int, float)) else "—"


def _pct(v: Optional[float]) -> str:
    return f"{v:.1f}%" if isinstance(v, (int, float)) else "—"


def _label(row: dict) -> str:
    sym = row.get("symbol")
    name = row.get("name") or "無名"
    return f"{name}（{sym}）" if sym else name


def format_reconciliation(result: dict) -> str:
    """照合結果を Markdown で返す。"""
    if not result:
        return "■ 三点照合\n  照合が実行されませんでした。\n"

    c = result.get("counts") or {}
    lines: list[str] = []
    lines.append("■ 三点照合（模型 / 実在 / 意図）")
    lines.append("")
    lines.append(f"  判定: {_STATUS_LABEL.get(result.get('status'), result.get('status'))}")
    lines.append(f"  口座: {c.get('broker', 0)}銘柄 / 模型: {c.get('model', 0)}銘柄 / "
                 f"一致 {c.get('matched', 0)} / 差分 {c.get('diffs', 0)}")

    for s in result.get("sources") or []:
        mark = "✓" if s.get("available") else "✗"
        lines.append(f"  {mark} {s.get('summary')}")
    lines.append("")

    for m in result.get("messages") or []:
        lines.append(f"  {m}")
    if result.get("messages"):
        lines.append("")

    lines += _block("🔴 原因不明の差分", result.get("unknown_diffs"), _diff_line,
                    tail="  → 原因が特定できるまで、以降の数値は暫定として扱ってください。")
    lines += _block("⚠️ 幽霊ポジション（模型にあるが口座に不在）",
                    result.get("ghosts"), _ghost_line,
                    tail="  → 売却記録を確認してください。"
                         "確認できるまでストレステスト・HHIから除外すべきです。")
    lines += _block("⚠️ 未記録ポジション（口座にあるが模型に無い）",
                    result.get("unrecorded"), _unrecorded_line,
                    tail="  → `python scripts/import_rakuten_csv.py` を実行してください。")
    lines += _block("ℹ️ コーポレートアクション由来の差分",
                    result.get("corporate_actions"), _ca_line)
    lines += _block("ℹ️ 照合できなかった銘柄",
                    result.get("unverified"), _unverified_line,
                    tail="  → これらは「一致」でも「幽霊」でもありません。**残高不明**です。")

    lines += _orphan_block(result)
    return "\n".join(lines).rstrip() + "\n"


def _block(title: str, rows, fmt, tail: str = "") -> list[str]:
    if not rows:
        return []
    out = [f"  {title} {len(rows)}件"]
    for r in rows:
        out.extend(fmt(r))
    if tail:
        out.append(tail)
    out.append("")
    return out


def _diff_line(r: dict) -> list[str]:
    return [f"     {_label(r)} — 模型 {r.get('model_shares')}株 / "
            f"口座 {r.get('broker_shares')}株",
            f"        {r.get('message')}"]


def _ghost_line(r: dict) -> list[str]:
    return [f"     {_label(r)} — 模型 {r.get('shares')}株 / 評価額 "
            f"{_money(r.get('value_jpy'))}（比率 {_pct(r.get('weight_pct'))}）",
            f"        {r.get('message')}"]


def _unrecorded_line(r: dict) -> list[str]:
    return [f"     {_label(r)} — 口座 {r.get('broker_shares')}株",
            f"        {r.get('message')}"]


def _ca_line(r: dict) -> list[str]:
    return [f"     {_label(r)} — {r.get('message')}"]


def _unverified_line(r: dict) -> list[str]:
    return [f"     {_label(r)} — {r.get('reason')}"]


def _orphan_block(result: dict) -> list[str]:
    orphans = result.get("orphans") or []
    if not orphans:
        return ["  ✅ 孤児ポジションはありません（全保有に thesis または政策があります）。", ""]

    burden = result.get("orphan_burden_pct")
    out = [f"  ⚠️ 孤児ポジション {len(orphans)}件（thesis・政策の双方なし）"]
    for o in orphans:
        out.append(f"     {_label(o)} — 評価額 {_money(o.get('value_jpy'))}"
                   f"（比率 {_pct(o.get('weight_pct'))}）")
    if burden is not None:
        out.append(f"  → 保有額の {burden}% が「なぜ持っているか未記述」の状態です。")
    else:
        out.append("  → これらは「なぜ持っているか」が記述されていません。")
    out.append("  → 損切りも利確も判断基準が無いポジションです。今週の議題に上げてください。")
    out.append("")
    return out


def format_compact(result: dict) -> str:
    """静穏週用の1〜2行版（提案8の折り畳み対象）。"""
    if not result:
        return "照合    未実行"
    c = result.get("counts") or {}
    if result.get("status") == "ok" and not result.get("orphans"):
        return "照合    差分なし（口座と模型が一致）"
    bits = []
    if c.get("diffs"):
        bits.append(f"差分{c['diffs']}件")
    if c.get("ghosts"):
        bits.append(f"幽霊{c['ghosts']}件")
    if c.get("unrecorded"):
        bits.append(f"未記録{c['unrecorded']}件")
    if c.get("orphans"):
        bits.append(f"孤児{c['orphans']}件")
    if result.get("status") == "circular":
        bits.append("独立検証なし")
    if result.get("status") == "unreconciled":
        bits.append("照合不能")
    return "照合    " + "・".join(bits or ["差分なし"])
