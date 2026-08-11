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

import time
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src.core.portfolio.weekly import (
    build_report_data,
    load_holdings_config,
)

# パックのスキーマ版。executor 側の互換チェック用。
PACK_VERSION = 1


#: 各工程の所要秒数。無人実行でどこが遅いかを後から追えるようにする。
#: 土曜7:12の無人実行がタイムアウトしたとき、原因を推測しないで済む。
_TIMINGS: dict[str, float] = {}


@contextmanager
def _timed(label: str):
    start = time.monotonic()
    try:
        yield
    finally:
        _TIMINGS[label] = round(time.monotonic() - start, 1)


def _reset_timings() -> None:
    _TIMINGS.clear()


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


def _proxy_technicals(symbol: Optional[str], name: Optional[str]) -> Optional[dict]:
    """ティッカーを持たない投信のテクニカルを、連動対象指数から代理計算する。

    これが無いと投信は「テクニカル取得できず＝判定不能」になり、
    2026-08-01 の週次では PF の 6.9%（FANG+）が過熱判定の対象外だった。
    **指数由来であることを必ず値に持たせる**（基準価額そのものではない）。
    """
    try:
        from src.core.risk.etf_lookthrough import resolve_technical_proxy

        px = resolve_technical_proxy(symbol, name)
        if not px:
            return None
        from src.core.technicals import analyze_prices
        from src.data import yahoo_client as yc

        # 指数 → 予備の順に試す。指数が引けなかった週に「判定不能」で
        # 終わらせないため（§16-8 単一の取得元に依存しない）。
        used = None
        closes: list[float] = []
        for candidate in (px["proxy"], px.get("fallback")):
            if not candidate:
                continue
            hist = yc.get_price_history(candidate, period="2y")
            if hist is not None and not hist.empty:
                closes = [float(x) for x in hist["Close"].dropna().tolist()]
                if closes:
                    used = candidate
                    break
        if not closes:
            return None
        t = _compact_technicals(analyze_prices(closes))
        if t:
            t["proxy_symbol"] = used
            t["is_proxy"] = True
            t["proxy_note"] = px.get("note")
            if used != px["proxy"]:
                t["proxy_fallback_used"] = True
                t["proxy_note"] = (
                    f"{px.get('note') or ''} ⚠️ 本来の代理（{px['proxy']}）が"
                    f"取得できず予備（{used}）を使用。")
        return t
    except Exception:
        return None


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
        "technicals": (_compact_technicals(a.get("technicals"))
                       or _proxy_technicals(a.get("symbol"), a.get("name"))),
        "wow_delta": wow,
        "competitors": competitors.get(a.get("symbol") or ""),
        "note": a.get("note"),
        "error": a.get("error"),
    }


# ---------------------------------------------------------------------------
# 今後の日程（moomoo の経済/決算/配当カレンダーを1本の時系列に）
# ---------------------------------------------------------------------------


def _forward_schedule(moomoo: dict, forward: Optional[dict] = None) -> list[dict]:
    """今後の日程を1本の時系列にする。

    以前はこの関数が **moomoo だけ**を材料にしていた。moomoo は opt-in で
    既定では無効なので、実運用では常に空リストが返り、各銘柄の節は
    「日程が取得できなかった」と書き続けていた。**取得していなかったのではなく、
    取得済みの yfinance 由来の日程をこの関数が見ていなかった。**

    そのため yfinance 由来のカレンダー（`forward.calendar`）を第一の材料とし、
    moomoo は経済指標・FOMC 等の**補完**として重ねる。
    """
    events: list[dict] = []

    # yfinance 由来（決算・配当）。moomoo の有無に関係なく常に入る。
    cal = (forward or {}).get("calendar") or {}
    for e in (cal.get("events") or []) + (cal.get("folded") or []):
        events.append(dict(e))

    # ETF/投信は自身に決算が無いので、中身の企業の決算を同じ時系列に載せる。
    for e in ((forward or {}).get("lookthrough_events") or {}).get("events") or []:
        events.append({
            "kind": "component_earnings", "date": e.get("date"),
            "day_label": e.get("day_label"), "symbol": e.get("symbol"),
            "title": f"{e.get('symbol')} 決算（ETF構成銘柄）",
            "effective_pct": e.get("effective_pct"), "via": e.get("via"),
            "source": "etf_lookthrough",
        })

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
                 "loss_harvest": [], "liquidity": None, "assumption_space": None,
                 "errors": []}

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

    # 前提空間（改善4）— 資産空間の HHI では見えない2種類の穴を見る:
    #   集中 … 複数銘柄が同一前提に依存している
    #   衝突 … 同じ変数の同じ方向に「好機」と「危機」が同居し、
    #          **計画の実行条件が揃った瞬間に原資が毀損する**
    # 制約側に置くのは、これが行動可能な空間の話だから（機会より先に読ませる）。
    try:
        from src.core.risk.assumptions import analyze_assumption_space

        out["assumption_space"] = analyze_assumption_space(
            holdings=[{"symbol": h.get("symbol"), "value": h.get("value_jpy")}
                      for h in holdings or []])
    except Exception as e:
        out["errors"].append(f"前提空間: {type(e).__name__}: {e}")

    return out


