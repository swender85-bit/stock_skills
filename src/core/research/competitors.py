"""競合・ベルウェザーの直近の値動きとバリュエーションを集めるモジュール。

ブリーフィングパックの「競合他社の決算・動向がどう効くか」節の入力になる。
保有銘柄ごとに、`config/competitors.yaml` の peers（レバレッジETFは原資産の
ベルウェザー構成銘柄）を引き、各 peer の直近価格・週間騰落・バリュエーションを
yahoo 経由で取得する。

すべて graceful degradation。設定不在・API失敗・peer不明は黙って空を返す。
決算「日程」そのものは moomoo 層（moomoo_insights）が担うため、ここでは
「今どう動いているか・割高割安か・成長しているか」を渡すことに徹する。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

DEFAULT_CONFIG = "config/competitors.yaml"


@lru_cache(maxsize=1)
def _load_config(path: str = DEFAULT_CONFIG) -> dict:
    try:
        import yaml

        p = Path(path)
        if not p.exists():
            return {}
        with p.open(encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def peers_for(symbol: str, sector: Optional[str] = None,
              holdings: Optional[list[str]] = None) -> list[str]:
    """symbol の競合/ベルウェザーを返す。

    1) competitors.yaml の明示マッピング
    2) 無ければ、同 sector の他保有銘柄でフォールバック（holdings+sectorが渡された時）
    どちらも無ければ空。
    """
    cfg = _load_config()
    peer_map = cfg.get("peers") or {}
    explicit = peer_map.get(symbol) or peer_map.get(symbol.upper())
    if explicit:
        return [p for p in explicit if p and p != symbol]
    return []


def peer_note(symbol: str) -> Optional[str]:
    notes = (_load_config().get("notes") or {})
    return notes.get(symbol) or notes.get(symbol.upper())


def _weekly_change_pct(symbol: str, client: Any) -> Optional[float]:
    """直近5営業日の始値→終値ではなく、6営業日前終値との比較で週間騰落を出す。"""
    try:
        hist = client.get_price_history(symbol, period="1mo")
        closes = [float(x) for x in hist["Close"].dropna().tolist()]
        if len(closes) < 2:
            return None
        prev = closes[-6] if len(closes) >= 6 else closes[0]
        last = closes[-1]
        return ((last - prev) / prev * 100.0) if prev else None
    except Exception:
        return None


def fetch_peer_snapshot(symbol: str, client: Any = None) -> Optional[dict]:
    """1つの peer の直近スナップショット（価格・週間騰落・バリュエーション・成長）。"""
    if client is None:
        try:
            from src.data import yahoo_client as client  # type: ignore
        except Exception:
            return None
    try:
        info = client.get_stock_info(symbol)
    except Exception:
        info = None
    if not info:
        return None
    snap = {
        "symbol": symbol,
        "name": info.get("name") or symbol,
        "price": info.get("price"),
        "week_change_pct": _weekly_change_pct(symbol, client),
        "per": info.get("per"),
        "forward_per": info.get("forward_per"),
        "revenue_growth": info.get("revenue_growth"),
        "earnings_growth": info.get("earnings_growth"),
        "sector": info.get("sector"),
        "fifty_two_week_high": info.get("fifty_two_week_high"),
        "fifty_two_week_low": info.get("fifty_two_week_low"),
    }
    snap.update(_peer_extras(symbol, client))
    return snap


def _peer_extras(symbol: str, client: Any) -> dict:
    """競合の「実績・世情・ニュース」を足す。

    週間騰落とPERだけでは**相対比較にならない**。
    「この銘柄は競合に比べて買い遅れか、割高か、決算が近いか」に答えるには、
    月次の位置・過熱・次回決算・直近ニュースが要る。
    """
    out: dict[str, Any] = {}

    # 月次・四半期の位置と過熱（週次だけでは「戻り」か「上放れ」か分からない）
    try:
        hist = client.get_price_history(symbol, period="1y")
        if hist is not None and not hist.empty:
            closes = [float(x) for x in hist["Close"].dropna().tolist()]
            if len(closes) > 22:
                out["month_change_pct"] = round(
                    (closes[-1] / closes[-22] - 1) * 100, 1)
            if len(closes) > 63:
                out["quarter_change_pct"] = round(
                    (closes[-1] / closes[-64] - 1) * 100, 1)
            try:
                from src.core.technicals import analyze_prices

                t = analyze_prices(closes) or {}
                out["rsi14"] = t.get("rsi14")
                out["sma200_deviation_pct"] = t.get("sma200_deviation_pct")
            except Exception:
                pass
    except Exception:
        pass

    # 次回決算（競合の決算は自分の銘柄の先行指標になる）
    try:
        from datetime import date

        from src.data.yahoo_client.events import get_symbol_events

        ev = get_symbol_events(symbol) or {}
        if ev.get("available"):
            upcoming = sorted(x for x in (ev.get("earnings_dates") or [])
                              if str(x) >= date.today().isoformat())
            if upcoming:
                out["next_earnings"] = upcoming[0]
                out["days_to_earnings"] = (
                    date.fromisoformat(upcoming[0][:10]) - date.today()).days
    except Exception:
        pass

    # ニュース（外部言説・深度1）。**単一の取得元に依存しない**（§16-8）
    try:
        from src.core.research.constituent_intel import _news_for

        news = _news_for(symbol, 7, 2)
        if news:
            out["news"] = news
    except Exception:
        pass

    return out


def build_peer_context(symbols: list[str], client: Any = None,
                       max_peers: int = 5) -> dict[str, dict]:
    """保有各銘柄について、競合スナップショットのリストを組み立てる。

    返り値: {holding_symbol: {"note": str|None, "peers": [snapshot, ...]}}
    peers が1つも取れなかった保有銘柄はキーごと省略する。
    """
    if client is None:
        try:
            from src.data import yahoo_client as client  # type: ignore
        except Exception:
            return {}

    out: dict[str, dict] = {}
    fetched: dict[str, Optional[dict]] = {}  # peer 重複取得を避けるキャッシュ
    for sym in symbols:
        if not sym:
            continue
        peer_syms = peers_for(sym)[:max_peers]
        if not peer_syms:
            continue
        snapshots: list[dict] = []
        for p in peer_syms:
            if p not in fetched:
                fetched[p] = fetch_peer_snapshot(p, client)
            snap = fetched[p]
            if snap:
                snapshots.append(snap)
        if snapshots:
            out[sym] = {"note": peer_note(sym), "peers": snapshots,
                        "ranking": _rank_against_peers(sym, snapshots, client)}
    return out


def _rank_against_peers(symbol: str, peers: list[dict],
                        client: Any = None) -> dict:
    """自分が競合の中でどの位置にいるか。

    「AVGO が +9.9%」だけでは意味が薄い。**自分と比べてどうか**が要る。
    「QCOM は競合5社中4位。買い遅れではなく、置いていかれている」まで言えて初めて
    判断材料になる。
    """
    try:
        info = client.get_stock_info(symbol) if client else None
    except Exception:
        info = None
    if not info:
        return {"available": False,
                "reason": "自銘柄の値が取れず相対比較できませんでした。"}

    own = {
        "week_change_pct": _weekly_change_pct(symbol, client),
        "per": info.get("per"),
        "forward_per": info.get("forward_per"),
    }

    def _rank(key: str, higher_is_better: bool) -> Optional[dict]:
        vals = [(p["symbol"], p.get(key)) for p in peers
                if isinstance(p.get(key), (int, float))]
        if own.get(key) is None or not vals:
            return None
        vals.append((symbol, own[key]))
        vals.sort(key=lambda kv: kv[1], reverse=higher_is_better)
        order = [s for s, _ in vals]
        return {"rank": order.index(symbol) + 1, "of": len(order),
                "order": order}

    week = _rank("week_change_pct", True)
    per = _rank("per", False)

    parts = []
    if week:
        parts.append(f"週間騰落 {week['rank']}/{week['of']}位")
    if per:
        parts.append(f"PERの低さ {per['rank']}/{per['of']}位")

    return {
        "available": bool(parts),
        "week_change": week,
        "per": per,
        "own": own,
        "message": (f"{symbol} は競合内で " + "、".join(parts) + "。"
                    if parts else "相対比較できる指標が足りませんでした。"),
    }
