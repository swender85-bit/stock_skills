"""Fable5 各案の health / レポート出力への配線 (案A P5 / 案1 / 案2 / 案C P4).

各案のコア層は独立に動くが、ユーザーの目に触れるのは health とレポートなので、
そこに出す整形をここに集約する。

全セクションが graceful degradation する。データが無ければ節ごと出さないか、
「測れていない」と明示する（**測れていないことを「問題なし」と誤読させない**）。
"""

from __future__ import annotations

from typing import Any, Iterable, Optional


def format_policy_coverage(holdings: list[dict]) -> str:
    """政策カバレッジ率と逸脱 (案A P5).

    「決めた通りに動けたか」という新しい成績軸。
    """
    try:
        from src.core.policy import coverage_rate
    except ImportError:
        return ""

    try:
        coverage = coverage_rate(holdings)
    except Exception:
        return ""

    if not coverage.get("holdings_count"):
        return ""

    lines = [
        "### 政策カバレッジ (案A)",
        "",
        f"- 撤退条件が定義済み: {coverage['covered_count']}/{coverage['holdings_count']}銘柄"
        f"（評価額ベース {coverage['value_rate'] * 100:.1f}%）",
    ]

    uncovered = coverage.get("uncovered_symbols") or []
    if uncovered:
        lines.append(f"- ⚠️ 事前応答が白紙: {', '.join(uncovered)}")
        lines.append(
            "  - 急変時にその場で判断することになる。平時のいま政策を決めておくこと。"
        )
    else:
        lines.append("- ✅ 全保有に事前応答あり。")

    lines.append("")
    return "\n".join(lines)


def format_deviations(
    trades: Optional[list[dict]] = None,
    market_states: Optional[dict] = None,
) -> str:
    """政策と実際の売買の乖離 (案A P5)。"""
    if not trades:
        return ""
    try:
        from src.core.policy import detect_deviations
    except ImportError:
        return ""

    try:
        deviations = detect_deviations(trades, market_states=market_states)
    except Exception:
        return ""

    if not deviations:
        return ""

    lines = ["### 政策逸脱 (案A)", ""]
    for d in deviations:
        lines.append(f"- **{d['symbol']}**: {d['detail']}")
    lines += [
        "",
        "> 逸脱は「過程」の誤りなので、案Bの二層化では origin=process となり",
        "> plan-check の制約になる資格を持つ。",
        "",
    ]
    return "\n".join(lines)


def format_investor_diagnosis() -> str:
    """質問ストリームからの投資家診断 (案1).

    分析の主語を「銘柄」から「銘柄＋投資家」に広げる節。
    """
    try:
        from src.data.question_log import investor_diagnosis
    except ImportError:
        return ""

    try:
        diagnosis = investor_diagnosis()
    except Exception:
        return ""

    if not diagnosis.get("total"):
        return ""

    lines = [
        "### 投資家診断 (案1)",
        "",
        "> システムは市場を記憶しているが、観測者も記憶する。",
        "> あなたの問いの種類・頻度・タイミングは、あなた自身の認知状態の時系列。",
        "",
        diagnosis["insight"],
        "",
    ]
    return "\n".join(lines)


def format_assumption_concentration(holdings: list[dict]) -> str:
    """前提空間の集中度 (案2)。"""
    try:
        from src.core.risk.assumptions import analyze_assumption_space
    except ImportError:
        return ""

    try:
        result = analyze_assumption_space(holdings=holdings)
    except Exception:
        return ""

    concentration = result.get("concentration") or {}
    if not concentration:
        return ""

    lines = [
        "### 前提空間の集中度 (案2)",
        "",
        "> セクター分散は資産空間の話。ここで見るのは前提空間の分散。",
        "> 価格相関は前提相関の遅行指標。",
        "",
        concentration.get("message", ""),
        "",
    ]

    exposure = concentration.get("exposure") or {}
    if exposure:
        top = sorted(exposure.items(), key=lambda kv: kv[1], reverse=True)[:3]
        for assumption, share in top:
            symbols = ", ".join((result.get("assumption_map") or {}).get(assumption, []))
            lines.append(f"- {assumption}: 保有の {share * 100:.1f}%（{symbols}）")
        lines.append("")

    return "\n".join(lines)


def format_provenance_health(claims: Optional[Iterable[dict]] = None) -> str:
    """知識の汚染度 — 自己推論比率と最大自己参照深度 (案C P4)。"""
    try:
        from src.core.provenance import load_claims, provenance_summary, regrounding_queue
    except ImportError:
        return ""

    try:
        if claims is None:
            claims = load_claims()
        claims = list(claims)
    except Exception:
        return ""

    if not claims:
        return ""

    summary = provenance_summary(claims)
    lines = [
        "### 知識の汚染度 (案C)",
        "",
        "> 自分の過去の推論が、一次観測と同格で現在の解釈に混入していないか。",
        "",
        summary["summary"],
        "",
    ]

    try:
        queue = regrounding_queue(claims, is_holding=True)
    except Exception:
        queue = []

    if queue:
        lines.append(f"- 再接地が必要な主張: {len(queue)}件（深い順）")
        for claim in queue[:3]:
            lines.append(f"  - 深度{claim.get('depth')}「{claim.get('text', '')[:40]}」")
        lines.append("")

    return "\n".join(lines)


def format_provenance_footer(claims: Optional[Iterable[dict]] = None) -> str:
    """レポート末尾に常設する系譜サマリ (案C P4).

    「本解釈の根拠: 一次観測62%／外部言説21%／自己推論17%（最大深度2）」
    """
    try:
        from src.core.provenance import provenance_summary
    except ImportError:
        return ""

    if not claims:
        return ""

    try:
        summary = provenance_summary(list(claims))
    except Exception:
        return ""

    if not summary.get("total"):
        return ""

    return f"\n---\n\n{summary['summary']}\n"


def format_fable5_health_section(
    holdings: list[dict],
    trades: Optional[list[dict]] = None,
    market_states: Optional[dict] = None,
) -> str:
    """health に足す Fable5 セクション一式。

    出せる節が1つも無ければ空文字を返す（見出しだけが浮かないように）。
    """
    blocks = [
        format_policy_coverage(holdings),
        format_deviations(trades, market_states),
        format_investor_diagnosis(),
        format_assumption_concentration(holdings),
        format_provenance_health(),
    ]
    body = "\n".join(b for b in blocks if b)
    if not body.strip():
        return ""

    return "\n## 構造診断 (Fable5)\n\n" + body
