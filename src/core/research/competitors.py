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
    return {
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
            out[sym] = {"note": peer_note(sym), "peers": snapshots}
    return out
