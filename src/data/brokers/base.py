"""ブローカー抽象層 — 「実在残高」の共通インタフェース (土曜設計書 提案1)。

## なぜこの層があるか

設計書の接地原理:

> 模型を真実とみなさない。**証券口座を唯一の残高真実**とし、
> 模型は週次で強制的に上書き同期される従属変数とする。

そのためには moomoo / 楽天CSV / 手動 を**同じ形**で扱う必要がある。
各ソースが違う dict を返すと、照合エンジンがソースの数だけ分岐して壊れる。

## 最重要の設計判断: 黙って古い残高を使わない

設計書 第5章-7:

> 外部API停止時、**黙って古いデータを正常値として扱わない**。

したがって `BrokerSnapshot` は「取れたかどうか」(`available`) と
「いつ時点のものか」(`as_of` / `age_hours` / `stale`) を**必ず**持つ。
呼び出し側は positions が空でも「保有ゼロ」と解釈してはならない。
`available=False` は「残高不明」であって「残高ゼロ」ではない。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

#: この鮮度を超えたスナップショットは stale 扱い（既定 30日）。
#: 保有構成は売買したときしか変わらないので日次である必要はないが、
#: 1ヶ月放置すると「この間に売買していれば反映されていない」領域に入る。
DEFAULT_MAX_AGE_HOURS = 24.0 * 30


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def make_position(
    symbol: Optional[str],
    shares: Optional[float],
    *,
    name: Optional[str] = None,
    account: Optional[str] = None,
    cost_price: Optional[float] = None,
    currency: Optional[str] = None,
    market_value: Optional[float] = None,
    market: Optional[str] = None,
    raw: Optional[dict] = None,
) -> dict:
    """1ポジションの正規形。

    `symbol` は投信のように無いことがある（その場合 `name` で同定する）。
    片方だけでも必ず入るよう、呼び出し側で保証すること。
    """
    return {
        "symbol": (symbol or "").strip() or None,
        "name": (name or "").strip() or None,
        "account": (account or "").strip() or None,
        "shares": float(shares) if isinstance(shares, (int, float)) else None,
        "cost_price": float(cost_price) if isinstance(cost_price, (int, float)) else None,
        "currency": (currency or "").strip().upper() or None,
        "market_value": float(market_value) if isinstance(market_value, (int, float)) else None,
        "market": market,
        "raw": raw or {},
    }


def make_snapshot(
    source: str,
    *,
    available: bool,
    positions: Optional[list[dict]] = None,
    cash: Optional[list[dict]] = None,
    as_of: Any = None,
    scope: Optional[list[str]] = None,
    error: Optional[str] = None,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    detail: Optional[dict] = None,
) -> dict:
    """ブローカースナップショットの正規形。

    Args:
        source: 'moomoo' / 'rakuten_csv' / 'manual' 等
        available: 実際にデータが取得できたか。**False なら残高は不明**であり、
            positions が空でも「保有ゼロ」を意味しない。
        as_of: データ自体の時点（取得時刻ではない）。CSVならエクスポート時刻。
        scope: このソースがカバーする市場のリスト（['US'] / ['JP','US','FUND'] 等）。
            照合エンジンは scope 外の銘柄について「口座に無い」と判定してはならない。
    """
    as_of_dt = _parse_dt(as_of) or (_utcnow() if available else None)
    age_hours: Optional[float] = None
    if as_of_dt is not None:
        age_hours = (_utcnow() - as_of_dt).total_seconds() / 3600.0

    stale = bool(age_hours is not None and age_hours > max_age_hours)

    return {
        "source": source,
        "available": bool(available),
        "fetched_at": _utcnow().isoformat(),
        "as_of": as_of_dt.isoformat() if as_of_dt else None,
        "age_hours": round(age_hours, 1) if age_hours is not None else None,
        "stale": stale,
        "max_age_hours": max_age_hours,
        "scope": list(scope or []),
        "positions": list(positions or []),
        "cash": list(cash or []),
        "error": error,
        "detail": detail or {},
    }


def snapshot_summary(snap: dict) -> str:
    """人間が読む1行サマリ。レポートの未照合フラグ表示に使う。"""
    src = snap.get("source") or "不明"
    if not snap.get("available"):
        return f"{src}: 取得できず（{snap.get('error') or '理由不明'}）— 残高は不明"
    n = len(snap.get("positions") or [])
    age = snap.get("age_hours")
    age_txt = f"{age / 24:.0f}日前" if isinstance(age, (int, float)) and age >= 24 else (
        f"{age:.0f}時間前" if isinstance(age, (int, float)) else "時点不明"
    )
    flag = " ⚠️古い" if snap.get("stale") else ""
    return f"{src}: {n}ポジション / {age_txt}時点{flag}"
