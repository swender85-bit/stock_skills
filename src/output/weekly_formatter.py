"""週次PF分析レポートの Markdown 整形 (Obsidian 向け).

frontmatter + wikiリンクを含む Obsidian Markdown を出力する。
数値が取れなかった項目は「—」で埋め、取れなかったこと自体を隠さない。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from src.core.portfolio.projection import HORIZON_LABELS


def _yen(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"¥{value:,.0f}"


def _pct(value: Optional[float], digits: int = 1, sign: bool = False) -> str:
    if value is None:
        return "—"
    fmt = f"{{:+.{digits}f}}%" if sign else f"{{:.{digits}f}}%"
    return fmt.format(value)


def _num(value: Optional[float], digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:,.{digits}f}"


def _ratio_pct(value: Optional[float]) -> str:
    """0-1 の比率 or 既に % の値を % 表示にする。"""
    if value is None:
        return "—"
    v = float(value)
    if -1.5 <= v <= 1.5:
        v *= 100.0
    return f"{v:.1f}%"


def _heat_emoji(state: str) -> str:
    return {
        "overbought": "🔴",
        "oversold": "🔵",
        "neutral": "⚪",
        "unknown": "❔",
    }.get(state, "❔")


def format_weekly_report(data: dict, as_of: Optional[datetime] = None) -> str:
    """週次レポート全体を Markdown 文字列にする。"""
    now = as_of or data.get("generated_at") or datetime.now()
    stamp = now.strftime("%Y-%m-%d")

    parts = [
        _frontmatter(data, stamp),
        _header(data, now),
        _summary(data),
        _holdings_table(data),
        _projection(data),
        _scenarios(data),
        _per_stock(data),
        _heat_overview(data),
        _footer(data),
    ]
    return "\n".join(p for p in parts if p)


# ---------------------------------------------------------------------------


def _frontmatter(data: dict, stamp: str) -> str:
    return (
        "---\n"
        f"title: 週次ポートフォリオ分析 {stamp}\n"
        "tags:\n"
        "  - 投資\n"
        "  - ポートフォリオ\n"
        "  - 週次レポート\n"
        f"created: {stamp}\n"
        f"holdings_source: {data.get('holdings_source', 'unknown')}\n"
        "---\n"
    )


#: 保有構成の取り込みがこれより古いと警告する（日）
_IMPORT_STALE_DAYS = 30


def _import_age_days(exported_at: Any) -> Optional[int]:
    """取り込み時刻からの経過日数。判定できなければ None。

    ここで 0 を返すと「古くない」と誤読されるので、
    パースできない場合は必ず None を返す。
    """
    if not exported_at:
        return None
    try:
        from datetime import timezone

        ts = datetime.fromisoformat(str(exported_at))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - ts).days)
    except (ValueError, TypeError):
        return None


def _header(data: dict, now: datetime) -> str:
    source = data.get("holdings_source")
    imported = data.get("holdings_import") or {}
    source_label = {
        "rakuten-rss": "楽天証券 MarketSpeed II RSS（実口座）",
        "config": "config/weekly_holdings.yaml（CLAUDE.md 基準スナップショット）",
    }.get(source, source)
    if source == "config" and imported.get("type") == "rakuten_csv":
        source_label = "楽天証券 保有商品一覧CSV（実口座）"

    lines = [
        f"# 週次ポートフォリオ分析 {now.strftime('%Y-%m-%d')}",
        "",
        f"- 生成: {now.strftime('%Y-%m-%d %H:%M')} UTC",
        f"- 保有データ源: **{source_label}**",
        f"- 為替: ¥{data.get('fx_rate', 0):,.2f}/USD",
    ]

    snapshot = data.get("rss_snapshot")
    if snapshot:
        lines.append(
            f"- RSSスナップショット: {snapshot.get('source_path')} "
            f"({snapshot.get('age_hours')}時間前 / {snapshot.get('row_count')}銘柄)"
        )
        freshness = snapshot.get("_freshness")
        if freshness and not freshness.get("fresh"):
            lines.append(f"- {freshness.get('message')}")
    elif imported.get("type") == "rakuten_csv":
        exported = str(imported.get("exported_at") or "")[:16].replace("T", " ")
        lines.append(
            f"- 保有構成の取り込み: {imported.get('file')}（{exported} 時点）"
        )
        age_days = _import_age_days(imported.get("exported_at"))
        if age_days is not None and age_days > _IMPORT_STALE_DAYS:
            lines.append(
                f"- ⚠️ **保有構成の取り込みから{age_days}日経過しています。**"
                " この間に売買していれば反映されていません。"
                "（楽天証券 → 保有商品一覧（すべて） → CSVで保存 →"
                " `python scripts/import_rakuten_csv.py`）"
            )
        else:
            lines.append(
                "- 株価は毎回自動取得。保有構成は売買時にCSVを取り込むまで固定されます。"
            )
    elif source == "config":
        lines.append(
            "- ⚠️ **楽天RSSスナップショットが読めなかったため、設定ファイルの保有を使用しています。**"
            " 実口座で売買した分は反映されていません。"
        )

    lines.append("")
    return "\n".join(lines)


def _summary(data: dict) -> str:
    total = data.get("total_jpy") or 0.0
    invested = data.get("invested_jpy") or 0.0
    cash = data.get("cash_jpy") or 0.0
    analyses = data.get("analyses") or []

    total_pl = sum(a["pl_jpy"] for a in analyses if a.get("pl_jpy") is not None)
    total_cost = sum(a["cost_jpy"] for a in analyses if a.get("cost_jpy") is not None)
    pl_pct = (total_pl / total_cost * 100.0) if total_cost else None

    week_moves = [
        (a["name"], a["week"]["week_change_pct"])
        for a in analyses
        if a.get("week") and a["week"].get("week_change_pct") is not None
    ]
    week_moves.sort(key=lambda x: x[1], reverse=True)

    lines = [
        "## サマリー",
        "",
        "| 項目 | 金額 |",
        "|:---|---:|",
        f"| 総資産 | {_yen(total)} |",
        f"| 保有評価額 | {_yen(invested)} |",
        f"| 現金 | {_yen(cash)} |",
        f"| 評価損益 | {_yen(total_pl)} ({_pct(pl_pct, sign=True)}) |",
        "",
    ]

    if week_moves:
        lines.append("### 今週の値動き")
        lines.append("")
        best = week_moves[0]
        worst = week_moves[-1]
        lines.append(f"- 最も上昇: **{best[0]}** {_pct(best[1], sign=True)}")
        lines.append(f"- 最も下落: **{worst[0]}** {_pct(worst[1], sign=True)}")
        lines.append("")

    if invested > 0:
        lines.append("### 構成比")
        lines.append("")
        lines.append("| 銘柄 | 評価額 | 総資産比 |")
        lines.append("|:---|---:|---:|")
        for a in sorted(
            [x for x in analyses if x.get("value_jpy")],
            key=lambda x: x["value_jpy"],
            reverse=True,
        ):
            weight = a["value_jpy"] / total * 100.0 if total else 0.0
            lines.append(f"| {a['name']} | {_yen(a['value_jpy'])} | {weight:.1f}% |")
        if cash:
            lines.append(f"| 現金 | {_yen(cash)} | {cash / total * 100.0:.1f}% |")
        lines.append("")

    return "\n".join(lines)


def _holdings_table(data: dict) -> str:
    lines = [
        "## 保有一覧（今週の終値ベース）",
        "",
        "| 銘柄 | 口座 | 数量 | 取得単価 | 今週終値 | 週間 | 評価額(¥) | 損益(¥) | 損益率 | 価格出所 |",
        "|:---|:---|---:|---:|---:|---:|---:|---:|---:|:---|",
    ]
    for a in data.get("analyses") or []:
        week_chg = a["week"]["week_change_pct"] if a.get("week") else None
        lines.append(
            f"| {a['name']} | {a.get('account', '')} | {a['shares']:,.0f} | "
            f"{_num(a['cost_price'])} | {_num(a['price'])} | {_pct(week_chg, sign=True)} | "
            f"{_yen(a['value_jpy'])} | {_yen(a['pl_jpy'])} | {_pct(a['pl_pct'], sign=True)} | "
            f"{a.get('price_source', '')} |"
        )
    lines.append("")
    return "\n".join(lines)


def _projection(data: dict) -> str:
    proj = data.get("projection") or {}
    lines = [
        "## ポートフォリオ推移予測",
        "",
        "> **これは予言ではない。** 下表の前提を置いたときの統計的なレンジ（概ね中央80%区間）。",
        "> レバレッジETFは原資産の前提から `L·μ − (L·σ)²/2` で中央値を導出しており、",
        "> ボラティリティ・ドラッグはこの式に織り込み済み。前提が外れればレンジも外れる。",
        "",
        "| ホライズン | 下限 | 中央 | 上限 | 中央値の変化 |",
        "|:---|---:|---:|---:|---:|",
    ]
    for key in ("short", "mid", "long"):
        p = proj.get(key)
        if not p:
            continue
        lines.append(
            f"| {p['label']} | {_yen(p['low'])} | {_yen(p['mid'])} | {_yen(p['high'])} | "
            f"{_pct(p['change_pct'], sign=True)} |"
        )

    contribution = data.get("monthly_contribution") or 0.0
    lines += [
        "",
        f"- 現在の総資産: {_yen(proj.get('current_total'))}",
        f"- 積立前提: 月 {_yen(contribution)}（FANG+ の DCA 継続を仮定）",
        "",
    ]

    positions = data.get("positions") or []
    if positions:
        lines += [
            "### 置いた前提（原資産ベース）",
            "",
            "| 銘柄 | 原資産 | 想定リターン | 原資産ボラ | 倍率 | 実効ボラ | ドラッグ |",
            "|:---|:---|---:|---:|---:|---:|---:|",
        ]
        for p in positions:
            lev = int(p.get("leverage") or 1)
            eff_vol = float(p.get("annual_vol_pct") or 0.0) * lev
            drag = p.get("drag_pct")
            lines.append(
                f"| {p.get('name', '')} | {p.get('underlying', '—')} | "
                f"{_pct(p.get('annual_return_pct'))} | {_pct(p.get('annual_vol_pct'))} | "
                f"{lev}x | {_pct(eff_vol)} | "
                f"{_pct(drag) if drag else '—'} |"
            )
        lines += [
            "",
            "> レバレッジETFの中央値は `L·μ − (L·σ)²/2` から導出している。",
            "> 原資産のボラティリティが高いほど倍率の減衰が効くため、**同じ3xでも",
            "> SOXL（半導体σ~35%）とTQQQ（NDXσ~22%）では期待値がまるで違う。**",
            "",
        ]

    return "\n".join(lines)


def _scenarios(data: dict) -> str:
    scenarios = data.get("scenarios") or []
    if not scenarios:
        return ""
    lines = [
        "## 下落シナリオ（レバレッジ倍率を反映）",
        "",
        "> 3xETFは原資産-20%で概ね-60%になる。**損切りしない運用方針では、この数字を",
        "> 平時に見ておくことが政策（案A）の前提になる。**",
        "",
        "| 原資産下落 | PF評価額 | 変化率 |",
        "|:---|---:|---:|",
    ]
    for s in scenarios:
        lines.append(
            f"| {s['underlying_drop_pct']:.0f}% | {_yen(s['after'])} | "
            f"{_pct(s['change_pct'], sign=True)} |"
        )
    lines.append("")
    return "\n".join(lines)


def _per_stock(data: dict) -> str:
    lines = ["## 個別銘柄分析", ""]

    seen: set = set()
    for a in data.get("analyses") or []:
        key = (a.get("symbol"), a.get("name"))
        if key in seen:
            continue
        seen.add(key)
        lines.append(_stock_section(a))
    return "\n".join(lines)


def _stock_section(a: dict) -> str:
    symbol = a.get("symbol") or "—"
    f = a.get("fundamentals") or {}
    t = a.get("technicals")

    lines = [
        f"### {a['name']}（{symbol}）",
        "",
    ]

    if a.get("error"):
        lines.append(f"> ⚠️ データ取得に問題: {a['error']}")
        lines.append("")

    # --- ポジション ---
    lines += [
        "**ポジション**",
        "",
        f"- 数量 {a['shares']:,.0f} / 取得単価 {_num(a['cost_price'])} {a['currency']}",
        f"- 現在値 {_num(a['price'])} {a['currency']}（出所: {a.get('price_source', '—')}）",
        f"- 評価額 {_yen(a['value_jpy'])} / 損益 {_yen(a['pl_jpy'])} "
        f"({_pct(a['pl_pct'], sign=True)})",
        "",
    ]

    # --- 業績・バリュエーション ---
    if f.get("is_etf"):
        lines += [
            "**ETF情報**",
            "",
            f"- 経費率: {_ratio_pct(f.get('expense_ratio'))}",
            f"- 純資産: {_yen(f.get('market_cap')) if f.get('market_cap') else '—'}",
            "- ※ レバレッジETFは日次リバランス型。長期保有ではボラティリティ・ドラッグが効く。",
            "",
        ]
    else:
        lines += [
            "**業績・バリュエーション**",
            "",
            "| 指標 | 値 |",
            "|:---|---:|",
            f"| PER | {_num(f.get('per'))} 倍 |",
            f"| 予想PER | {_num(f.get('forward_per'))} 倍 |",
            f"| PBR | {_num(f.get('pbr'))} 倍 |",
            f"| ROE | {_ratio_pct(f.get('roe'))} |",
            f"| 営業利益率 | {_ratio_pct(f.get('operating_margin'))} |",
            f"| 純利益率 | {_ratio_pct(f.get('profit_margin'))} |",
            f"| 売上成長率 | {_ratio_pct(f.get('revenue_growth'))} |",
            f"| 利益成長率 | {_ratio_pct(f.get('earnings_growth'))} |",
            f"| 配当利回り | {_ratio_pct(f.get('dividend_yield'))} |",
            f"| D/Eレシオ | {_num(f.get('debt_to_equity'))} |",
            "",
        ]

    # --- テクニカル ---
    if t:
        macd = t.get("macd") or {}
        bb = t.get("bollinger") or {}
        rng = t.get("range_52w") or {}
        lines += [
            "**テクニカル**",
            "",
            "| 指標 | 値 |",
            "|:---|---:|",
            f"| 終値 | {_num(t.get('last'))} |",
            f"| 20日移動平均 | {_num(t.get('sma20'))} |",
            f"| 50日移動平均 | {_num(t.get('sma50'))} |",
            f"| 200日移動平均 | {_num(t.get('sma200'))} |",
            f"| 200日線乖離 | {_pct(t.get('sma200_deviation_pct'), sign=True)} |",
            f"| RSI(14) | {_num(t.get('rsi14'), 1)} |",
            f"| MACD | {_num(macd.get('macd'))} |",
            f"| MACDシグナル | {_num(macd.get('signal'))} |",
            f"| MACDヒストグラム | {_num(macd.get('histogram'))} |",
            f"| ボリンジャー上限 | {_num(bb.get('upper'))} |",
            f"| ボリンジャー下限 | {_num(bb.get('lower'))} |",
            f"| %B | {_num(bb.get('percent_b'))} |",
            f"| 52週高値 | {_num(rng.get('high'))} |",
            f"| 52週安値 | {_num(rng.get('low'))} |",
            f"| 高値からの下落 | {_pct(rng.get('from_high_pct'), sign=True)} |",
            f"| 年率ボラティリティ | {_pct(t.get('volatility_pct'))} |",
            "",
        ]
        if t.get("trend"):
            lines.append(f"- トレンド: {t['trend']}")

        heat = t.get("heat") or {}
        lines.append(
            f"- **過熱判定: {_heat_emoji(heat.get('state', 'unknown'))} "
            f"{heat.get('label', '判定不能')}**"
            f"（スコア {heat.get('score', 0):+d} / 使用指標 {heat.get('available', 0)}件）"
        )
        for sig in heat.get("signals") or []:
            lines.append(f"  - {sig}")
        lines.append("")
    else:
        lines += ["**テクニカル**", "", "- 価格履歴が取得できず算出不可。", ""]

    if a.get("note"):
        lines += [f"> {a['note']}", ""]

    return "\n".join(lines)


def _heat_overview(data: dict) -> str:
    analyses = [a for a in data.get("analyses") or [] if a.get("technicals")]
    if not analyses:
        return ""

    lines = [
        "## 売られすぎ / 買われすぎ 一覧",
        "",
        "| 銘柄 | RSI(14) | %B | 52週位置 | 200日乖離 | 判定 |",
        "|:---|---:|---:|---:|---:|:---|",
    ]
    overbought: list[str] = []
    oversold: list[str] = []

    for a in analyses:
        t = a["technicals"]
        heat = t.get("heat") or {}
        bb = t.get("bollinger") or {}
        rng = t.get("range_52w") or {}
        pos = rng.get("position")
        lines.append(
            f"| {a['name']} | {_num(t.get('rsi14'), 1)} | {_num(bb.get('percent_b'))} | "
            f"{_pct(pos * 100 if pos is not None else None, 0)} | "
            f"{_pct(t.get('sma200_deviation_pct'), sign=True)} | "
            f"{_heat_emoji(heat.get('state', 'unknown'))} {heat.get('label', '—')} |"
        )
        if heat.get("state") == "overbought":
            overbought.append(a["name"])
        elif heat.get("state") == "oversold":
            oversold.append(a["name"])

    lines.append("")

    # PF加重の過熱集中度（plan-check の Risk Checker が見る観点）
    total = sum(a["value_jpy"] for a in analyses if a.get("value_jpy"))
    hot_value = sum(
        a["value_jpy"] for a in analyses
        if a.get("value_jpy") and (a["technicals"].get("heat") or {}).get("state") == "overbought"
    )
    if total:
        share = hot_value / total * 100.0
        lines.append(f"- 買われすぎ銘柄がPFに占める比率: **{share:.1f}%**")
        if share >= 30:
            lines.append(
                "  - ⚠️ 30%以上が過熱圏。**この状態での買い増しは慎重に**"
                "（plan-check の過熱リスク基準に抵触）。"
            )
    if overbought:
        lines.append(f"- 🔴 買われすぎ: {', '.join(overbought)}")
    if oversold:
        lines.append(f"- 🔵 売られすぎ: {', '.join(oversold)}")
    lines.append("")
    return "\n".join(lines)


def _footer(data: dict) -> str:
    return "\n".join([
        "---",
        "",
        "## 前提と限界",
        "",
        "- 推移予測は資産クラス別の年率リターン・ボラティリティ前提から導いた**統計的レンジ**であり、",
        "  個別の業績予想や相場観を反映したものではない。",
        "- レバレッジETFのドラッグは一次近似（`L(L-1)σ²/2`）。実際の減衰は経路依存で変わる。",
        "- テクニカル指標は終値のみから算出。出来高・板情報は見ていない。",
        "- 業績指標は Yahoo Finance 由来。決算直後は未反映のことがある。",
        "",
        "関連: [[投資記録]] / [[ポートフォリオ方針]]",
        "",
    ])