# ---------------------------------------------------------------------------
# 差分レポート / 信念の点検 (土曜設計書 提案8)
# ---------------------------------------------------------------------------


def _safe_external_views(holdings: list[dict]) -> dict:
    """外部批評家の直近見解（改善5）。台帳を読むだけで API は叩かない。

    取れなかった場合に「誰も何も言っていない」と読ませないよう、
    `available` と `note` で状態を明示する。
    """
    try:
        from src.core.critic_calibration import build_external_views

        symbols = [h.get("symbol") for h in holdings or [] if h.get("symbol")]
        return build_external_views(days=7, symbols=symbols)
    except Exception as e:
        return {"available": False, "views": [], "by_symbol": {}, "macro_views": [],
                "usable_count": 0, "sources": [],
                "note": f"外部言説を読めませんでした（{type(e).__name__}）。"
                        "『発言が無かった』ではありません。"}


#: 価格がこの比率を下回ったら、レポートを書かせずに再実行させる。
#: 2026-08-08 は 10件中 9件が null（カバレッジ 10%）で、それでもレポートが
#: 生成・保存された。**分析ではなく「取得できず」の一覧が届いた。**
MIN_PRICE_COVERAGE = 0.7


def _data_quality(holdings: list[dict], network: Optional[dict] = None) -> dict:
    """このパックでレポートを書いてよいかを判定する。

    価格が取れていないパックからは、どれだけ丁寧に書いても
    「取得できず」の一覧しか出てこない。**書く前に止める。**

    Returns
    -------
    dict
        {"price_coverage", "priced", "total", "missing", "usable", "verdict"}
        `usable=False` のとき、呼び出し側は保存せず**再実行**すること。
    """
    rows = [h for h in holdings or [] if h.get("symbol") or h.get("name")]
    total = len(rows)
    priced = [h for h in rows if isinstance(h.get("price"), (int, float))]
    missing = [str(h.get("symbol") or h.get("name")) for h in rows
               if not isinstance(h.get("price"), (int, float))]
    coverage = (len(priced) / total) if total else 0.0

    # 取得失敗の理由を拾えるだけ拾う（「データが無い」との区別のため）
    reasons: dict[str, str] = {}
    try:
        from src.data.yahoo_client.detail import last_fetch_error

        for name in missing:
            reason = last_fetch_error(name)
            if reason:
                reasons[name] = reason
    except Exception:
        pass

    usable = coverage >= MIN_PRICE_COVERAGE
    if usable:
        verdict = f"価格カバレッジ {coverage:.0%}（{len(priced)}/{total}）。レポート生成可。"
    else:
        verdict = (
            f"🔴 価格カバレッジ {coverage:.0%}（{len(priced)}/{total}）。"
            f"**レポートを書ける状態ではありません。** 取得できなかった: {', '.join(missing)}。"
            "これは『値動きが無かった』ではなく『取りに行けなかった』です。"
        )
        if network and network.get("ready") is False:
            verdict += f" ネットワーク: {network.get('message')}"

    return {
        "price_coverage": round(coverage, 3),
        "priced": len(priced),
        "total": total,
        "missing": missing,
        "reasons": reasons,
        "usable": usable,
        "min_required": MIN_PRICE_COVERAGE,
        "verdict": verdict,
    }


def _safe_watch_plan(holdings: list[dict], forward: Optional[dict]) -> dict:
    """今の保有から、見るべきもの（指数・競合・構成銘柄）を導出する。

    **保有が変われば監視対象も自動で変わる。**
    また、直接保有と「ETF経由のみの曝露」を明示的に分けるので、
    INTC のような非保有銘柄が保有と同じ見た目で並ばない。
    """
    try:
        from src.core.research.watch_plan import build_watch_plan

        return build_watch_plan(holdings, (forward or {}).get("lookthrough"))
    except Exception as e:
        return {"note": f"監視計画を導出できませんでした（{type(e).__name__}: {e}）。",
                "holdings": {"direct": [], "etf_only": []},
                "indices": [], "peers": {"plan": {}, "stale_config": []},
                "constituents": []}


def _safe_composition_check() -> dict:
    """投信の想定構成が、連動対象指数に追随しているかを実測する。

    構成は運用会社の月次レポートにしか無く機械では確認できないが、
    **「確認できない」と「検証できない」は違う。**
    等ウェイトのバスケットが指数とどれだけ一致するかは測れる。
    """
    try:
        from src.core.risk.composition_check import verify_configured_funds

        return verify_configured_funds()
    except Exception as e:
        return {"_error": f"構成を検証できませんでした（{type(e).__name__}: {e}）。"
                          "**『検証済み』ではありません。**"}


