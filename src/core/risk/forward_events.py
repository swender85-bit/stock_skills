"""前方イベント脊椎 — レポートの骨格を過去から未来へ (土曜設計書 提案4)。

## 名指しする問題

週次レポートは構造上どうしても回顧的になる（今週の値動き、今週のニュース）。
しかし土曜時点で最も価値がある情報は **翌週に何が確定して起きるか** である。
これは推測ではなく日程として確定している。

> 「週次レポートとは、その週に何が起きたかの報告である」は誤前提。
> スタンバイ運用における正しい機能は、**次の一週間への事前準備**である。
> 過去は既に価格に織り込まれており、行動可能性はゼロである。

## ここで計算するもの

- **イベント集中度** … 翌週に決算を通過する保有の評価額比率。
  10銘柄のうち3社が同じ週に決算なら、その週の変動リスクは平常週の数倍。
  **これは事前に完全に分かる。**
- **政策カバレッジの穴** … 決算を迎えるのに「-15%になったら何をするか」が
  未定義の銘柄。第2弾・案A（政策台帳）との合流点。
  土曜に決めるしかない（月曜以降は決められない）。
- **配当落ちの分離** … 権利落ちによる下落は損失ではない。
  週次騰落率から分離して表示しないと、ユーザーは毎回それを損失と誤認する。

## 予測装置にしない

先物・ADR は「予測」ではなく **市場の現時点の織り込み** である。
的中率の記録は取らない（設計書 提案4-⑧）。日程には必ず出典と取得時刻を添え、
「変更され得る」と明示する。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

#: イベント集中度の警告閾値（翌週決算を通過する保有の評価額比率）
CONCENTRATION_WARN_PCT = 15.0
CONCENTRATION_DANGER_PCT = 25.0

#: この評価額比率未満のイベントは折り畳む（イベント過多で重要度が埋没しないよう）
DEFAULT_MIN_WEIGHT_PCT = 1.0

_WEEKDAY_JP = ("月", "火", "水", "木", "金", "土", "日")

#: CME 日経225先物。moomoo(US LV3) には日本指数の権限が無いため yfinance で補う。
#: NKD=F はドル建て、NIY=F は円建て。円建てを優先する。
NIKKEI_FUTURES_TICKERS = ("NIY=F", "NKD=F")


# ---------------------------------------------------------------------------
# 期間
# ---------------------------------------------------------------------------


def next_week_range(as_of: Optional[date] = None) -> tuple[date, date]:
    """土曜時点から見た「翌週」= 次の月曜〜金曜。

    土曜に実行する前提だが、平日に手動実行しても壊れないように
    「as_of の翌日以降で最初の月曜」から5営業日を返す。
    """
    as_of = as_of or date.today()
    days_ahead = (7 - as_of.weekday()) % 7 or 7
    monday = as_of + timedelta(days=days_ahead)
    return monday, monday + timedelta(days=4)


def _jp_label(d: date) -> str:
    return f"{_WEEKDAY_JP[d.weekday()]} {d.month}/{d.day}"


def prior_business_day(d: date) -> date:
    """前営業日（土日のみ考慮）。

    ⚠️ 祝日は考慮できない。権利付最終日が祝日でずれる可能性があるため、
    出力側で「祝日でずれる可能性あり」と明示する。
    """
    out = d - timedelta(days=1)
    while out.weekday() >= 5:
        out -= timedelta(days=1)
    return out


# ---------------------------------------------------------------------------
# カレンダー構築
# ---------------------------------------------------------------------------


def build_calendar(
    holdings: list[dict],
    *,
    as_of: Optional[date] = None,
    events_by_symbol: Optional[dict] = None,
    moomoo: Optional[dict] = None,
    min_weight_pct: float = DEFAULT_MIN_WEIGHT_PCT,
) -> dict:
    """翌週の確定イベント集合を組み立てる。

    Args:
        holdings: パックの保有行（symbol / name / weight_pct を使う）
        events_by_symbol: `yahoo_client.get_events_for()` の戻り。None なら取得する
        moomoo: moomoo インサイト（経済指標・FOMC 等の補完）
    """
    as_of = as_of or date.today()
    start, end = next_week_range(as_of)

    # 同一銘柄を複数口座（特定/NISA等）で持っている場合、
    # 素朴に回すとイベントが二重に出て、比率も片方の口座分だけになる。
    # 銘柄単位に畳んで評価額比を合算する。
    symbols, weights, names = _aggregate_holdings(holdings)
    if events_by_symbol is None:
        events_by_symbol = _safe_fetch_events(symbols)

    events: list[dict] = []
    unavailable: list[str] = []

    for sym in symbols:
        ev = events_by_symbol.get(sym) or {}
        if not ev.get("available"):
            unavailable.append(sym)
            continue
        w = weights.get(sym)
        for iso in ev.get("earnings_dates") or []:
            d = _parse(iso)
            if d and start <= d <= end:
                events.append({
                    "kind": "earnings", "date": iso, "day_label": _jp_label(d),
                    "symbol": sym, "name": names.get(sym),
                    "title": f"{names.get(sym) or sym} 決算",
                    "weight_pct": w, "source": ev.get("source"),
                    "fetched_at": ev.get("fetched_at"),
                })
        ex = _parse(ev.get("ex_dividend_date"))
        if ex:
            # 日本株は「権利付最終日」の方が行動に関係する（買うならその日まで）
            record_day = prior_business_day(ex) if _is_jp(sym) else ex
            if start <= record_day <= end:
                events.append({
                    "kind": "ex_dividend", "date": record_day.isoformat(),
                    "day_label": _jp_label(record_day),
                    "symbol": sym, "name": names.get(sym),
                    "title": (f"{names.get(sym) or sym} "
                              + ("権利付最終日" if _is_jp(sym) else "配当落ち日")),
                    "weight_pct": w, "ex_date": ex.isoformat(),
                    "holiday_caveat": _is_jp(sym),
                    "source": ev.get("source"), "fetched_at": ev.get("fetched_at"),
                })

    # ETF は「決算が無い」のであって「日程が不明」なのではない。
    # 取得できなかった銘柄のうち ETF/投信は、中身の企業の決算に読み替える。
    events.extend(_macro_events(moomoo, start, end))
    events.sort(key=lambda e: (str(e.get("date") or ""), -(e.get("weight_pct") or 0)))

    shown = [e for e in events
             if e.get("kind") in ("economic", "fomc")
             or (e.get("weight_pct") or 0) >= min_weight_pct]
    folded = [e for e in events if e not in shown]

    return {
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "as_of": as_of.isoformat(),
        "events": shown,
        "folded": folded,
        "unavailable_symbols": unavailable,
        "note": ("日程は変更され得ます。出典と取得時刻を各イベントに付与しています。"
                 "日程が取れなかった銘柄は「予定なし」ではなく**取得できなかった**"
                 "として扱ってください。"),
    }


def _aggregate_holdings(holdings: list[dict]):
    """保有を銘柄単位に畳む。評価額比は合算、名前は最初に出たものを使う。"""
    order: list[str] = []
    weights: dict[str, Optional[float]] = {}
    names: dict[str, Optional[str]] = {}
    for h in holdings or []:
        sym = h.get("symbol")
        if not sym:
            continue
        if sym not in weights:
            order.append(sym)
            weights[sym] = None
            names[sym] = h.get("name")
        w = h.get("weight_pct")
        if isinstance(w, (int, float)):
            weights[sym] = (weights[sym] or 0.0) + float(w)
    return order, weights, names


def _safe_fetch_events(symbols: list[str]) -> dict:
    try:
        from src.data import yahoo_client as yc

        return yc.get_events_for(symbols)
    except Exception:
        return {}


def _parse(iso: Any) -> Optional[date]:
    if not iso:
        return None
    try:
        return date.fromisoformat(str(iso)[:10])
    except ValueError:
        return None


def _is_jp(symbol: Optional[str]) -> bool:
    s = str(symbol or "").upper()
    return s.endswith(".T") or s.endswith(".JP")


#: マクロイベントの退避先。moomoo が落ちた週に FOMC が黙って消えるのを防ぐ。
MACRO_CACHE_PATH = "data/cache/macro_events.json"

#: キャッシュをこの時間より古くまで使う。マクロ日程は頻繁には変わらない。
MACRO_CACHE_MAX_AGE_HOURS = 24 * 21


def _macro_cache_path() -> str:
    import os

    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    return os.path.join(root, MACRO_CACHE_PATH)


def save_macro_cache(moomoo: Optional[dict]) -> bool:
    """取得できたマクロ材料を退避する。取得できたときだけ上書きする。"""
    import json
    import os

    m = moomoo or {}
    if not (m.get("economic_events") or m.get("fed_watch")):
        return False
    try:
        path = _macro_cache_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "economic_events": m.get("economic_events") or [],
                "fed_watch": m.get("fed_watch") or {},
            }, f, ensure_ascii=False)
        return True
    except Exception:
        return False


def load_macro_cache() -> Optional[dict]:
    """退避したマクロ材料。古すぎる・読めないなら None。"""
    import json

    try:
        with open(_macro_cache_path(), encoding="utf-8") as f:
            data = json.load(f)
        stamp = datetime.fromisoformat(str(data.get("fetched_at")))
        age_h = (datetime.now(timezone.utc) - stamp).total_seconds() / 3600.0
        if age_h > MACRO_CACHE_MAX_AGE_HOURS:
            return None
        data["age_hours"] = round(age_h, 1)
        return data
    except Exception:
        return None


def _macro_events(moomoo: Optional[dict], start: date, end: date) -> list[dict]:
    """マクロイベント（経済指標・FOMC）。

    以前は moomoo だけを見ていたため、**moomoo が一時的に落ちた週は FOMC が
    黙ってカレンダーから消えていた**。「イベントが無い」と「取得できなかった」の
    混同そのもので、しかもマクロは保有全体に効くので影響が大きい。

    取得できた週に退避し、落ちた週はそれを使う。使ったことは `source` に残す
    （`moomoo(cached)`）ので、鮮度を伏せたまま最新であるかのように見せない。
    """
    out: list[dict] = []
    m = moomoo or {}
    cached_age: Optional[float] = None

    if m.get("economic_events") or m.get("fed_watch"):
        save_macro_cache(m)
    else:
        fallback = load_macro_cache()
        if fallback:
            m = fallback
            cached_age = fallback.get("age_hours")

    suffix = "(cached)" if cached_age is not None else ""

    for e in m.get("economic_events") or []:
        d = _parse(e.get("date") or e.get("release_date"))
        if not d or not (start <= d <= end):
            continue
        out.append({
            "kind": "economic", "date": d.isoformat(), "day_label": _jp_label(d),
            "title": e.get("title"), "country": e.get("country"),
            "importance": e.get("star"), "consensus": e.get("consensus"),
            "previous": e.get("previous"), "source": f"moomoo{suffix}",
            "cached_age_hours": cached_age,
        })

    fw = m.get("fed_watch") or {}
    d = _parse(fw.get("next_meeting"))
    if d and start <= d <= end:
        out.append({
            "kind": "fomc", "date": d.isoformat(), "day_label": _jp_label(d),
            "title": "FOMC 政策金利発表",
            "top_range": fw.get("top_range"), "top_prob": fw.get("top_prob"),
            "source": f"moomoo{suffix}",
            "cached_age_hours": cached_age,
        })
    return out


# ---------------------------------------------------------------------------
# イベント集中度
# ---------------------------------------------------------------------------


#: 「次回決算」として提示する上限期間。これを超える日付は参考値扱い。
DEFAULT_HORIZON_DAYS = 120

#: 日程の状態。空リストを「取得できなかった」と読ませないための区別。
SCHEDULE_STATES = ("scheduled", "none_upcoming", "no_earnings", "unavailable")


def _fund_kind(symbol: Optional[str], name: Optional[str]) -> Optional[str]:
    """ETF/投信なら種別を返す。個別株なら None。"""
    try:
        from src.core.risk.etf_lookthrough import resolve_proxy

        kind = (resolve_proxy(symbol, name) or {}).get("kind")
    except Exception:
        return None
    return kind if kind in ("leveraged_etf", "fund_proxy", "unmapped_fund") else None


def symbol_schedule_status(
    holdings: list[dict],
    *,
    as_of: Optional[date] = None,
    events_by_symbol: Optional[dict] = None,
    lookthrough_events: Optional[dict] = None,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
) -> dict:
    """銘柄ごとの日程の**状態**を返す。

    ## なぜこれが要るか

    従来、各銘柄に渡していたのは日程の**リスト**だけだった。リストが空のとき、
    それが「取得に失敗した」のか「取得できたが翌週は予定が無い」のかを
    区別する情報がどこにも無く、結果として**予定が無いだけの銘柄まで
    一律に「取得できなかった」と書かれていた**。

    空であることは状態ではない。状態を明示的に持たせる。

    | status | 意味 |
    |:---|:---|
    | `scheduled` | 翌週に確定イベントがある |
    | `none_upcoming` | 取得成功。翌週は無い（次回日付があれば添える） |
    | `no_earnings` | ETF/投信。決算という概念が無い → 中身に読み替える |
    | `unavailable` | **本当に取得できなかった** |
    """
    as_of = as_of or date.today()
    start, end = next_week_range(as_of)
    symbols, weights, names = _aggregate_holdings(holdings)
    if events_by_symbol is None:
        events_by_symbol = _safe_fetch_events(symbols)

    lt_by_symbol: dict[str, list] = {}
    for e in (lookthrough_events or {}).get("events") or []:
        for src in e.get("via") or e.get("sources") or []:
            lt_by_symbol.setdefault(str(src), []).append(e)

    out: dict[str, dict] = {}
    for sym in symbols:
        ev = events_by_symbol.get(sym) or {}
        fund = _fund_kind(sym, names.get(sym))
        row: dict[str, Any] = {
            "symbol": sym,
            "name": names.get(sym),
            "weight_pct": weights.get(sym),
            "fetched": bool(ev.get("available")),
            "source": ev.get("source"),
            "fetched_at": ev.get("fetched_at"),
            "error": ev.get("error"),
            "is_fund": bool(fund),
            "next_earnings": None,
            "days_until": None,
            "in_next_week": False,
            "ex_dividend_date": ev.get("ex_dividend_date"),
        }

        upcoming = sorted(d for d in (_parse(i) for i in ev.get("earnings_dates") or [])
                          if d and d >= as_of)
        if upcoming:
            nxt = upcoming[0]
            row["next_earnings"] = nxt.isoformat()
            row["days_until"] = (nxt - as_of).days
            row["in_next_week"] = start <= nxt <= end
            row["beyond_horizon"] = row["days_until"] > horizon_days

        ex = _parse(ev.get("ex_dividend_date"))
        ex_next_week = False
        if ex:
            record_day = prior_business_day(ex) if _is_jp(sym) else ex
            ex_next_week = start <= record_day <= end

        # ETF/投信は決算を持たない。これは欠測ではなく性質。
        if fund:
            comps = lt_by_symbol.get(sym) or []
            row["status"] = "no_earnings"
            row["fund_kind"] = fund
            row["component_events"] = comps
            row["label"] = (
                f"ETF/投信のため決算はありません。中身の企業では翌週 {len(comps)}件"
                f"の決算があります（実質 "
                f"{round(sum(c.get('effective_pct') or 0 for c in comps), 1)}%）。"
                if comps else
                "ETF/投信のため決算はありません。中身の企業にも翌週の決算は"
                "検出されませんでした。")
        elif row["in_next_week"] or ex_next_week:
            row["status"] = "scheduled"
            bits = []
            if row["in_next_week"]:
                bits.append(f"{row['next_earnings']} 決算")
            if ex_next_week:
                bits.append("配当権利日")
            row["label"] = "翌週に " + "・".join(bits) + " があります。"
        elif row["fetched"]:
            row["status"] = "none_upcoming"
            if row["next_earnings"]:
                row["label"] = (
                    f"取得済み。翌週に予定はありません。次回決算は "
                    f"{row['next_earnings']}（あと {row['days_until']}日）。")
            else:
                row["label"] = ("取得済み。翌週に予定はなく、次回決算日も"
                                "未公表です（企業側が未発表）。")
        else:
            row["status"] = "unavailable"
            row["label"] = ("⚠️ 日程を**取得できませんでした**。"
                            "「予定なし」ではありません。"
                            + (f"（{row['error']}）" if row.get("error") else ""))
        out[sym] = row
    return out


def event_concentration(calendar: dict, kinds: tuple[str, ...] = ("earnings",)) -> dict:
    """翌週に対象イベントを通過する保有の評価額比率。

    設計書 提案4-⑨-2: **評価額加重**で計算する。銘柄数ではない。
    小さい保有3件と主力1件では、同じ「3件」でもリスクが違う。
    """
    all_events = (calendar.get("events") or []) + (calendar.get("folded") or [])
    hit: dict[str, float] = {}
    for e in all_events:
        if e.get("kind") not in kinds:
            continue
        sym = e.get("symbol")
        w = e.get("weight_pct")
        if sym and isinstance(w, (int, float)):
            hit[sym] = max(hit.get(sym, 0.0), float(w))

    pct = round(sum(hit.values()), 1) if hit else 0.0
    level = ("danger" if pct >= CONCENTRATION_DANGER_PCT else
             "warning" if pct >= CONCENTRATION_WARN_PCT else "ok")

    message = None
    if level != "ok":
        message = (f"保有の {pct}% が同一週に決算を通過します。"
                   "この週のポートフォリオ変動幅は通常より大きくなります。")
    elif hit:
        message = f"翌週に決算を通過する保有は評価額比 {pct}% です。"

    return {
        "pct": pct, "level": level, "symbols": sorted(hit),
        "count": len(hit), "message": message,
        "unweighted_note": (
            "評価額が取れない保有は集中度に算入されていません。"
            if _has_unweighted(all_events, kinds) else None),
    }


def _has_unweighted(events: list[dict], kinds: tuple[str, ...]) -> bool:
    return any(e.get("kind") in kinds and e.get("symbol")
               and not isinstance(e.get("weight_pct"), (int, float))
               for e in events)


# ---------------------------------------------------------------------------
# 政策カバレッジの穴（第2弾・案A との合流点）
# ---------------------------------------------------------------------------


def policy_coverage_gaps(calendar: dict,
                         policies_by_symbol: Optional[dict] = None) -> dict:
    """決算を迎えるのに政策が無い銘柄を洗い出す。

    設計書 提案4-⑦:

    > 決算という**予告された不確実性**に対して、事前に行動を決めておくという、
    > 個人投資家がまず実行しない規律を毎週強制できる。

    決算で -15% になった場合の行動が未定義なら、それは土曜に決めるしかない。
    月曜以降は「決める」ことができない（既に動いている）。
    """
    all_events = (calendar.get("events") or []) + (calendar.get("folded") or [])
    earnings_symbols = {e.get("symbol"): e for e in all_events
                        if e.get("kind") == "earnings" and e.get("symbol")}
    if not earnings_symbols:
        return {"gaps": [], "covered": [], "message": None,
                "note": "翌週に決算を迎える保有はありません。"}

    if policies_by_symbol is None:
        policies_by_symbol = _load_policies(list(earnings_symbols))

    gaps: list[dict] = []
    covered: list[dict] = []
    for sym, ev in earnings_symbols.items():
        pols = policies_by_symbol.get(sym) or []
        row = {"symbol": sym, "name": ev.get("name"), "date": ev.get("date"),
               "weight_pct": ev.get("weight_pct"), "policies": len(pols)}
        (covered if pols else gaps).append(row)

    message = None
    if gaps:
        labels = ", ".join(str(g.get("name") or g["symbol"]) for g in gaps)
        message = (f"{labels} の{len(gaps)}銘柄に、決算ギャップ時の対応政策がありません。"
                   "決算で -15% になった場合の行動が未定義です。"
                   "→ 土曜の今、決めてください（月曜以降は決められません）。")

    return {
        "gaps": gaps, "covered": covered, "message": message,
        "how_to": ("python scripts/manage_policy.py add --symbol <SYMBOL> "
                   "--response \"<行動>\" --trigger \"price_change_pct<=-15\" "
                   "--expires <YYYY-MM-DD> --rationale \"<理由>\""),
    }


def _load_policies(symbols: list[str]) -> dict[str, list]:
    try:
        from src.core.policy.ledger import list_policies
    except Exception:
        return {}
    out: dict[str, list] = {}
    for s in symbols:
        try:
            out[s] = list_policies(symbol=s, active_only=True) or []
        except Exception:
            out[s] = []
    return out


def approaching_triggers(policies: Optional[list[dict]] = None,
                         market_state_by_symbol: Optional[dict] = None) -> dict:
    """翌週イベントと突合して「トリガー接近中の政策」を抽出する。

    設計書 提案4-⑤: 政策トリガーと翌週イベントを突合し、接近中の政策を抽出。
    """
    try:
        from src.core.policy.evaluator import evaluate_trigger
        from src.core.policy.ledger import list_policies
    except Exception:
        return {"available": False, "approaching": [], "met": [],
                "reason": "政策台帳が使えません"}

    if policies is None:
        try:
            policies = list_policies(active_only=True) or []
        except Exception:
            policies = []

    states = market_state_by_symbol or {}
    approaching: list[dict] = []
    met: list[dict] = []

    for pol in policies:
        sym = pol.get("symbol")
        state = states.get(sym) or {}
        for trg in pol.get("triggers") or []:
            try:
                ev = evaluate_trigger(trg, state)
            except Exception:
                continue
            row = {"policy_id": pol.get("id") or pol.get("policy_id"),
                   "symbol": sym, "response": pol.get("response"),
                   "expires": pol.get("expires"), **ev}
            if ev.get("state") == "met":
                met.append(row)
            elif ev.get("state") == "near":
                approaching.append(row)

    return {
        "available": True, "approaching": approaching, "met": met,
        "checked": len(policies),
        "message": (f"トリガー成立 {len(met)}件 / 接近中 {len(approaching)}件"
                    if (met or approaching) else "トリガー接近・成立はありません。"),
    }


# ---------------------------------------------------------------------------
# 配当落ちの分離
# ---------------------------------------------------------------------------


def dividend_drop_adjustments(holdings: list[dict],
                              events_by_symbol: Optional[dict] = None,
                              *, window_days: int = 7,
                              as_of: Optional[date] = None) -> dict:
    """直近に配当落ちを通過した保有を洗い出す。

    権利落ちの下落は損失ではない。週次騰落率から分離して示さないと、
    ユーザーは毎回それを損失と誤認する（設計書 提案4-①）。
    """
    as_of = as_of or date.today()
    symbols, _, _ = _aggregate_holdings(holdings)
    if events_by_symbol is None:
        events_by_symbol = _safe_fetch_events(symbols)

    by_symbol = {h.get("symbol"): h for h in holdings or []}
    out: list[dict] = []
    for sym in symbols:
        ev = events_by_symbol.get(sym) or {}
        ex = _parse(ev.get("ex_dividend_date"))
        if not ex:
            continue
        delta = (as_of - ex).days
        if not (0 <= delta <= window_days):
            continue
        h = by_symbol.get(sym) or {}
        yield_pct = _dividend_yield_pct(h)
        out.append({
            "symbol": sym, "name": h.get("name"), "ex_date": ex.isoformat(),
            "days_ago": delta,
            "week_change_pct": h.get("week_change_pct"),
            "dividend_yield_pct": yield_pct,
            "note": ("この期間の下落には配当落ち分が含まれます。"
                     "配当落ちは損失ではありません。"),
        })
    return {
        "items": out,
        "message": (f"{len(out)}銘柄が直近{window_days}日に配当落ちを通過しています。"
                    "週次騰落率から配当落ち分を差し引いて読んでください。"
                    if out else None),
    }


def _dividend_yield_pct(holding: dict) -> Optional[float]:
    f = holding.get("fundamentals") or {}
    v = f.get("dividend_yield")
    if not isinstance(v, (int, float)):
        return None
    return round(v * 100.0, 2) if abs(v) <= 1.5 else round(float(v), 2)


# ---------------------------------------------------------------------------
# 月曜寄付の見通し（予測ではなく市場の織り込み）
# ---------------------------------------------------------------------------


def monday_outlook(indices: Optional[list[dict]] = None,
                   moomoo: Optional[dict] = None) -> dict:
    """土曜時点で市場が織り込んでいる月曜の出発点。

    ⚠️ **これは予測ではない。** 金曜の東証引け後に世界で起きたことを反映した価格。
    的中率の記録は取らない（予測装置に見せないため・設計書 提案4-⑧）。
    """
    rows = {str((i or {}).get("symbol") or "").upper(): i for i in indices or []}
    nikkei = rows.get("^N225")
    futures = _nikkei_futures(moomoo)

    out: dict = {
        "available": False,
        "nikkei_close": (nikkei or {}).get("price"),
        "futures": futures,
        "gap_pct": None,
        "disclaimer": ("先物・ADR は予測ではなく、市場の現時点の織り込みです。"
                       "的中率は記録しません。"),
    }

    close = out["nikkei_close"]
    fut = (futures or {}).get("price")
    if isinstance(close, (int, float)) and isinstance(fut, (int, float)) and close:
        gap = (fut - close) / close * 100.0
        out["available"] = True
        out["gap_pct"] = round(gap, 2)
        direction = "上" if gap > 0 else ("下" if gap < 0 else "ほぼ変わらず")
        # 「上回って」を固定文言にすると、下落時に「-1.82% 上回って」と出る。
        side = "上回って" if gap > 0 else "下回って"
        src = (futures or {}).get("source") or "不明"
        out["message"] = (
            f"日経225先物（週末値 {fut:,.0f} / 出典 {src}）は東証終値"
            f"（{close:,.0f}）を {abs(gap):.2f}% {side}引けています。"
            f"月曜は{direction}寄りで始まる可能性が織り込まれています。" if gap else
            "先物は東証終値とほぼ同水準です。")
    else:
        out["message"] = ("日経225先物の週末値が取得できず、月曜寄付の織り込みは"
                          "提示できません（材料なしではなく取得不可）。")
    return out


def _nikkei_futures(moomoo: Optional[dict]) -> Optional[dict]:
    """moomoo から日経先物の週末値。取れなければ None。

    実測で US LV3 では日本指数の権限が無い。取れないことは想定内。
    """
    m = moomoo or {}
    for key in ("nikkei_futures", "futures"):
        v = m.get(key)
        if isinstance(v, dict) and isinstance(v.get("price"), (int, float)):
            return {"price": v["price"], "source": "moomoo",
                    "as_of": v.get("as_of")}

    # moomoo が無くても CME 日経先物は yfinance から取れる。
    # moomoo 単一依存のままだと、US LV3 に日本指数の権限が無いせいで
    # 「月曜寄付の織り込みは提示できない」が毎週出続ける。
    for ticker in NIKKEI_FUTURES_TICKERS:
        try:
            import yfinance as yf

            hist = yf.Ticker(ticker).history(period="5d")
            if hist is None or hist.empty:
                continue
            price = float(hist["Close"].dropna().iloc[-1])
            stamp = hist.index[-1]
            return {"price": price, "source": f"yfinance:{ticker}",
                    "as_of": getattr(stamp, "isoformat", lambda: str(stamp))()}
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# 日程変更の検出
# ---------------------------------------------------------------------------


def detect_schedule_changes(current: dict, prior: Optional[dict]) -> dict:
    """前週のカレンダーと比較して日程変更を検出する。

    決算日の変更はしばしば重要なシグナルである（設計書 提案4-⑤）。
    前週分が無い場合は「比較できない」と言う（変更なしと言わない）。
    """
    if not prior:
        return {"available": False, "changes": [],
                "reason": "前週のイベントカレンダーがありません（初回実行）"}

    def index(cal: dict) -> dict:
        out: dict[tuple, str] = {}
        for e in (cal.get("events") or []) + (cal.get("folded") or []):
            sym = e.get("symbol")
            if not sym:
                continue
            out[(sym, e.get("kind"))] = e.get("date")
        return out

    cur, old = index(current), index(prior)
    changes: list[dict] = []
    for key, new_date in cur.items():
        old_date = old.get(key)
        if old_date and old_date != new_date:
            changes.append({"symbol": key[0], "kind": key[1],
                            "previous_date": old_date, "current_date": new_date,
                            "note": "日程が変更されています。理由の確認を推奨します。"})
    for key, old_date in old.items():
        if key not in cur:
            changes.append({"symbol": key[0], "kind": key[1],
                            "previous_date": old_date, "current_date": None,
                            "note": "前週にあった予定が今週のカレンダーにありません。"})
    return {"available": True, "changes": changes,
            "message": (f"日程変更 {len(changes)}件を検出しました。"
                        if changes else "日程の変更はありません。")}


# ---------------------------------------------------------------------------
# まとめ
# ---------------------------------------------------------------------------


def build_forward_section(
    holdings: list[dict],
    *,
    as_of: Optional[date] = None,
    moomoo: Optional[dict] = None,
    indices: Optional[list[dict]] = None,
    prior_calendar: Optional[dict] = None,
) -> dict:
    """第3セクション「前方イベント」の材料を一括で作る。

    各要素は独立に失敗し得るので、1つ落ちても他は返す。
    """
    as_of = as_of or date.today()
    # 同一銘柄を複数口座で持っていても API は1回で済ませる
    symbols, _, _ = _aggregate_holdings(holdings)
    events_by_symbol = _safe_fetch_events(symbols)

    out: dict = {"generated_at": datetime.now(timezone.utc).isoformat(),
                 "errors": []}

    try:
        cal = build_calendar(holdings, as_of=as_of,
                             events_by_symbol=events_by_symbol, moomoo=moomoo)
        out["calendar"] = cal
    except Exception as e:
        out["calendar"] = {"events": [], "folded": [], "unavailable_symbols": []}
        out["errors"].append(f"カレンダー: {type(e).__name__}: {e}")
        cal = out["calendar"]

    # ETF/投信は決算を持たないので `unavailable_symbols` に落ちるが、
    # それは「日程が不明」ではない。中身の企業の決算に読み替える。
    try:
        from src.core.risk.etf_lookthrough import (
            build_lookthrough,
            lookthrough_events,
        )

        lt = build_lookthrough(holdings)
        out["lookthrough"] = lt
        out["lookthrough_events"] = lookthrough_events(lt, as_of=as_of)
    except Exception as e:
        out["lookthrough"] = None
        out["lookthrough_events"] = None
        out["errors"].append(f"ETFルックスルー: {type(e).__name__}: {e}")

    # 銘柄ごとの日程の「状態」。空リストを取得失敗と誤読させないための材料。
    try:
        out["schedule_status"] = symbol_schedule_status(
            holdings, as_of=as_of, events_by_symbol=events_by_symbol,
            lookthrough_events=out.get("lookthrough_events"))
    except Exception as e:
        out["schedule_status"] = {}
        out["errors"].append(f"日程状態: {type(e).__name__}: {e}")

    for name, fn in (
        ("concentration", lambda: event_concentration(cal)),
        ("policy_gaps", lambda: policy_coverage_gaps(cal)),
        ("dividend_drops", lambda: dividend_drop_adjustments(
            holdings, events_by_symbol, as_of=as_of)),
        ("monday_outlook", lambda: monday_outlook(indices, moomoo)),
        ("schedule_changes", lambda: detect_schedule_changes(cal, prior_calendar)),
        ("triggers", lambda: approaching_triggers(
            market_state_by_symbol=_states(holdings))),
    ):
        try:
            out[name] = fn()
        except Exception as e:
            out[name] = None
            out["errors"].append(f"{name}: {type(e).__name__}: {e}")

    # 要対応の抽出（情報量判定に渡す）
    actionable: list[dict] = []
    gaps = (out.get("policy_gaps") or {}).get("gaps") or []
    if gaps:
        actionable.append({
            "title": f"決算を迎える{len(gaps)}銘柄に対応政策がありません",
            "detail": (out["policy_gaps"].get("message") or "")})
    conc = out.get("concentration") or {}
    if conc.get("level") in ("warning", "danger"):
        actionable.append({"title": f"イベント集中度 {conc.get('pct')}%",
                           "detail": conc.get("message")})
    met = (out.get("triggers") or {}).get("met") or []
    if met:
        actionable.append({
            "title": f"政策トリガーが成立している銘柄 {len(met)}件",
            "detail": "政策に従った既定行動があります。新たに判断しないでください。"})

    lte = out.get("lookthrough_events") or {}
    total_lt = lte.get("total_effective_pct") or 0
    if total_lt >= CONCENTRATION_WARN_PCT:
        actionable.append({
            "title": f"ETF経由の実質エクスポージャー {total_lt}% が翌週に決算を通過",
            "detail": (lte.get("message") or "")
            + " ETF自体に決算は無くても、中身の企業の決算はレバレッジ倍率で効きます。"})

    out["actionable"] = actionable
    return out


def _states(holdings: list[dict]) -> dict:
    """政策トリガー評価用の市場状態を保有から作る。"""
    try:
        from src.core.portfolio.falsification import market_state_from_holding
    except Exception:
        return {}
    return {h.get("symbol"): market_state_from_holding(h)
            for h in holdings or [] if h.get("symbol")}
