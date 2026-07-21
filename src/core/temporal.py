"""Temporal discipline and seal hashing -- shared foundation (Fable5 第2弾 共通基盤).

三案（政策台帳・可知集合の凍結・系譜会計）はいずれも「時間の一級市民化」を必要とする。
本モジュールはその共通部品を提供する:

1. UTC + 市場タイムゾーン併記のタイムスタンプ規律 (`stamp`)
2. 凍結・封印用の正準ハッシュ機構 (`seal`, `verify_seal`)
3. 開示時刻と判断時刻の前後判定 (`compare_instants`)

いずれも既存動作には触れない追加レイヤー。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone, timedelta
from typing import Any, Optional


# ---------------------------------------------------------------------------
# 市場タイムゾーン
# ---------------------------------------------------------------------------

# zoneinfo は Windows で tzdata 未導入だと落ちるため、固定オフセットで持つ。
# 市場の取引時間帯は年間を通じて DST を持つもの(US/EU)と持たないもの(JP/HK)がある。
_MARKET_TZ: dict[str, tuple[str, int]] = {
    # market key: (表示名, UTCオフセット時間)
    "JP": ("Asia/Tokyo", 9),
    "HK": ("Asia/Hong_Kong", 8),
    "CN": ("Asia/Shanghai", 8),
    "TW": ("Asia/Taipei", 8),
    "KR": ("Asia/Seoul", 9),
    "SG": ("Asia/Singapore", 8),
    "US": ("America/New_York", -5),  # EST基準。DST期間は -4
    "EU": ("Europe/London", 0),
}

# ティッカーサフィックス → 市場キー
_SUFFIX_MARKET: dict[str, str] = {
    ".T": "JP", ".JP": "JP",
    ".HK": "HK",
    ".SS": "CN", ".SZ": "CN",
    ".TW": "TW", ".TWO": "TW",
    ".KS": "KR", ".KQ": "KR",
    ".SI": "SG",
    ".L": "EU", ".PA": "EU", ".DE": "EU", ".AS": "EU",
}

DEFAULT_MARKET = "US"


def market_of(symbol: str) -> str:
    """ティッカーから市場キーを推定する。サフィックスなしは米国扱い。"""
    if not symbol:
        return DEFAULT_MARKET
    for suffix, market in _SUFFIX_MARKET.items():
        if symbol.upper().endswith(suffix):
            return market
    return DEFAULT_MARKET


def _is_us_dst(dt_utc: datetime) -> bool:
    """米国DST(3月第2日曜〜11月第1日曜)の概算判定。境界日の時刻精度は問わない。"""
    year = dt_utc.year
    march = datetime(year, 3, 1, tzinfo=timezone.utc)
    second_sunday = march + timedelta(days=(6 - march.weekday()) % 7 + 7)
    november = datetime(year, 11, 1, tzinfo=timezone.utc)
    first_sunday = november + timedelta(days=(6 - november.weekday()) % 7)
    return second_sunday <= dt_utc < first_sunday


def market_offset(market: str, dt_utc: Optional[datetime] = None) -> int:
    """市場のUTCオフセット(時間)。米国のみDSTを反映する。"""
    name_offset = _MARKET_TZ.get(market.upper())
    if name_offset is None:
        name_offset = _MARKET_TZ[DEFAULT_MARKET]
    _, offset = name_offset
    if market.upper() == "US":
        ref = dt_utc or datetime.now(timezone.utc)
        if _is_us_dst(ref):
            return offset + 1
    return offset


def now_utc() -> datetime:
    """タイムゾーン付きの現在UTC時刻。"""
    return datetime.now(timezone.utc)


def stamp(market: str = DEFAULT_MARKET, at: Optional[datetime] = None) -> dict:
    """UTC + 市場ローカル時刻を併記したタイムスタンプを作る。

    全ての新規ノード・パッケージ・政策はこの形式で時刻を持つ。

    Returns
    -------
    dict
        {"utc": ISO8601(UTC), "market": "JP", "market_tz": "Asia/Tokyo",
         "market_local": ISO8601(市場ローカル), "offset_hours": 9}
    """
    dt = at or now_utc()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt_utc = dt.astimezone(timezone.utc)

    key = market.upper() if market else DEFAULT_MARKET
    tz_name = _MARKET_TZ.get(key, _MARKET_TZ[DEFAULT_MARKET])[0]
    offset = market_offset(key, dt_utc)
    local = dt_utc.astimezone(timezone(timedelta(hours=offset)))

    return {
        "utc": dt_utc.isoformat(timespec="seconds"),
        "market": key,
        "market_tz": tz_name,
        "market_local": local.isoformat(timespec="seconds"),
        "offset_hours": offset,
    }


def stamp_for_symbol(symbol: str, at: Optional[datetime] = None) -> dict:
    """ティッカーから市場を推定してタイムスタンプを作る。"""
    return stamp(market_of(symbol), at=at)


def parse_instant(value: Any) -> Optional[datetime]:
    """ISO8601文字列 / datetime / stampのdict を tz-aware UTC datetime に正規化する。

    tzinfo を持たない入力は UTC とみなす(素朴なローカル時刻の混入を防ぐため、
    呼び出し側は必ず tz 付きで渡すこと)。
    """
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("utc") or value.get("market_local")
        if value is None:
            return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def compare_instants(a: Any, b: Any) -> Optional[int]:
    """2時刻の前後を比較する。a<b で -1、a==b で 0、a>b で 1。判定不能なら None。

    案B の可知境界判定(開示時刻 vs 判断時刻)で使う。両者を UTC に正規化してから
    比較するため、市場タイムゾーンをまたいでも正しく前後がつく。
    """
    da, db = parse_instant(a), parse_instant(b)
    if da is None or db is None:
        return None
    if da < db:
        return -1
    if da > db:
        return 1
    return 0


# ---------------------------------------------------------------------------
# 封印ハッシュ
# ---------------------------------------------------------------------------

SEAL_FIELD = "seal"


def canonical_json(payload: Any) -> str:
    """ハッシュ入力用の正準JSON。キー順を固定し空白を排除する。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def seal(payload: dict) -> str:
    """payload の封印ハッシュ(sha256)を計算する。

    `seal` キー自身は計算対象から除外するため、封印値をpayloadに書き戻しても
    再計算結果は変わらない。
    """
    body = {k: v for k, v in payload.items() if k != SEAL_FIELD}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def apply_seal(payload: dict) -> dict:
    """payload に封印ハッシュを埋め込んで返す(元のdictを変更する)。"""
    payload[SEAL_FIELD] = seal(payload)
    return payload


def verify_seal(payload: dict) -> bool:
    """封印済み payload が改変されていないかを検証する。

    封印がそもそも無い場合は False(未封印を「検証済み」と誤認させない)。
    """
    recorded = payload.get(SEAL_FIELD)
    if not recorded:
        return False
    return recorded == seal(payload)