def _safe_regime(base: dict, sleeve: Optional[dict],
                 constituents: Optional[dict]) -> dict:
    """市況レジーム（F&G・VIX・長期金利 × PFの状態）。

    統計レンジ「1ヶ月で ▲22%〜+30%」だけでは判断に使えない。
    いま上下どちらに傾いているかは、既にある材料から言える。
    """
    try:
        from src.core.market_dashboard import (
            compute_fear_greed,
            get_vix_history,
            get_yield_curve,
        )
        from src.core.risk.regime import assess_regime

        fg = vix = ust30y = None
        try:
            fg = (compute_fear_greed() or {}).get("score")
        except Exception:
            pass
        try:
            vix = (get_vix_history() or {}).get("current")
        except Exception:
            pass
        try:
            ust30y = ((get_yield_curve() or {}).get("yields") or {}).get("30Y")
        except Exception:
            pass

        total = base.get("total_jpy") or 0.0
        cash = base.get("cash_jpy") or 0.0
        return assess_regime(
            fear_greed=fg, vix=vix, ust30y=ust30y,
            effective_leverage=(sleeve or {}).get("effective_leverage"),
            cash_ratio=(cash / total) if total else None,
            constituent_signals=(constituents or {}).get("signals"),
        )
    except Exception as e:
        return {"axes": [], "available_axes": 0, "total_axes": 0,
                "tilt": "判定不能", "tilt_note": "", "cautions": [],
                "note": f"レジームを判定できませんでした（{type(e).__name__}: {e}）。"
                        "**『中立』ではありません。**"}


def _safe_leverage_sleeve(holdings: list[dict], forward: Optional[dict],
                          total_jpy: Optional[float]) -> dict:
    """3xスリーブの実体（重複・ドラッグ・単一銘柄感応度）。

    保有比率で並べると3つの独立資産に見えるが、同じ銘柄を持っている。
    「3本＝分散」ではない。
    """
    try:
        from src.core.risk.leverage_sleeve import analyze_sleeve

        return analyze_sleeve(holdings, (forward or {}).get("lookthrough"), total_jpy)
    except Exception as e:
        return {"available": False, "etfs": [], "overlap": {}, "sensitivity": [],
                "note": f"レバレッジ・スリーブを分析できませんでした"
                        f"（{type(e).__name__}: {e}）。"}


def _safe_constituent_intel(forward: Optional[dict],
                            holdings: list[dict]) -> dict:
    """ETF構成銘柄の判断材料（価格・過熱・決算日・ニュース・形）。

    比率の一覧だけでは「だから何を見るべきか」に答えられない。
    """
    try:
        from src.core.research.constituent_intel import build_constituent_intel

        return build_constituent_intel(
            (forward or {}).get("lookthrough"), holdings)
    except Exception as e:
        return {"available": False, "dossiers": [], "covered_pct": 0.0,
                "signals": {}, "missing_news": [],
                "note": f"構成銘柄を分析できませんでした（{type(e).__name__}: {e}）。"
                        "**『中身が無い』ではありません。**"}


def _safe_forward_horizon(holdings: list[dict], forward: Optional[dict]) -> dict:
    """数ヶ月先の決算・配当カレンダー（実効エクスポージャー付き）。

    既存の前方イベントは**翌1週間しか見ていない**。それだけを見ていると、
    3週間後に PF の2割が決算を通過することに気づけない。
    """
    try:
        from src.core.risk.forward_horizon import build_forward_horizon

        return build_forward_horizon(
            holdings, (forward or {}).get("lookthrough"))
    except Exception as e:
        return {"available": False, "events": [], "unavailable": [],
                "no_earnings": [], "by_month": {},
                "note": f"前方カレンダーを作れませんでした（{type(e).__name__}: {e}）。"
                        "**『予定なし』ではありません。**"}


#: 一次観測を取りに行く構成銘柄の実効エクスポージャー下限。
#: **ETF経由の曝露が大きい銘柄の開示は、直接保有と同じくらい重要。**
#: NVDA は直接保有ゼロだが実効20%で、その 8-K は PF の2割に効く。
PRIMARY_CONSTITUENT_MIN_PCT = 5.0


