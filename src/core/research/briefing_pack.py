"""ブリーフィングパック生成 — Claude の深掘り synthesis に渡す「全材料」を1つに束ねる層。

## 位置づけ

週次レポートも個別質問（"この株どう？"）も、根本の欠陥は同じだった:
**材料集め（Python・トークン0）はできているのに、解釈・統合（Claude/LLMの仕事）が
無い。** このモジュールは前者を極限まで厚くし、後者に渡す構造化パックを作る。

パックには「数字」だけでなく **前回からの差分・今後の日程・競合の動き・過去の
テーゼ/懸念/lesson** まで含める。Claude はこれを読んで「銘柄ごとに何が変わり、
PF上どういう立ち位置で、今後の日程がどう効き、どう動くべきか」を書ける。

## 2モード

- ``build_portfolio_briefing()``  … 保有全体（週次レポート用）
- ``build_symbol_briefing(symbol)`` … 単一銘柄＋その競合＋セクター指数（個別質問用）

## graceful degradation

各データ源は独立に try/except で守り、失敗したら該当キーを空にして続行する。
Neo4j/moomoo/Finnhub/ネットワークがどれか落ちてもパックは必ず返る。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional

from src.core.portfolio.weekly import (
    build_report_data,
    load_holdings_config,
)

# パックのスキーマ版。executor 側の互換チェック用。
PACK_VERSION = 1


# ---------------------------------------------------------------------------
# 前回差分（data/history/report のスナップショットから）
# ---------------------------------------------------------------------------

#: 前回差分を計算する数値フィールド
_DELTA_FIELDS = (
    "price", "per", "pbr", "roe", "revenue_growth", "value_score",
    "dividend_yield", "market_cap",
)


def _norm_sym(sym: Any) -> str:
    return str(sym or "").strip().upper()


def _prior_report_index(days_back: int = 180) -> dict[str, list[dict]]:
    """history/report をシンボル別・日付降順に索引化する。"""
    try:
        from src.data.history.load import load_history

        snaps = load_history("report", days_back=days_back)
    except Exception:
        return {}
    index: dict[str, list[dict]] = {}
    for s in snaps:
        key = _norm_sym(s.get("symbol"))
        if key:
            index.setdefault(key, []).append(s)
    # load_history は既に新しい順。各リストの先頭が最新。
    return index


def week_over_week_delta(symbol: str, current: dict,
                         prior_index: dict[str, list[dict]],
                         today: Optional[str] = None) -> Optional[dict]:
    """1銘柄の「前回スナップショットからの変化」を返す。無ければ None。

    current は fundamentals + price を含む dict。
    """
    key = _norm_sym(symbol)
    snaps = prior_index.get(key) or []
    today = today or date.today().isoformat()
    prior = next((s for s in snaps if str(s.get("date", "")) < today), None)
    if not prior:
        return None

    delta: dict[str, Any] = {}
    for f in _DELTA_FIELDS:
        now_v = current.get(f)
        old_v = prior.get(f)
        if isinstance(now_v, (int, float)) and isinstance(old_v, (int, float)):
            diff = now_v - old_v
            pct = (diff / old_v * 100.0) if old_v else None
            delta[f] = {"prior": old_v, "now": now_v, "diff": diff, "pct": pct}
    return {
        "prior_date": prior.get("date"),
        "prior_verdict": prior.get("verdict"),
        "now_verdict": current.get("verdict"),
        "fields": delta,
    }


# ---------------------------------------------------------------------------
# 各データ源の安全ラッパ
# ---------------------------------------------------------------------------


def _safe_news_watch(symbols: list[str]) -> dict:
    try:
        from src.core.research.portfolio_news import build_news_watch

        return build_news_watch(symbols=symbols) or {}
    except Exception:
        return {}


def _safe_moomoo(symbols: list[str]) -> dict:
    """moomoo(OpenD) インサイト。opt-in（MOOMOO_ENABLED=on）かつ起動時のみ。"""
    try:
        from src.data import moomoo_client
        from src.core.research import moomoo_insights
    except Exception:
        return {}
    if not symbols:
        return {}
    try:
        with moomoo_client.ensure_opend() as up:
            if not up:
                return {}
            return moomoo_insights.collect_weekly_insights(symbols) or {}
    except Exception:
        return {}


def _safe_context(query: str) -> str:
    """Neo4j/グラフからの過去テーゼ・懸念・lesson・リサーチ履歴（markdown）。"""
    try:
        from src.data.context.auto_context import get_context

        result = get_context(query)
        if result and result.get("context_markdown"):
            return result["context_markdown"]
    except Exception:
        pass
    return ""


def _safe_competitors(symbols: list[str]) -> dict:
    try:
        from src.core.research.competitors import build_peer_context

        return build_peer_context(symbols) or {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# テクニカルの要約（パックを読みやすく保つため主要指標だけ抜く）
# ---------------------------------------------------------------------------


def _compact_technicals(t: Optional[dict]) -> Optional[dict]:
    if not t:
        return None
    heat = t.get("heat") or {}
    bb = t.get("bollinger") or {}
    rng = t.get("range_52w") or {}
    return {
        "last": t.get("last"),
        "sma50": t.get("sma50"),
        "sma200": t.get("sma200"),
        "sma200_deviation_pct": t.get("sma200_deviation_pct"),
        "rsi14": t.get("rsi14"),
        "percent_b": bb.get("percent_b"),
        "range_52w_position": rng.get("position"),
        "from_high_pct": rng.get("from_high_pct"),
        "volatility_pct": t.get("volatility_pct"),
        "trend": t.get("trend"),
        "heat_state": heat.get("state"),
        "heat_label": heat.get("label"),
        "heat_signals": heat.get("signals"),
    }


def _holding_view(a: dict, prior_index: dict, competitors: dict,
                  total_jpy: float, today: str) -> dict:
    f = a.get("fundamentals") or {}
    # 差分計算に渡す current（fundamentals + price + verdict）
    current = dict(f)
    current["price"] = a.get("price")
    wow = week_over_week_delta(a.get("symbol") or "", current, prior_index, today)
    weight = (a["value_jpy"] / total_jpy * 100.0) if (a.get("value_jpy") and total_jpy) else None
    return {
        "name": a.get("name"),
        "symbol": a.get("symbol"),
        "account": a.get("account"),
        "shares": a.get("shares"),
        "cost_price": a.get("cost_price"),
        "price": a.get("price"),
        "price_source": a.get("price_source"),
        "currency": a.get("currency"),
        "value_jpy": a.get("value_jpy"),
        "pl_jpy": a.get("pl_jpy"),
        "pl_pct": a.get("pl_pct"),
        "weight_pct": weight,
        "week_change_pct": (a.get("week") or {}).get("week_change_pct"),
        "leverage": a.get("leverage"),
        "fundamentals": f,
        "technicals": _compact_technicals(a.get("technicals")),
        "wow_delta": wow,
        "competitors": competitors.get(a.get("symbol") or ""),
        "note": a.get("note"),
        "error": a.get("error"),
    }


# ---------------------------------------------------------------------------
# 今後の日程（moomoo の経済/決算/配当カレンダーを1本の時系列に）
# ---------------------------------------------------------------------------


def _forward_schedule(moomoo: dict) -> list[dict]:
    events: list[dict] = []
    for e in moomoo.get("economic_events") or []:
        events.append({
            "kind": "economic", "title": e.get("title"),
            "country": e.get("country"), "importance": e.get("star"),
            "consensus": e.get("consensus"), "previous": e.get("previous"),
        })
    for e in moomoo.get("earnings") or []:
        events.append({
            "kind": "earnings", "symbol": e.get("security"),
            "date": e.get("date"), "title": f"{e.get('security')} 決算",
            "period": e.get("period"), "eps_predict": e.get("eps_predict"),
        })
    for d in moomoo.get("dividends") or []:
        events.append({
            "kind": "dividend", "symbol": d.get("security"),
            "date": d.get("ex_date"), "title": f"{d.get('security')} 配当除権",
            "statement": d.get("statement"),
        })
    fw = moomoo.get("fed_watch")
    if fw:
        events.append({
            "kind": "fomc", "date": fw.get("next_meeting"),
            "title": "FOMC", "top_range": fw.get("top_range"),
            "top_prob": fw.get("top_prob"),
        })
    return events


# ---------------------------------------------------------------------------
# パック本体
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 三点照合 / 物語量スナップショット (土曜設計書 提案1 / 提案7)
# ---------------------------------------------------------------------------


def _safe_reconciliation(config: dict, base: dict, include_moomoo: bool) -> dict:
    """照合はレポートの第1セクション。失敗しても**黙って成功にしない**。"""
    try:
        from src.core.portfolio.reconciliation import run_reconciliation

        sources = None if include_moomoo else ["rakuten_csv"]
        return run_reconciliation(config, report_data=base, sources=sources,
                                  autostart_opend=include_moomoo)
    except Exception as e:
        return {
            "status": "unreconciled", "blocking": True, "reconcilable": False,
            "independently_verified": False,
            "counts": {}, "diffs": [], "ghosts": [], "unrecorded": [],
            "unverified": [], "orphans": [], "sources": [],
            "messages": [f"⛔ 照合の実行自体に失敗しました: {type(e).__name__}: {e}"],
        }


def _safe_narrative(holdings: list[dict], capture: bool) -> dict:
    """物語量スナップショットを記録し、混雑度を添える。

    **記録は遡れない**（設計書 提案7-⑩）ので、分析が未完成でも記録だけは毎週回す。
    ネットワークが死んでいても週次レポート本体は止めない。
    """
    out: dict = {"captured": None, "crowding": {}, "note": None}
    try:
        from src.core.research import narrative as nv
    except Exception:
        return out

    if capture:
        try:
            out["captured"] = nv.capture_many(holdings, occasion="weekly")
        except Exception as e:
            out["captured"] = {"captured": 0, "attempted": len(holdings or []),
                               "error": f"{type(e).__name__}"}

    for h in holdings or []:
        sym, name = h.get("symbol"), h.get("name")
        if not sym and not name:
            continue
        try:
            c = nv.crowding(sym, name)
        except Exception:
            continue
        out["crowding"][sym or f"name:{name}"] = c

    ready = [c for c in out["crowding"].values() if c.get("available")]
    out["note"] = (
        "混雑度は記録開始以降しか測れません。基準が揃うまで分析は保留されます。"
        if not ready else
        "混雑度は『テーゼがまだ少数派か』の指標であり、単独で売り推奨を作りません。"
    )
    return out


# ---------------------------------------------------------------------------
# 前方イベント脊椎 (土曜設計書 提案4)
# ---------------------------------------------------------------------------


def _safe_forward(holdings: list[dict], moomoo: dict, indices: list[dict],
                  prior_calendar: Optional[dict]) -> dict:
    """翌週の確定イベント集合。レポートの骨格そのもの。"""
    try:
        from src.core.risk.forward_events import build_forward_section

        return build_forward_section(holdings, moomoo=moomoo, indices=indices,
                                     prior_calendar=prior_calendar)
    except Exception as e:
        return {"calendar": {"events": [], "folded": [], "unavailable_symbols": []},
                "actionable": [],
                "errors": [f"前方イベントの構築に失敗: {type(e).__name__}: {e}"]}


def _safe_execution_audit(days: int = 90) -> dict:
    """執行監査（提案5）。判断も約定も取れないのが通常なので、静かに縮退する。

    ここが空でも「精度が良い」ではなく**「測れていない」**。
    """
    try:
        from src.core.portfolio.execution_audit import build_execution_audit

        return build_execution_audit(days=days)
    except Exception as e:
        return {"survival": {"available": False,
                             "reason": f"{type(e).__name__}: {e}"},
                "errors": [f"執行監査を実行できません: {type(e).__name__}"]}


def _safe_model_audit(holdings: list[dict], pf_summary: dict,
                      indices: list[dict], store: bool) -> dict:
    """模型監査（提案10）。**記録は今週から始める。**

    26週の蓄積が前提だが、記録を始めなければ26週後も測れない。
    分析は「データ蓄積中」でよいので、(予測, 実現) のペアだけは毎週積む。
    """
    try:
        from src.core.risk.model_audit import build_model_audit
    except Exception as e:
        return {"score": {"available": False, "reason": f"{type(e).__name__}"},
                "errors": [f"模型監査を読み込めません: {type(e).__name__}"]}

    pf_betas = _safe_pf_betas(holdings)
    factor_moves = _factor_moves(indices)
    realized = _realized_week_pct(holdings)

    try:
        return build_model_audit(
            pf_betas, factor_moves, realized,
            context={"has_leverage": any(h.get("leverage") for h in holdings or [])},
            store=store)
    except Exception as e:
        return {"score": {"available": False, "reason": f"{type(e).__name__}"},
                "errors": [f"模型監査に失敗: {type(e).__name__}: {e}"]}


def _safe_pf_betas(holdings: list[dict]) -> dict:
    """PF の因子エクスポージャー。取れなければ空（予測を作らない）。"""
    try:
        from src.core.exposure import estimate_many, portfolio_exposure

        symbols = sorted({h.get("symbol") for h in holdings or [] if h.get("symbol")})
        if not symbols:
            return {}
        pf = portfolio_exposure(holdings, estimate_many(symbols))
        return pf.get("betas") or {} if pf.get("available") else {}
    except Exception:
        return {}


def _factor_moves(indices: list[dict]) -> dict:
    """因子の**週次**変化率。

    指数ウォッチの `percent_change` は日次なので流用してはいけない。
    実現リターンが週次である以上、因子も週次で揃えないと単位が食い違う。
    """
    try:
        from src.core.exposure import weekly_factor_moves

        return weekly_factor_moves()
    except Exception:
        return {}


def _realized_week_pct(holdings: list[dict]) -> Optional[float]:
    """今週のPF実現リターン（評価額加重）。取れなければ None。"""
    total_weight = 0.0
    weighted = 0.0
    for h in holdings or []:
        w = h.get("weight_pct")
        chg = h.get("week_change_pct")
        if isinstance(w, (int, float)) and isinstance(chg, (int, float)):
            total_weight += float(w)
            weighted += float(w) * float(chg)
    return round(weighted / total_weight, 3) if total_weight else None


def _prior_calendar() -> Optional[dict]:
    """前週スナップショットに保存された翌週カレンダー。

    日程変更（決算日の前倒し/後ろ倒し）は前週と比べないと検出できない。
    """
    try:
        from src.core.portfolio.report_diff import load_snapshots, prior_snapshot

        prev = prior_snapshot(load_snapshots(), weeks_back=1)
        return (prev or {}).get("forward_calendar")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 制約: 税・現金・入金・注意 (土曜設計書 提案3 / 提案9)
# ---------------------------------------------------------------------------


def _safe_constraints(config: dict, base: dict, holdings: list[dict],
                      reconciliation: Optional[dict]) -> dict:
    """行動可能な空間を先に確定させる材料（第4セクション）。

    設計書 第3章の順序: 制約 → 機会。逆にすると実行できない推奨を先に読ませる。
    """
    out: dict = {"tax_state": None, "runway_bundle": None, "attention": None,
                 "loss_harvest": [], "liquidity": None, "errors": []}

    total_jpy = base.get("total_jpy")
    cash_jpy = base.get("cash_jpy")
    orphans = len((reconciliation or {}).get("orphans") or [])

    try:
        from src.core.portfolio.tax import build_tax_state, loss_harvest_value

        tax_state = build_tax_state(config.get("holdings") or [])
        out["tax_state"] = tax_state
        realized = tax_state.get("realized_gain_ytd_jpy")
        for h in holdings or []:
            pl = h.get("pl_jpy")
            if isinstance(pl, (int, float)) and pl < 0:
                lh = loss_harvest_value(pl, realized, h.get("account"))
                lh["symbol"] = h.get("symbol")
                lh["label"] = h.get("name")
                out["loss_harvest"].append(lh)
    except Exception as e:
        out["errors"].append(f"税務状態: {type(e).__name__}: {e}")

    # 流動性は「行動可能な空間」の一部（設計書 第3章の第4セクション）。
    # 売れない銘柄の売却を計画に入れないため、制約側に置く。
    try:
        from src.core.risk.liquidity import build_liquidity_section

        out["liquidity"] = build_liquidity_section(
            holdings, cash_jpy=cash_jpy, total_jpy=total_jpy)
    except Exception as e:
        out["errors"].append(f"流動性: {type(e).__name__}: {e}")

    try:
        from src.core.portfolio.runway import (
            attention_budget,
            cash_purpose_check,
            load_cashflow_config,
            runway,
            weekly_investable,
        )

        cfg = load_cashflow_config()
        est = weekly_investable(None, cfg)
        out["runway_bundle"] = {
            "estimate": est,
            "runway": runway(est.get("weekly_jpy"), cash_jpy=cash_jpy, cfg=cfg),
            "cash": cash_purpose_check(cash_jpy, total_jpy, cfg),
        }
        # 孤児は「レビューの基準が無い＝実質監視されていない」として注意予算に効く
        out["attention"] = attention_budget(len(holdings or []), orphans, cfg)
    except Exception as e:
        out["errors"].append(f"資金ランウェイ: {type(e).__name__}: {e}")

    return out


# ---------------------------------------------------------------------------
# 差分レポート / 信念の点検 (土曜設計書 提案8)
# ---------------------------------------------------------------------------


def _safe_falsification(holdings: list[dict]) -> dict:
    """反証条件の点検。**価格ではなく信念の変化**を最初に見るための材料。"""
    try:
        from src.core.portfolio.falsification import check_all

        return check_all(holdings)
    except Exception as e:
        return {"falsified": [], "near": [], "unchecked": [], "missing": [],
                "intact": 0, "checked": 0,
                "error": f"{type(e).__name__}: {e}"}


def _safe_diff(pack_like: dict, store: bool) -> dict:
    """前週スナップショットとの差分＋累積差分。今週分の保存もここで行う。

    保存を分析より先に済ませると、途中で落ちても**来週の比較対象は残る**。
    """
    out: dict = {"diff": {"available": False, "reason": "差分エンジンが使えません"},
                 "cumulative": {}, "snapshot_saved": None}
    try:
        from src.core.portfolio import report_diff as rd
    except Exception:
        return out

    try:
        snapshot = rd.build_snapshot(pack_like)
        history = rd.load_snapshots()
        prev = rd.prior_snapshot(history, weeks_back=1,
                                 today=str(snapshot.get("date") or ""))
        out["diff"] = rd.diff_snapshots(snapshot, prev)
        out["cumulative"] = rd.cumulative_diff(snapshot, history)
        if store:
            path = rd.save_snapshot(snapshot)
            out["snapshot_saved"] = str(path) if path else None
        out["history_weeks"] = len(history)
    except Exception as e:
        out["diff"] = {"available": False,
                       "reason": f"差分計算に失敗: {type(e).__name__}: {e}"}
    return out


def _portfolio_summary(base: dict) -> dict:
    analyses = base.get("analyses") or []
    total_pl = sum(a["pl_jpy"] for a in analyses if a.get("pl_jpy") is not None)
    total_cost = sum(a["cost_jpy"] for a in analyses if a.get("cost_jpy") is not None)
    return {
        "total_jpy": base.get("total_jpy"),
        "invested_jpy": base.get("invested_jpy"),
        "cash_jpy": base.get("cash_jpy"),
        "fx_rate": base.get("fx_rate"),
        "total_pl_jpy": total_pl,
        "pl_pct": (total_pl / total_cost * 100.0) if total_cost else None,
    }


def build_portfolio_briefing(
    config: Optional[dict] = None,
    rss_snapshot: Optional[dict] = None,
    monthly_contribution: float = 50000.0,
    include_moomoo: bool = True,
    include_context: bool = True,
    include_reconciliation: bool = True,
    capture_narrative: bool = True,
    store_snapshot: bool = True,
) -> dict:
    """保有全体のブリーフィングパックを組み立てる（週次レポート用）。"""
    if config is None:
        config = load_holdings_config()

    base = build_report_data(
        config, rss_snapshot=rss_snapshot, monthly_contribution=monthly_contribution
    )
    analyses = base.get("analyses") or []
    symbols = [a.get("symbol") for a in analyses if a.get("symbol")]
    total_jpy = base.get("total_jpy") or 0.0
    today = date.today().isoformat()

    prior_index = _prior_report_index()
    competitors = _safe_competitors(symbols)
    news = _safe_news_watch(symbols)
    moomoo = _safe_moomoo(symbols) if include_moomoo else {}
    prior_context = _safe_context("週次ポートフォリオ分析 PF全体の状況") if include_context else ""

    holdings = [
        _holding_view(a, prior_index, competitors, total_jpy, today)
        for a in analyses
    ]

    # 照合は分析より先。ここが通らないと下流の数値は全部条件付きになる。
    reconciliation = (_safe_reconciliation(config, base, include_moomoo)
                      if include_reconciliation else None)
    narrative = _safe_narrative(holdings, capture_narrative)

    warnings: list[str] = []
    if not news.get("available"):
        warnings.append("ニュース/指数が取得できませんでした（材料なしではなく取得不可）。")
    if not moomoo:
        warnings.append("moomoo インサイトは無効/未起動（決算・配当・アナリスト日程は限定的）。")
    if reconciliation and reconciliation.get("blocking"):
        warnings.append(
            "三点照合が通っていません。以降の全数値は**未照合**として扱ってください"
            "（幽霊ポジションがあると、存在しない資産のリスクを計算している状態になります）。")
    if reconciliation and not reconciliation.get("independently_verified", True):
        warnings.append(
            "残高の独立検証ができていません（模型の生成元と同じデータを見ています）。")

    forward = _safe_forward(holdings, moomoo, news.get("index_watch") or [],
                            _prior_calendar())
    constraints = _safe_constraints(config, base, holdings, reconciliation)

    # 信念の点検と前週差分。差分は「今週のパック」の形に依存するので、
    # holdings / portfolio が確定した後に計算する。
    falsification = _safe_falsification(holdings)
    pack_like = {
        "meta": {"as_of": today},
        "portfolio": _portfolio_summary(base),
        "holdings": holdings,
        "reconciliation": reconciliation,
        # 来週の日程変更検出に使うので、カレンダーもスナップショットに残す
        "forward_calendar": forward.get("calendar"),
    }
    diff_bundle = _safe_diff(pack_like, store_snapshot)

    assessment = None
    try:
        from src.core.portfolio.report_diff import assess_information

        assessment = assess_information(
            diff_bundle["diff"], diff_bundle.get("cumulative"),
            falsified=falsification.get("falsified"),
            falsification=falsification,
            reconciliation=reconciliation,
            forward=forward,
        )
    except Exception:
        assessment = None

    if falsification.get("falsified"):
        warnings.append(
            f"反証条件が成立した保有が {len(falsification['falsified'])}件あります。"
            "価格ではなく信念が変わった週です。")

    return {
        "pack_version": PACK_VERSION,
        "mode": "portfolio",
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "as_of": today,
            "holdings_source": base.get("holdings_source"),
            "holdings_import": base.get("holdings_import"),
            "fx_rate": base.get("fx_rate"),
            "warnings": warnings,
        },
        "reconciliation": reconciliation,
        "falsification": falsification,
        "forward": forward,
        "constraints": constraints,
        "execution_audit": _safe_execution_audit(),
        "model_audit": _safe_model_audit(
            holdings, _portfolio_summary(base), news.get("index_watch") or [],
            store_snapshot),
        "week_diff": diff_bundle.get("diff"),
        "cumulative_diff": diff_bundle.get("cumulative"),
        "information": assessment,
        "narrative": narrative,
        "portfolio": _portfolio_summary(base),
        "holdings": holdings,
        "indices": news.get("index_watch") or [],
        "holding_news": news.get("holding_news") or {},
        "market_news": news.get("market_news") or [],
        "moomoo": moomoo,
        "forward_schedule": _forward_schedule(moomoo),
        "projection": base.get("projection"),
        "scenarios": base.get("scenarios"),
        "positions_assumptions": base.get("positions"),
        "monthly_contribution": monthly_contribution,
        "prior_context": prior_context,
    }


def build_symbol_briefing(
    symbol: str,
    include_moomoo: bool = True,
    include_context: bool = True,
    capture_narrative: bool = True,
) -> dict:
    """単一銘柄＋競合＋指数のブリーフィングパック（個別質問「常に全力」用）。

    保有していれば保有情報も添える。していなくても分析できる。
    """
    from src.data import yahoo_client as yc

    symbol = symbol.strip()
    today = date.today().isoformat()

    info = None
    detail = None
    technicals = None
    try:
        info = yc.get_stock_info(symbol)
    except Exception:
        info = None
    try:
        detail = yc.get_stock_detail(symbol)
    except Exception:
        detail = None
    try:
        hist = yc.get_price_history(symbol, period="2y")
        from src.core.technicals import analyze_prices

        closes = [float(x) for x in hist["Close"].dropna().tolist()] if hist is not None else []
        technicals = analyze_prices(closes) if closes else None
    except Exception:
        technicals = None

    prior_index = _prior_report_index()
    current = dict(detail or info or {})
    current["price"] = (info or {}).get("price")
    wow = week_over_week_delta(symbol, current, prior_index, today)

    competitors = _safe_competitors([symbol]).get(symbol)
    news = _safe_news_watch([symbol])
    moomoo = _safe_moomoo([symbol]) if include_moomoo else {}
    prior_context = _safe_context(f"{symbol} の分析") if include_context else ""

    # 物語量: 個別質問のたびに1点記録しておく。後から遡れないため。
    narrative = _safe_narrative(
        [{"symbol": symbol, "name": (detail or info or {}).get("name")}],
        capture_narrative)

    # 保有チェック
    held = None
    try:
        from src.core.research.portfolio_news import get_portfolio_symbols

        if _norm_sym(symbol) in {_norm_sym(s) for s in get_portfolio_symbols()}:
            held = True
    except Exception:
        held = None

    return {
        "pack_version": PACK_VERSION,
        "mode": "symbol",
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "as_of": today,
            "symbol": symbol,
            "is_held": held,
        },
        "info": info,
        "fundamentals": {k: (detail or {}).get(k) for k in (
            "per", "forward_per", "pbr", "roe", "roa", "profit_margin",
            "operating_margin", "revenue_growth", "earnings_growth",
            "dividend_yield", "debt_to_equity", "market_cap", "beta",
            "sector", "industry", "name",
        )} if detail else (info or {}),
        "technicals": _compact_technicals(technicals),
        "wow_delta": wow,
        "narrative": narrative,
        "competitors": competitors,
        "indices": news.get("index_watch") or [],
        "news": (news.get("holding_news") or {}).get(symbol) or [],
        "market_news": news.get("market_news") or [],
        "moomoo": moomoo,
        "forward_schedule": _forward_schedule(moomoo),
        "prior_context": prior_context,
    }
