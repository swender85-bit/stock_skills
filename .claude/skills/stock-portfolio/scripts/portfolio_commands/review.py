"""Portfolio command: review -- Display trade performance review."""

import sys

from portfolio_commands import (
    HAS_PERFORMANCE_REVIEW,
    HAS_REVIEW_FORMATTER,
    format_performance_review,
    get_performance_review,
)


def cmd_review(
    year: int | None = None,
    symbol: str | None = None,
) -> None:
    """売買パフォーマンスレビューを表示する (KIK-441 + 土曜設計書 提案5)。"""
    if not HAS_PERFORMANCE_REVIEW:
        print("Error: get_performance_review が利用できません。")
        sys.exit(1)

    data = get_performance_review(year=year, symbol=symbol)

    if HAS_REVIEW_FORMATTER:
        print(format_performance_review(data, year=year, symbol=symbol))
        _print_execution_audit()
    else:
        # フォールバック: 統計だけプリント
        stats = data.get("stats", {})
        trades = data.get("trades", [])
        print(f"## 売買パフォーマンスレビュー")
        print(f"- 取引件数: {stats.get('total', 0)}")
        if stats.get("win_rate") is not None:
            print(f"- 勝率: {stats['win_rate'] * 100:.1f}%")
        if stats.get("total_pnl") is not None:
            print(f"- 合計実現損益: {stats['total_pnl']:+,.0f}")
        _print_execution_audit()


def _print_execution_audit(days: int = 90) -> None:
    """執行監査を追記する (土曜設計書 提案5)。

    従来の勝率・損益は**執行されなかった判断の成績を含んでいる**。
    決定生存率を併記しないと、システムの成績表とユーザーの実際の資産の
    乖離が見えないままになる。
    """
    try:
        from src.core.portfolio.execution_audit import build_execution_audit
    except Exception:
        return

    try:
        audit = build_execution_audit(days=days)
    except Exception:
        return

    survival = audit.get("survival") or {}
    print()
    print(f"## 執行監査（直近{days}日）")
    print()

    if not survival.get("available"):
        # 「測れていない」を「精度が良い」と誤読させない
        print(f"- 決定生存率: 測定できません — {survival.get('reason')}")
        for e in audit.get("errors") or []:
            print(f"  - {e}")
        print("- ℹ️ 上の勝率は、**実際には執行されなかった判断も含んだ**数字です。")
        return

    print(f"- 生成された判断: {survival['total']}件")
    print(f"- 実際に執行された判断: {survival['executed']}件"
          f"（決定生存率 {survival['rate_pct']}%）")
    print(f"- {survival.get('message')}")

    for m in (audit.get("reasons") or {}).get("messages") or []:
        print(f"  - {m}")

    short = audit.get("shortfall") or {}
    if short.get("available"):
        print(f"- 判断→約定の遅延: 中央値 {short.get('median_delay_days')}日")
        if short.get("avg_shortfall_pct") is not None:
            print(f"- 執行ショートフォール: 平均 {short['avg_shortfall_pct']:+.2f}%")
        if short.get("message"):
            print(f"  {short['message']}")
        if short.get("buy_dip_note"):
            print(f"  ⚠️ {short['buy_dip_note']}")

    perf = audit.get("performance") or {}
    ex, un = perf.get("executed") or {}, perf.get("unexecuted_virtual") or {}
    if ex.get("available") or un.get("available"):
        print()
        print("### 成績の分離表示")
        if ex.get("available"):
            print(f"- 執行済み判断: 勝率 {ex['win_rate_pct']}% / "
                  f"実現損益 {ex['total_pnl']:+,.0f}")
        if un.get("available"):
            print(f"- 未執行判断（仮想）: 勝率 {un['win_rate_pct']}% / "
                  f"仮想損益 {un['total_pnl']:+,.0f}")
        print(f"- ℹ️ {perf.get('note')}")