def _safe_primary_filings(holdings: list[dict],
                          forward: Optional[dict] = None) -> dict:
    """開示原文（SEC EDGAR / EDINET）。**一次観測の唯一の供給源。**

    これが空の週は、レポートの全ての解釈が外部言説（深度1）と自己推論の上に
    立っていることになる。取得できなかったことを黙って落とさない。

    **直接保有だけでなく、ETF経由で曝露の大きい構成銘柄も対象にする。**
    NVDA は直接保有ゼロだが実効20%（PFの5分の1）で、その開示は
    どの直接保有よりも PF に効く。保有リストだけ見ていると取りこぼす。
    """
    try:
        from src.core.primary_source import build_primary_section, source_status

        symbols = [h.get("symbol") for h in holdings or [] if h.get("symbol")]
        via_etf = [
            r["symbol"] for r in ((forward or {}).get("lookthrough") or {}).get(
                "effective") or []
            if (r.get("effective_pct") or 0) >= PRIMARY_CONSTITUENT_MIN_PCT
            and r.get("symbol") and r["symbol"] not in symbols
        ]
        section = build_primary_section(symbols + via_etf, days=45,
                                        limit_per_symbol=3)
        section["source_status"] = source_status()
        section["constituent_symbols"] = via_etf
        return section
    except Exception as e:
        return {"available": False, "by_symbol": {}, "claims": [],
                "primary_count": 0, "unavailable_symbols": [],
                "note": f"一次開示を取得できませんでした（{type(e).__name__}）。"
                        "**『開示が無かった』ではありません。**"}


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

    _reset_timings()

    # ⚠️ 取得を始める前にネットワークの復帰を待つ。
    #
    # 2026-08-08、PC がスタンバイから起こされた直後（WakeToRun）に実行が始まり、
    # Wi-Fi の再接続が終わる前に価格取得が走った。最初の17秒が全滅し、
    # リトライが無かったため**その一瞬の失敗が「今週の価格は取得不能」として確定**した。
    # 135秒後の narrative は成功しており、通信は後から復旧していた。
    #
    # 「スタンバイのまま無人で動く」ことを前提にしている以上、
    # 起床直後にネットワークが無い状態は**想定すべき通常状態**である。
    with _timed("network_wait"):
        try:
            from src.data.yahoo_client._net import wait_for_network

            network = wait_for_network()
        except Exception as e:
            network = {"ready": None, "message": f"疎通確認に失敗: {type(e).__name__}: {e}"}

    with _timed("prices_and_holdings"):
        base = build_report_data(
            config, rss_snapshot=rss_snapshot,
            monthly_contribution=monthly_contribution
        )
    analyses = base.get("analyses") or []
    symbols = [a.get("symbol") for a in analyses if a.get("symbol")]
    total_jpy = base.get("total_jpy") or 0.0
    today = date.today().isoformat()

    with _timed("prior_reports"):
        prior_index = _prior_report_index()
    with _timed("competitors"):
        competitors = _safe_competitors(symbols)
    with _timed("news"):
        news = _safe_news_watch(symbols)
    with _timed("moomoo"):
        moomoo = _safe_moomoo(symbols) if include_moomoo else {}
    with _timed("graph_context"):
        prior_context = (_safe_context("週次ポートフォリオ分析 PF全体の状況")
                         if include_context else "")

    holdings = [
        _holding_view(a, prior_index, competitors, total_jpy, today)
        for a in analyses
    ]

    # 照合は分析より先。ここが通らないと下流の数値は全部条件付きになる。
    with _timed("reconciliation"):
        reconciliation = (_safe_reconciliation(config, base, include_moomoo)
                          if include_reconciliation else None)
    with _timed("narrative"):
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

    with _timed("forward_events"):
        forward = _safe_forward(holdings, moomoo, news.get("index_watch") or [],
                                _prior_calendar())
    with _timed("constraints"):
        constraints = _safe_constraints(config, base, holdings, reconciliation)

    # 外部言説（改善5）。**API は叩かず台帳を読むだけ**なのでコストは0。
    # 重みが未測定・不足のものは本文の根拠に使えない印が付いて渡る。
    with _timed("external_views"):
        external_views = _safe_external_views(holdings)

    # 一次観測（開示原文）。**系譜台帳で唯一 深度0 の錨になる材料。**
    # これが空の週の解釈は、全て外部言説と自己推論の上に立っている。
    with _timed("primary_filings"):
        primary_filings = _safe_primary_filings(holdings, forward)

    # 前方カレンダー（数ヶ月先）。
    # 「翌週ゼロ」と「3ヶ月ゼロ」はまったく違う。翌週だけを見ていると、
    # 3週間後に PF の2割（NVDA 実効20%）が決算を通過することに気づけない。
    with _timed("forward_horizon"):
        horizon = _safe_forward_horizon(holdings, forward)

    # 構成銘柄インテリジェンス。
    # 「NVDA が実効20%」だけでは判断に使えない。価格・過熱・決算日・ニュースを
    # 束ねて、**単独指標では見えない形**（週次プラス×月次マイナス＝戻り 等）を出す。
    with _timed("constituent_intel"):
        constituents = _safe_constituent_intel(forward, holdings)

    # レバレッジ・スリーブの実体。
    # SOXL/TECL/TQQQ は「3つのポジション」ではなく**重み違いの同一ポジション**。
    # 重複・ボラドラッグ・単一銘柄感応度を金額で出す。
    with _timed("leverage_sleeve"):
        sleeve = _safe_leverage_sleeve(holdings, forward, base.get("total_jpy"))

    # 市況レジーム。「±22%」という統計レンジを判断に使える形にする。
    # **状態の記述であって予測ではない。確率も出さない。**
    with _timed("regime"):
        regime = _safe_regime(base, sleeve, constituents)

    # 構成の自己検証。「未確認」を「精度が測定済み」に変える。
    with _timed("composition_check"):
        composition = _safe_composition_check()

    # 監視計画を**今の保有から毎回導出する**。
    # 手書き設定に固定していると、売却済み銘柄（2737.T）の競合を追い続け、
    # 新しく買った銘柄の競合は追わない、という状態が静かに続く。
    with _timed("watch_plan"):
        watch = _safe_watch_plan(holdings, forward)

    # 信念の点検と前週差分。差分は「今週のパック」の形に依存するので、
    # holdings / portfolio が確定した後に計算する。
    with _timed("falsification"):
        falsification = _safe_falsification(holdings)
    pack_like = {
        "meta": {"as_of": today},
        "portfolio": _portfolio_summary(base),
        "holdings": holdings,
        "reconciliation": reconciliation,
        # 来週の日程変更検出に使うので、カレンダーもスナップショットに残す
        "forward_calendar": forward.get("calendar"),
    }
    with _timed("week_diff"):
        diff_bundle = _safe_diff(pack_like, store_snapshot)
    with _timed("execution_audit"):
        execution_audit = _safe_execution_audit()
    with _timed("model_audit"):
        model_audit = _safe_model_audit(
            holdings, _portfolio_summary(base), news.get("index_watch") or [],
            store_snapshot)

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
            # 無人実行がタイムアウトしたとき、どこが遅かったかを後から追える
            "timings_sec": dict(_TIMINGS),
            "total_sec": round(sum(_TIMINGS.values()), 1),
            # ネットワークの状態と、価格がどれだけ取れたか。
            # **これが悪いパックからレポートを書かせない**ための判定材料。
            "network": network,
            "data_quality": _data_quality(holdings, network),
        },
        "reconciliation": reconciliation,
        "falsification": falsification,
        "forward": forward,
        "constraints": constraints,
        # 外部批評家の直近見解（改善5）。各見解に citation が付いており、
        # 重み未測定・不足のものは本文の根拠に使ってはならない。
        "external_views": external_views,
        # 開示原文（SEC EDGAR / EDINET）。系譜台帳で唯一 深度0 の錨になる。
        "primary_filings": primary_filings,
        # 数ヶ月先の決算・配当（保有＋ETF構成銘柄・実効エクスポージャー付き）
        "forward_horizon": horizon,
        # ETF構成銘柄の判断材料（価格・過熱・決算日・ニュース・共通する形）
        "constituents": constituents,
        # 3xスリーブの実体（重複・ボラドラッグ・単一銘柄感応度）
        "leverage_sleeve": sleeve,
        # 市況レジーム（状態の記述。予測でも確率でもない）
        "regime": regime,
        # 投信の想定構成が指数に追随しているかの実測（未確認→精度が測定済み）
        "composition_check": composition,
        # 今の保有から導出した監視計画（指数・競合・構成銘柄・陳腐化検出）
        "watch_plan": watch,
        "execution_audit": execution_audit,
        "model_audit": model_audit,
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
        "forward_schedule": _forward_schedule(moomoo, forward),
        "schedule_status": (forward or {}).get("schedule_status") or {},
        "projection": base.get("projection"),
        "scenarios": base.get("scenarios"),
        "positions_assumptions": base.get("positions"),
        "vol_calibration": base.get("vol_calibration"),
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

    # 個別問い合わせも週次と同じ保護をかける。
    # **判断の質を質問の形式で変えない**（週次だけ守られている状態を作らない）。
    try:
        from src.data.yahoo_client._net import wait_for_network

        network = wait_for_network()
    except Exception as e:
        network = {"ready": None, "message": f"疎通確認に失敗: {type(e).__name__}: {e}"}

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

    # 個別銘柄でも「翌週の日程」は同じ材料で出す。
    # 以前はここが moomoo 依存だったため、常に「日程を取得できなかった」になっていた。
    symbol_forward: dict = {}
    try:
        from src.core.risk.forward_events import build_forward_section

        symbol_forward = build_forward_section(
            [{"symbol": symbol, "name": (info or {}).get("name") or symbol,
              "weight_pct": 100.0}])
    except Exception:
        symbol_forward = {}

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

    # --- 個別分析にも週次と同じ判断層を通す -------------------------------
    #
    # ここが無いと「週次レポートだけが賢い」状態になる。実際そうなっていた:
    # 政策・反証条件・一次観測・外部批評家・前提の衝突は PF パックにしか
    # 入っておらず、「トヨタってどう？」の経路には一切届いていなかった。
    #
    # **判断の質を、質問の形式で変えてはいけない。**
    holding_row = {
        "symbol": symbol,
        "name": (detail or info or {}).get("name") or symbol,
        "price": (info or {}).get("price"),
        "weight_pct": None,
        "fundamentals": dict(detail or info or {}),
        "technicals": _compact_technicals(technicals),
        "leverage": _leverage_of(symbol),
    }
    policy = _safe_symbol_policy(holding_row)
    falsification = _safe_falsification([holding_row])
    primary_filings = _safe_primary_filings([{"symbol": symbol}])
    external_views = _safe_external_views([{"symbol": symbol}])

    # 個別でも週次と同じ層を通す。**質問の形式で判断の質を変えない。**
    #
    # ETF・投信なら、その銘柄の中身（構成銘柄）まで開けないと分析にならない。
    # 「SOXL ってどう？」に価格とRSIだけ返すのは、中身を見ずに答えているのと同じ。
    # 単一銘柄の分析では「その銘柄が100%」として中身と感応度を見る。
    # 実際の保有額ではなく比率で語るので、金額は入れない（誤読を招く）。
    sleeve_row = {**holding_row, "weight_pct": 100.0, "value_jpy": 100.0}
    symbol_lookthrough = _safe_symbol_lookthrough(holding_row)
    symbol_constituents = _safe_constituent_intel(
        {"lookthrough": symbol_lookthrough}, [holding_row])
    symbol_horizon = _safe_forward_horizon(
        [sleeve_row], {"lookthrough": symbol_lookthrough})
    symbol_sleeve = _safe_leverage_sleeve(
        [sleeve_row], {"lookthrough": symbol_lookthrough}, 100.0)
    symbol_regime = _safe_regime(
        {"total_jpy": None, "cash_jpy": None}, symbol_sleeve, symbol_constituents)
    symbol_projection = _safe_symbol_projection(symbol, holding_row, technicals)
    symbol_composition = _safe_symbol_composition(symbol, holding_row.get("name"))
    # 前提の衝突（改善4）は PF 全体の話だが、**個別判断にこそ効く**:
    # 「この銘柄のテーゼは円安継続が前提。だが円高を待つ計画がある」は、
    # その銘柄を買う/持ち続ける判断に直接ぶつかる。
    assumption_space = _safe_assumption_space_for(symbol)

    return {
        "pack_version": PACK_VERSION,
        "mode": "symbol",
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "as_of": today,
            "symbol": symbol,
            "is_held": held,
            "network": network,
            # 週次と同じ判定。価格が取れていないなら、それを明示して
            # 「値動きが無かった」と読ませない。
            "data_quality": _data_quality(
                [{"symbol": symbol, "price": (info or {}).get("price")}], network),
        },
        "info": info,
        "fundamentals": {k: (detail or {}).get(k) for k in (
            "per", "forward_per", "pbr", "roe", "roa", "profit_margin",
            "operating_margin", "revenue_growth", "earnings_growth",
            "dividend_yield", "debt_to_equity", "market_cap", "beta",
            "sector", "industry", "name",
            # 成長率の出所と期間。これを落とすと、四半期YoYのスパイク
            # （2737.T の +1043% 等）が注記なしで載る。
            "growth_derived", "growth_annual", "growth_period_warning",
            "turned_profitable",
        )} if detail else (info or {}),
        "technicals": _compact_technicals(technicals),
        "wow_delta": wow,
        "narrative": narrative,
        "competitors": competitors,
        "indices": news.get("index_watch") or [],
        "news": (news.get("holding_news") or {}).get(symbol) or [],
        "market_news": news.get("market_news") or [],
        "moomoo": moomoo,
        "forward_schedule": _forward_schedule(moomoo, symbol_forward),
        "schedule_status": (symbol_forward or {}).get("schedule_status") or {},
        "prior_context": prior_context,
        # --- 週次と同じ判断層（個別質問でも必ず通す） ---
        # 政策が先。急変時に新しく判断させないため（policy-ledger.md）。
        "policy": policy,
        # 価格ではなく信念の変化を見る
        "falsification": falsification,
        # 開示原文（深度0の錨）。ニュースより系譜が強い
        "primary_filings": primary_filings,
        # 外部批評家。citation に従い、未測定なら根拠にしない
        "external_views": external_views,
        # 前方イベント（政策カバレッジの穴・トリガー距離を含む）
        "forward": symbol_forward,
        # 前提の衝突（この銘柄のテーゼが、待っている計画と食い違っていないか）
        "assumption_space": assumption_space,
        # ETF/投信なら中身まで開ける。価格とRSIだけで答えない。
        "lookthrough": symbol_lookthrough,
        "constituents": symbol_constituents,
        # 数ヶ月先の日程（翌週だけ見て「予定なし」と書かない）
        "forward_horizon": symbol_horizon,
        # レバレッジ商品ならドラッグと重複
        "leverage_sleeve": symbol_sleeve,
        # 市況レジーム（状態の記述。予測ではない）
        "regime": symbol_regime,
        # 短期(1ヶ月)/中期(3ヶ月)/長期(6ヶ月・3年)のレンジ。
        # **点推定ではない。** 前提σの帰結であり、前提が外れればレンジも外れる。
        "projection": symbol_projection,
        # 投信なら、名前が指す指数に実際に追随しているかの実測。対象外なら空。
        "composition_check": symbol_composition,
    }


