"""moomoo (OpenD) から週次レポート用の付加インサイトを収集する。

:mod:`src.data.moomoo_client` が「クオートのフォールバック」だったのに対し、
こちらは **moomoo でしか取れない/補完になる** データ（FedWatch・経済/決算/配当
カレンダー・アナリスト/Morningstar・インサイダー取引・資金流向・市場ブレッドス
など）を集める。すべて OpenD が起動・ログイン済みのときだけ動き、取れなければ
黙って空を返す（graceful degradation）。

## 実測で確定した前提（2026-07-24, OpenD 10.9 / US LV3 権限）

- **日本株は権限なし** → JP 銘柄は対象外（yahoo に任せる）
- 米指数・米オプション権限なし → オプションIV は対象外
- ページング系 API は ``(ret, DataFrame, next_key, total)`` を返す＝データは要素**[1]**。
  一部（配当カレンダー・プレマーケット）は ``(ret, (count, DataFrame))`` と入れ子。
  この差異は :func:`_unwrap` が吸収する。

## 使い方

    from src.core.research import moomoo_insights
    data = moomoo_insights.collect_weekly_insights(["US.QCOM", "MDT", "SOXL"])
    md = moomoo_insights.format_weekly_section(data)

``collect_weekly_insights`` は OpenD への接続を **1本だけ**開いて全項目を集める。
無効化時（``MOOMOO_ENABLED`` 未設定）・OpenD 未起動・SDK 無し・例外時は
``{}`` を返し、``format_weekly_section({})`` は空文字を返す。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Optional

from src.data import moomoo_client

# ---------------------------------------------------------------------------
# 低レベルヘルパ
# ---------------------------------------------------------------------------


def _unwrap(r):
    """SDK の多様な戻り値から ``(ret, data)`` を取り出す。

    - ``(ret, df)`` / ``(ret, dict)`` → そのまま
    - ``(ret, df, next_key, total)``（ページング系）→ data は要素[1]
    - ``(ret, (count, df))``（配当/プレマーケット）→ 入れ子を1段ほどく
    """
    if not isinstance(r, tuple) or len(r) < 2:
        return None, None
    ret = r[0]
    data = r[1]
    if isinstance(data, tuple) and data:
        data = data[-1]
    return ret, data


def _f(x) -> Optional[float]:
    """'178.268' / 'N/A' / None を float or None に。"""
    if x is None:
        return None
    s = str(x).strip()
    if not s or s.upper() in ("N/A", "NAN", "NONE", "--"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _records(df, limit: Optional[int] = None) -> list[dict]:
    """DataFrame を dict のリストに。DataFrame でなければ空。"""
    try:
        rows = df.to_dict("records")
    except AttributeError:
        return []
    return rows[:limit] if limit is not None else rows


@contextmanager
def _quote_ctx():
    """OpenD への OpenQuoteContext を1本開いて閉じる。使えなければ None を yield。"""
    if not moomoo_client.is_available():
        yield None
        return
    sdk = moomoo_client._import_sdk()
    if sdk is None:
        yield None
        return
    host, port = moomoo_client._get_endpoint()
    ctx = None
    try:
        ctx = sdk.OpenQuoteContext(host=host, port=port)
        yield ctx
    except Exception:
        yield None
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass


def _ok(sdk, ret) -> bool:
    return ret == getattr(sdk, "RET_OK", 0)


def _market_us(sdk):
    return getattr(getattr(sdk, "Market", None), "US", "US")


# ---------------------------------------------------------------------------
# 個別ゲッター（すべて例外は握り潰し None/[] を返す）
# ---------------------------------------------------------------------------


def fed_watch(ctx, sdk) -> Optional[dict]:
    """FedWatch: 次回会合の最有力ターゲットレンジ + ドットプロット中央値。

    CLAUDE.md の「INTC弾は FOMC 通過後に 1/3 ずつ投入」に直結する最重要指標。
    """
    try:
        ret, target = _unwrap(ctx.get_fed_watch_target_rate())
        if not _ok(sdk, ret):
            return None
        rows = _records(target)
        if not rows:
            return None
        # 次回会合 = 最も近い meeting_date
        next_date = min(r.get("meeting_date", "9999") for r in rows)
        same = [r for r in rows if r.get("meeting_date") == next_date]
        top = max(same, key=lambda r: _f(r.get("probability")) or -1.0)

        dot = {}
        ret2, dotdf = _unwrap(ctx.get_fed_watch_dot_plot())
        if _ok(sdk, ret2):
            drows = _records(dotdf)
            if drows:
                dot = {
                    "median_rate": _f(drows[0].get("median_rate")),
                    "current_rate": _f(drows[0].get("current_rate")),
                    "year": drows[0].get("year"),
                }
        return {
            "next_meeting": next_date,
            "top_range": top.get("target_range"),
            "top_prob": _f(top.get("probability")),
            "dot_plot": dot,
        }
    except Exception:
        return None


def economic_events(ctx, sdk, days: int = 7, limit: int = 12) -> list[dict]:
    """今後 ``days`` 日の経済イベント（重要度 HIGH/MEDIUM 優先）。"""
    try:
        begin = datetime.now().strftime("%Y-%m-%d")
        end = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
        ret, df = _unwrap(ctx.get_economic_calendar(begin, end_date=end))
        if not _ok(sdk, ret):
            return []
        rows = _records(df)
        rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
        rows.sort(key=lambda r: (rank.get(str(r.get("star", "")).upper(), 3),
                                 str(r.get("timestamp", ""))))
        out = []
        for r in rows[:limit]:
            out.append({
                "title": r.get("title"),
                "country": r.get("country"),
                "star": r.get("star"),
                "consensus": r.get("consensus"),
                "previous": r.get("previous"),
            })
        return out
    except Exception:
        return []


def earnings_for(ctx, sdk, us_symbols: list[str], days: int = 30) -> list[dict]:
    """保有米国株のうち今後 ``days`` 日に決算があるもの。API上限に合わせ7日窓で分割。"""
    if not us_symbols:
        return []
    wanted = {s.upper() for s in us_symbols}
    market = _market_us(sdk)
    found: dict[str, dict] = {}
    try:
        start = datetime.now()
        # get_earnings_calendar は「7日以内」制限があるため窓を刻む
        for w in range(0, max(days, 1), 7):
            b = (start + timedelta(days=w)).strftime("%Y-%m-%d")
            e = (start + timedelta(days=min(w + 6, days))).strftime("%Y-%m-%d")
            ret, df = _unwrap(ctx.get_earnings_calendar(market, begin_date=b, end_date=e))
            if not _ok(sdk, ret):
                continue
            for r in _records(df):
                sec = str(r.get("security", "")).upper()
                if sec in wanted and sec not in found:
                    found[sec] = {
                        "security": sec,
                        "name": r.get("name"),
                        "date": r.get("earnings_date"),
                        "period": r.get("period_text"),
                        "eps_predict": _f(r.get("eps_predict")),
                    }
    except Exception:
        return list(found.values())
    return list(found.values())


def dividend_for(ctx, sdk, us_symbols: list[str], days: int = 30) -> list[dict]:
    """保有米国株の今後の配当（除権日ベース）。"""
    if not us_symbols:
        return []
    wanted = {s.upper() for s in us_symbols}
    market = _market_us(sdk)
    today = datetime.now().strftime("%Y-%m-%d")
    out: list[dict] = []
    try:
        ret, df = _unwrap(ctx.get_dividend_calendar(market, today))
        if not _ok(sdk, ret):
            return []
        for r in _records(df):
            if str(r.get("security", "")).upper() in wanted:
                out.append({
                    "security": str(r.get("security")).upper(),
                    "name": r.get("name"),
                    "statement": r.get("statement"),
                    "ex_date": r.get("ex_date"),
                    "payable": r.get("dividend_payable_date"),
                })
    except Exception:
        return out
    return out


def rating_changes_for(ctx, sdk, us_symbols: list[str], scan: int = 200) -> list[dict]:
    """市場全体のレーティング変更のうち保有銘柄に該当するもの。"""
    if not us_symbols:
        return []
    wanted = {s.upper() for s in us_symbols}
    market = _market_us(sdk)
    out: list[dict] = []
    try:
        ret, df = _unwrap(ctx.get_rating_change(market, count=scan))
        if not _ok(sdk, ret):
            return []
        for r in _records(df):
            if str(r.get("security", "")).upper() in wanted:
                out.append({
                    "security": str(r.get("security")).upper(),
                    "rating": r.get("rating"),
                    "last_rating": r.get("last_rating"),
                    "change_type": r.get("change_type"),
                    "target_price": _f(r.get("target_price")),
                    "institution": r.get("institution_name"),
                    "date": r.get("recommendation_date"),
                })
    except Exception:
        return out
    return out


def analyst_consensus(ctx, sdk, symbol: str) -> Optional[dict]:
    """アナリストコンセンサス（目標株価レンジ + buy/hold/sell%）。実株のみ。"""
    try:
        ret, d = _unwrap(ctx.get_research_analyst_consensus(symbol))
        if not _ok(sdk, ret) or not isinstance(d, dict):
            return None
        return {
            "highest": _f(d.get("highest")),
            "average": _f(d.get("average")),
            "lowest": _f(d.get("lowest")),
            "total": d.get("total"),
            "buy": _f(d.get("buy")),
            "hold": _f(d.get("hold")),
            "sell": _f(d.get("sell")),
            "update": d.get("update_time_str"),
        }
    except Exception:
        return None


def morningstar(ctx, sdk, symbol: str) -> Optional[dict]:
    """Morningstar のスターレーティング・フェアバリュー・moat。実株のみ。"""
    try:
        ret, d = _unwrap(ctx.get_research_morningstar_report(symbol))
        if not _ok(sdk, ret) or not isinstance(d, dict):
            return None
        return {
            "star_rating": d.get("star_rating"),
            "fair_value": _f(d.get("fair_value")),
            "moat": d.get("economic_moat_label"),
            "uncertainty": d.get("uncertainty_label"),
            "update": d.get("star_update_time_str"),
        }
    except Exception:
        return None


def insider_trades(ctx, sdk, symbol: str, num: int = 5) -> list[dict]:
    """直近のインサイダー取引（Form 4 等）。実株のみ。"""
    try:
        ret, df = _unwrap(ctx.get_insider_trade_list(symbol))
        if not _ok(sdk, ret):
            return []
        out = []
        for r in _records(df, limit=num):
            out.append({
                "name": r.get("name"),
                "title": r.get("title"),
                "shares": _f(r.get("trade_shares")),
                "date": r.get("max_trade_date_str"),
                "type": r.get("transaction_type"),
            })
        return out
    except Exception:
        return []


def capital_flow(ctx, sdk, symbol: str) -> Optional[dict]:
    """直近の主力資金の純流入（main_in_flow の最新有効値）。"""
    try:
        ret, df = _unwrap(ctx.get_capital_flow(symbol))
        if not _ok(sdk, ret):
            return None
        rows = _records(df)
        # main_in_flow が N/A でない最新行を後ろから探す
        for r in reversed(rows):
            main = _f(r.get("main_in_flow"))
            if main is not None:
                return {"main_in_flow": main, "time": r.get("capital_flow_item_time")}
        return None
    except Exception:
        return None


def ark_holdings(ctx, sdk, top: int = 10) -> list[dict]:
    """ARK 基金の保有上位（ウェイト順）。"""
    try:
        ret, df = _unwrap(ctx.get_ark_fund_holding(count=top))
        if not _ok(sdk, ret):
            return []
        out = []
        for r in _records(df, limit=top):
            out.append({
                "security": r.get("security"),
                "name": r.get("name"),
                "weight": _f(r.get("weight")),
                "weight_change": _f(r.get("weight_change")),
            })
        return out
    except Exception:
        return []


def heat_map(ctx, sdk, top: int = 8) -> list[dict]:
    """セクター（プレート）別の騰落上位。"""
    try:
        ret, df = _unwrap(ctx.get_heat_map_data(market=_market_us(sdk), count=top))
        if not _ok(sdk, ret):
            return []
        rows = _records(df)
        rows.sort(key=lambda r: _f(r.get("change_rate")) or 0.0, reverse=True)
        out = []
        for r in rows[:top]:
            out.append({
                "name": r.get("plate_name"),
                "change_rate": _f(r.get("change_rate")),
                "leader": r.get("leader_stock"),
            })
        return out
    except Exception:
        return []


def rise_fall(ctx, sdk) -> Optional[dict]:
    """市場ブレッドス（値上がり/値下がり分布）を上昇/下落の銘柄数に集約。"""
    try:
        ret, d = _unwrap(ctx.get_rise_fall_distribution(market=_market_us(sdk)))
        if not _ok(sdk, ret) or not isinstance(d, dict):
            return None
        up = down = flat = 0
        for seg in d.get("range_list", []) or []:
            cnt = seg.get("stock_count") or seg.get("count") or 0
            try:
                cnt = int(cnt)
            except (TypeError, ValueError):
                cnt = 0
            left = seg.get("left_border")
            if left is None:
                flat += cnt
            elif _f(left) is not None and _f(left) >= 0:
                up += cnt
            else:
                down += cnt
        if up == down == flat == 0:
            return None
        return {"up": up, "down": down, "flat": flat}
    except Exception:
        return None


def prepost_movers(ctx, sdk, kind: str = "pre", top: int = 5) -> list[dict]:
    """米株プレ/アフター/オーバーナイトの値動き上位。

    ``kind`` は ``pre`` / ``after`` / ``overnight``。
    """
    fn_name = {
        "pre": "get_us_pre_market_rank",
        "after": "get_us_after_hours_rank",
        "overnight": "get_us_overnight_rank",
    }.get(kind)
    if fn_name is None:
        return []
    fn = getattr(ctx, fn_name, None)
    if fn is None:
        return []
    try:
        ret, df = _unwrap(fn())
        if not _ok(sdk, ret):
            return []
        rows = _records(df)

        def _chg(r):
            for k in ("pre_market_change_ratio", "after_market_change_ratio",
                      "overnight_change_ratio", "change_ratio"):
                v = _f(r.get(k))
                if v is not None:
                    return v
            return 0.0

        rows.sort(key=_chg, reverse=True)
        out = []
        for r in rows[:top]:
            out.append({"security": r.get("security"), "name": r.get("name"),
                        "change_ratio": _chg(r)})
        return out
    except Exception:
        return []


def news_search(ctx, sdk, query: str, top: int = 5) -> list[dict]:
    """キーワードでニュース検索。"""
    try:
        ret, df = _unwrap(ctx.get_search_news(query))
        if not _ok(sdk, ret):
            return []
        out = []
        for r in _records(df, limit=top):
            out.append({"title": r.get("title"), "source": r.get("source"),
                        "time": r.get("publish_time"), "url": r.get("url")})
        return out
    except Exception:
        return []


# ---------------------------------------------------------------------------
# オーケストレーション
# ---------------------------------------------------------------------------


def _to_us_symbols(holdings: list[str]) -> list[str]:
    """yahoo 形式の保有シンボルを moomoo の ``US.*`` に変換（米国株のみ抽出）。"""
    out: list[str] = []
    for sym in holdings or []:
        mm = moomoo_client.to_moomoo_symbol(sym)
        if mm and mm.startswith("US.") and mm not in out:
            out.append(mm)
    return out


def collect_weekly_insights(holdings: list[str], news_query: str = "semiconductor") -> dict:
    """OpenD 接続を1本開いて週次インサイトを全収集する。

    ``holdings`` は yahoo 形式（``QCOM`` / ``MDT`` / ``SOXL`` / ``7203.T`` …）。
    JP 等の非米国は自動で除外される。使えなければ ``{}``。
    """
    us = _to_us_symbols(holdings)
    with _quote_ctx() as ctx:
        if ctx is None:
            return {}
        sdk = moomoo_client._import_sdk()
        if sdk is None:
            return {}

        data: dict = {
            "fed_watch": fed_watch(ctx, sdk),
            "economic_events": economic_events(ctx, sdk),
            "earnings": earnings_for(ctx, sdk, us),
            "dividends": dividend_for(ctx, sdk, us),
            "rating_changes": rating_changes_for(ctx, sdk, us),
            "ark": ark_holdings(ctx, sdk),
            "heat_map": heat_map(ctx, sdk),
            "rise_fall": rise_fall(ctx, sdk),
            "pre_market": prepost_movers(ctx, sdk, "pre"),
            "after_hours": prepost_movers(ctx, sdk, "after"),
            "overnight": prepost_movers(ctx, sdk, "overnight"),
            "news": news_search(ctx, sdk, news_query),
            "per_stock": {},
        }
        for sym in us:
            entry = {
                "capital_flow": capital_flow(ctx, sdk, sym),
                "analyst": analyst_consensus(ctx, sdk, sym),
                "morningstar": morningstar(ctx, sdk, sym),
                "insider": insider_trades(ctx, sdk, sym),
            }
            if any(v for v in entry.values()):
                data["per_stock"][sym] = entry
        return data


# ---------------------------------------------------------------------------
# 整形（markdown）
# ---------------------------------------------------------------------------


def _fmt_flow(v: Optional[float]) -> str:
    if v is None:
        return "—"
    sign = "+" if v >= 0 else ""
    if abs(v) >= 1e8:
        return f"{sign}{v / 1e8:.1f}億$"
    if abs(v) >= 1e4:
        return f"{sign}{v / 1e4:.0f}万$"
    return f"{sign}{v:.0f}$"


def format_weekly_section(data: dict) -> str:
    """収集結果を週次レポート用の markdown セクションにする。空なら空文字。"""
    if not data:
        return ""
    lines: list[str] = ["## moomoo インサイト（OpenD）", ""]
    had = False

    fw = data.get("fed_watch")
    if fw:
        had = True
        dot = fw.get("dot_plot") or {}
        prob = fw.get("top_prob")
        prob_s = f"{prob:.1f}%" if prob is not None else "—"
        lines.append(f"### 🎯 FedWatch（INTC弾の関門）")
        lines.append(f"- 次回会合 **{fw.get('next_meeting')}**: 最有力 "
                     f"**{fw.get('top_range')}**（{prob_s}）")
        if dot.get("median_rate") is not None:
            lines.append(f"- ドットプロット中央値 {dot.get('median_rate')}%"
                         f"（現行 {dot.get('current_rate')}%）")
        lines.append("")

    ev = data.get("economic_events") or []
    if ev:
        had = True
        lines.append("### 📅 今週の経済イベント")
        for e in ev[:8]:
            star = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "⚪"}.get(
                str(e.get("star", "")).upper(), "・")
            cons = e.get("consensus") or ""
            cons = f"（予想 {cons}）" if cons else ""
            lines.append(f"- {star} {e.get('title')} [{e.get('country')}]{cons}")
        lines.append("")

    earn = data.get("earnings") or []
    div = data.get("dividends") or []
    rc = data.get("rating_changes") or []
    if earn or div or rc:
        had = True
        lines.append("### 🔔 保有銘柄のイベント")
        for e in earn:
            lines.append(f"- 📊 決算 **{e.get('security')}** {e.get('date')}"
                         f"（{e.get('period')}）")
        for d in div:
            lines.append(f"- 💵 配当 **{d.get('security')}** 除権 {d.get('ex_date')}"
                         f" / {d.get('statement')}")
        for r in rc:
            tp = r.get("target_price")
            tp_s = f" 目標 ${tp:.0f}" if tp is not None else ""
            lines.append(f"- 📈 格付 **{r.get('security')}** {r.get('change_type')}"
                         f" → {r.get('rating')}（{r.get('institution')}）{tp_s}")
        lines.append("")

    per = data.get("per_stock") or {}
    if per:
        had = True
        lines.append("### 🔬 保有米国株の詳細")
        for sym, e in per.items():
            lines.append(f"**{sym}**")
            an = e.get("analyst")
            if an:
                def _p(v):
                    return f"{v:.0f}" if isinstance(v, (int, float)) else "—"
                lines.append(f"- アナリスト目標: 高${_p(an.get('highest'))} / "
                             f"平均${_p(an.get('average'))} / 安${_p(an.get('lowest'))}"
                             f"（買{_p(an.get('buy'))}% 中{_p(an.get('hold'))}% "
                             f"売{_p(an.get('sell'))}% n={an.get('total')}）")
            ms = e.get("morningstar")
            if ms:
                fv = ms.get("fair_value")
                fv_s = f"${fv:.2f}" if isinstance(fv, (int, float)) else "—"
                lines.append(f"- Morningstar: ★{ms.get('star_rating')} / "
                             f"フェアバリュー {fv_s} / "
                             f"moat {ms.get('moat')} / 不確実性 {ms.get('uncertainty')}")
            cf = e.get("capital_flow")
            if cf:
                lines.append(f"- 主力資金: {_fmt_flow(cf.get('main_in_flow'))}"
                             f"（{cf.get('time')}）")
            ins = e.get("insider") or []
            for t in ins[:3]:
                sh = t.get("shares")
                sh_s = f"{sh:+,.0f}株" if sh is not None else ""
                lines.append(f"- インサイダー: {t.get('name')}（{t.get('title')}）"
                             f"{t.get('type')} {sh_s} {t.get('date')}")
            lines.append("")

    rf = data.get("rise_fall")
    hm = data.get("heat_map") or []
    if rf or hm:
        had = True
        lines.append("### 🌡️ 米国市場ブレッドス")
        if rf:
            lines.append(f"- 値上がり {rf.get('up')} / 値下がり {rf.get('down')} / "
                         f"変わらず {rf.get('flat')}")
        for h in hm[:5]:
            cr = h.get("change_rate")
            cr_s = f"{cr:+.1f}%" if cr is not None else "—"
            lines.append(f"- {h.get('name')}: {cr_s}（主導 {h.get('leader')}）")
        lines.append("")

    for key, label in (("pre_market", "プレマーケット"),
                       ("after_hours", "アフターアワー"),
                       ("overnight", "オーバーナイト")):
        movers = data.get(key) or []
        if movers:
            had = True
            top = movers[0]
            cr = top.get("change_ratio")
            cr_s = f"{cr:+.1f}%" if cr is not None else "—"
            lines.append(f"- {label} 首位: {top.get('name')}（{cr_s}）")

    ark = data.get("ark") or []
    if ark:
        had = True
        lines.append("")
        lines.append("### 🚀 ARK 保有上位")
        for a in ark[:5]:
            wc = a.get("weight_change")
            wc_s = f"（{wc:+.2f}pt）" if wc is not None else ""
            lines.append(f"- {a.get('name')}: {a.get('weight')}%{wc_s}")

    news = data.get("news") or []
    if news:
        had = True
        lines.append("")
        lines.append("### 📰 関連ニュース")
        for n in news[:5]:
            lines.append(f"- {n.get('title')}（{n.get('source')}, {n.get('time')}）")

    if not had:
        return ""
    lines.append("")
    return "\n".join(lines)