def _safe_symbol_projection(
    symbol: str, holding: dict, technicals: Optional[dict]
) -> dict:
    """この銘柄100を基準にした 1ヶ月/3ヶ月/6ヶ月/3年 のレンジ。

    **金額ではなく指数(100基準)で返す。** 保有していない銘柄にも答えるので、
    金額を出すと「いくら儲かる」と読まれてしまう。倍率で語れば
    保有・非保有どちらでも同じ意味で読める。

    ボラティリティは**週次と同じ較正器**（`vol_calibration.calibrate`）に通す。
    ここで独自に実測を測って置き換えると、週次と個別で違うσを使うことになり、
    「質問の形式で判断が変わる」ことそのものになる。較正器は実測を
    そのまま採らず縮小推定で混ぜ、前提・実測・採用値を全部開示する。
    """
    try:
        from src.core.portfolio.projection import (
            HORIZON_LABELS, HORIZON_ORDER, HORIZONS, project_value,
            volatility_drag,
        )
        from src.core.portfolio.vol_calibration import calibrate
        from src.core.portfolio.weekly import _assumption, _infer_category

        lev = int(holding.get("leverage") or 1)
        assumption = _assumption(
            _infer_category({"quote_symbol": symbol}), symbol)
        vol = float(assumption.get("annual_vol_pct") or 0.0)
        ret = float(assumption.get("annual_return_pct") or 0.0)
        vol_source = "前提（カテゴリ/銘柄テーブル）"

        calib = calibrate(symbol, assumption, _closes_1y(symbol), leverage=lev)
        used = calib.get("used_underlying_vol_pct")
        if calib.get("available") and isinstance(used, (int, float)):
            vol = float(used)
            vol_source = (
                f"較正済み（前提 {assumption.get('annual_vol_pct')}% × "
                f"{calib.get('effective_window')}日実測 "
                f"{calib.get('implied_underlying_vol_pct')}%）")

        rows = []
        for key in HORIZON_ORDER:
            days = HORIZONS[key][1]
            p = project_value(100.0, ret, vol, days, lev)
            rows.append({
                "key": key, "label": HORIZON_LABELS[key], "horizon_days": days,
                "low_pct": p["low"] - 100.0,
                "mid_pct": p["mid"] - 100.0,
                "high_pct": p["high"] - 100.0,
            })

        return {
            "available": True,
            "basis": "現在値を100とした指数",
            "underlying_vol_pct": vol,
            "vol_source": vol_source,
            "annual_return_pct": ret,
            "leverage": lev,
            "drag_pct": volatility_drag(vol, lev),
            "underlying": assumption.get("underlying"),
            "calibration": calib,
            "horizons": rows,
            "note": ("**予測ではなく、置いた前提の帰結。** 前提σが外れればレンジも外れる。"
                     "確率としては読めない（中央80%区間の目安）。"),
        }
    except Exception as e:
        return {"available": False,
                "note": f"レンジを算出できませんでした（{type(e).__name__}: {e}）。"
                        "『変動しない』という意味ではありません。"}


def _closes_1y(symbol: str) -> list:
    """直近1年の終値列。取れなければ空リスト（較正器が観測不足として扱う）。"""
    try:
        from src.data import yahoo_client

        hist = yahoo_client.get_price_history(symbol, period="1y")
        if hist is None or "Close" not in getattr(hist, "columns", []):
            return []
        # ⚠️ `hist.get("Close") or []` と書いてはいけない。pandas の Series は
        # 真偽評価で ValueError を投げるので、実測σが**黙って**前提σへ
        # フォールバックする（実際なった）。
        return [float(c) for c in hist["Close"].tolist()
                if c is not None and c == c and c > 0]
    except Exception:
        return []


def _safe_symbol_lookthrough(holding: dict) -> dict:
    """この銘柄が ETF/投信なら中身を展開する。個別株なら空。

    「SOXL ってどう？」に価格とRSIだけ返すのは、**中身を見ずに答えている**のと同じ。
    """
    try:
        from src.core.risk.etf_lookthrough import build_lookthrough

        row = dict(holding)
        # ⚠️ `setdefault` は None を置き換えない。holding_row は weight_pct を
        # 明示的に None にしているので、それだと展開が丸ごと空になる（実際なった）。
        # 単一銘柄の分析では「その銘柄が100%」として中身を見る。
        if not row.get("weight_pct"):
            row["weight_pct"] = 100.0
        return build_lookthrough([row])
    except Exception as e:
        return {"available": False, "effective": [], "resolved_etfs": [],
                "unresolved": [],
                "note": f"中身を展開できませんでした（{type(e).__name__}）。"
                        "**『中身が無い』ではありません。**"}


def _safe_symbol_composition(symbol: str, name: Optional[str]) -> dict:
    """この銘柄が構成検証の対象（投信）なら、その検証結果だけを返す。

    `stock_deep.md` の材料表は `composition_check` を約束していたが、
    **個別パックはこれを一度も入れていなかった**。仕様が要求する材料が
    届かなければ、仕様の指示は空振りする。

    照合は名前で行う（投信はティッカーを持たないことがある）。
    該当しない銘柄では空を返す — **空は「構成が正しい」ではなく「対象外」**。
    """
    try:
        all_checks = _safe_composition_check()
        if not isinstance(all_checks, dict) or all_checks.get("_error"):
            return all_checks if isinstance(all_checks, dict) else {}
        keys = [str(k) for k in all_checks
                if _matches_fund(str(k), symbol, name)]
        return {k: all_checks[k] for k in keys}
    except Exception as e:
        return {"_error": f"構成を検証できませんでした（{type(e).__name__}）。"
                          "**『検証済み』ではありません。**"}


def _matches_fund(fund_name: str, symbol: str, name: Optional[str]) -> bool:
    """設定上の投信名と、問い合わせ対象が同じものを指しているか。"""
    haystack = f"{symbol or ''} {name or ''}".upper()
    fund = fund_name.upper()
    if fund in haystack:
        return True
    # 「iFreeNEXT FANG+」対「iFreeNEXT FANG+インデックス」のような表記ゆれ。
    # 語単位で見て、有意な語が全部含まれていれば同一とみなす。
    words = [w for w in fund.replace("+", " ").split() if len(w) > 2]
    return bool(words) and all(w in haystack for w in words)


def _safe_assumption_space_for(symbol: str) -> dict:
    """前提空間のうち、この銘柄に関係する部分だけを返す（改善4）。

    PF 全体の HHI ではなく、**この銘柄の前提が他の計画とぶつかっていないか**を見る。
    「円安継続が前提の銘柄」を持ちながら「円高¥155で両替する計画」があるなら、
    計画の実行条件が揃った瞬間にこの銘柄が毀損する。
    """
    try:
        from src.core.risk.assumptions import analyze_assumption_space

        result = analyze_assumption_space(holdings=[{"symbol": symbol, "value": 1.0}])
        conflicts = [
            c for c in result.get("conflicts") or []
            if not c.get("exposed_symbols") or symbol in c["exposed_symbols"]
        ]
        return {
            "conflicts": conflicts,
            "conflict_detectable": result.get("conflict_detectable"),
            "assumptions": (result.get("assumption_map") or {}),
            "note": ("この銘柄のテーゼが依存する前提と、待っている計画の前提が"
                     "同じ変数で食い違っていないかを見る。"),
        }
    except Exception as e:
        return {"conflicts": [], "conflict_detectable": None,
                "note": f"前提空間を評価できませんでした（{type(e).__name__}）。"}


def _leverage_of(symbol: str) -> Optional[float]:
    """保有設定からレバレッジ倍率を引く（金利ゲートの適用判定に使う）。"""
    try:
        import yaml

        root = Path(__file__).resolve().parent.parent.parent.parent
        cfg = yaml.safe_load(
            (root / "config" / "weekly_holdings.yaml").read_text(encoding="utf-8")) or {}
        for h in cfg.get("holdings") or []:
            if str(h.get("quote_symbol") or "") == symbol:
                return h.get("leverage")
    except Exception:
        pass
    return None


def _safe_symbol_policy(holding: dict) -> dict:
    """その銘柄の政策上の応答（案A）。**個別質問でもこれを先に引く。**

    `.claude/rules/policy-ledger.md`:
    > 急変時の質問では、まず ask を実行して政策上の応答を提示する。
    > 分析はその後、またはユーザーが明示的に求めた場合のみ。

    週次レポートにしか政策が入っていなかったため、「〇〇売るべき？」という
    **最も政策が要る場面**でこれが引かれていなかった。
    """
    symbol = holding.get("symbol") or ""
    try:
        from src.core.policy import policy_response, rate_state_from_yield_curve
        from src.core.portfolio.falsification import market_state_from_holding

        state = market_state_from_holding(holding)

        # 長期金利ゲート（改善6）。レバレッジ商品の投入判断に効く。
        try:
            if holding.get("leverage") and float(holding["leverage"]) >= 2:
                from src.core.market_dashboard import get_yield_curve

                state.update(rate_state_from_yield_curve(get_yield_curve()))
        except Exception:
            pass

        result = policy_response(symbol, state, leverage=holding.get("leverage"))
        result["market_state"] = state
        return result
    except Exception as e:
        return {"symbol": symbol, "has_policy": False, "assessments": [],
                "answer": f"政策を照会できませんでした（{type(e).__name__}）。"
                          "**『政策が無い』ではありません。**",
                "error": True}
