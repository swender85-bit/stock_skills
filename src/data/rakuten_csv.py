"""楽天証券「保有商品一覧（すべて）」CSV の読み取り。

## なぜこれが MS2 RSS より良いか

MarketSpeed II RSS の `RssPositionList` は **国内株式のみ** が対象で、
外国株式・投資信託を扱う関数は提供されていない（楽天公式ヘルプで確認）。
このポートフォリオは評価額の約8割が米国株と投信なので、RSS 経路では
本体をカバーできない。加えて Excel を開いて保存する手間が毎回かかる。

一方このCSVは、楽天証券Webの「保有商品一覧（すべて）→ CSVで保存」で
1クリックで落ちてきて、**国内株・米国株・投信・外貨預り金・為替レートが
すべて1ファイルに入っている**。数式も常駐アプリも要らない。

## ファイルの形

- 文字コード: Shift-JIS (cp932)
- 複数セクションが空行で区切られた構造。明細は `■ 保有商品詳細` の下
- 明細の見出し行:
  `種別, 銘柄コード・ティッカー, 銘柄, 口座, 保有数量, ［単位］,
   平均取得価額, ［単位］, 現在値, ［単位］, ..., 時価評価額[円], ...`
- `外貨預り金` 行は保有ではなく現金として扱う
- 末尾に `■参考為替レート` セクションがあり、USDJPY を拾える

資格情報はこのリポジトリに一切置かない。ユーザーが自分でログインして
落としたファイルを読むだけ。
"""

from __future__ import annotations

import csv
import io
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

#: 明細セクションの見出しに必ず含まれる語
_DETAIL_HEADER_KEY = "種別"

#: 見出し名 -> 内部フィールド名
_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "asset_type": ("種別",),
    "code": ("銘柄コード・ティッカー", "銘柄コード", "ティッカー"),
    "name": ("銘柄", "銘柄名"),
    "account": ("口座",),
    "shares": ("保有数量",),
    "cost_price": ("平均取得価額",),
    "price": ("現在値",),
    "market_value_jpy": ("時価評価額[円]",),
}

_NUMERIC_FIELDS = {"shares", "cost_price", "price", "market_value_jpy"}

#: 保有ではなく現金として扱う種別
_CASH_TYPES = {"外貨預り金", "預り金"}

#: 種別 -> 通貨の既定（明細に通貨列が無いため単位列と種別から決める）
_TYPE_DEFAULT_CURRENCY = {
    "国内株式": "JPY",
    "米国株式": "USD",
    "中国株式": "HKD",
    "アセアン株式": "USD",
    "投資信託": "JPY",
}


class SnapshotUnavailable(RuntimeError):
    """CSV が読めない・明細が見つからない。"""


def _decode(raw: bytes) -> str:
    for enc in ("cp932", "utf-8-sig", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise SnapshotUnavailable("文字コードを判別できませんでした（cp932/utf-8 いずれでもない）")


def _to_number(value: Any) -> Optional[float]:
    """'3,906.03' や '+96,000' を float に。数値でなければ None。"""
    if value is None:
        return None
    s = str(value).strip().replace(",", "").replace("+", "")
    if not s or s in ("-", "―", "未取得"):
        return None
    # '44,272.25 USD' のように単位が付く場合は先頭の数値だけ取る
    m = re.match(r"^-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _normalize_header(cell: Any) -> Optional[str]:
    if cell is None:
        return None
    text = str(cell).strip()
    if not text:
        return None
    for field, aliases in _COLUMN_ALIASES.items():
        if text in aliases:
            return field
    return None


def _find_detail_header(rows: list[list[str]]) -> tuple[int, dict[int, str]]:
    """明細の見出し行を探し、(行番号, 列index->フィールド名) を返す。"""
    for idx, row in enumerate(rows):
        if not row:
            continue
        if not any(str(c).strip() == _DETAIL_HEADER_KEY for c in row):
            continue
        mapping = {
            i: field
            for i, cell in enumerate(row)
            if (field := _normalize_header(cell)) is not None
        }
        # 種別・銘柄・保有数量が揃っていて初めて明細とみなす
        if {"asset_type", "name", "shares"} <= set(mapping.values()):
            return idx, mapping
    raise SnapshotUnavailable(
        "明細の見出し行（種別/銘柄/保有数量）が見つかりません。"
        "「保有商品一覧（すべて）」の CSV か確認してください。"
    )


def to_quote_symbol(code: str, asset_type: str) -> str:
    """楽天のコードを yfinance ティッカーに変換。変換できなければ空文字。

    国内株 ``2802`` -> ``2802.T`` / 米国株 ``SOXL`` -> ``SOXL``。
    投信はティッカーが無いので空（価格取得はスキップされる）。
    """
    c = (code or "").strip().upper()
    if not c:
        return ""
    if asset_type == "国内株式":
        return f"{c}.T" if c.isdigit() else c
    if asset_type == "中国株式":
        return f"{c.zfill(4)}.HK" if c.isdigit() else c
    return c


def _parse_fx(rows: list[list[str]]) -> dict[str, float]:
    """末尾の「■参考為替レート」から通貨->円レートを拾う。"""
    fx: dict[str, float] = {}
    for row in rows:
        if len(row) < 3:
            continue
        unit = str(row[2]).strip()
        m = re.match(r"^円/([A-Z]{3})$", unit)
        if not m:
            continue
        rate = _to_number(row[1])
        if rate is not None:
            fx[m.group(1)] = rate
    return fx


def read_asset_balance(path: str | os.PathLike) -> dict:
    """保有商品一覧CSVを読む。

    Returns
    -------
    dict
        ``{"holdings": [...], "cash": [...], "fx": {"USD": 163.67, ...},
        "source_path": str, "modified_at": ISO8601, "age_hours": float,
        "row_count": int}``

        holdings の各要素は rakuten_rss.read_snapshot と同じキーを持つ:
        ``name / code / account / shares / cost_price / price / currency /
        quote_symbol``（加えて ``asset_type`` と ``market_value_jpy``）。

    Raises
    ------
    SnapshotUnavailable
    """
    p = Path(path)
    if not p.exists():
        raise SnapshotUnavailable(f"CSV が見つかりません: {p}")

    text = _decode(p.read_bytes())
    rows = [list(r) for r in csv.reader(io.StringIO(text))]

    header_idx, mapping = _find_detail_header(rows)

    holdings: list[dict] = []
    cash: list[dict] = []

    for row in rows[header_idx + 1:]:
        if not row or all(str(c).strip() == "" for c in row):
            break  # 明細セクションは空行で終わる

        record: dict = {}
        for col, field in mapping.items():
            value = row[col] if col < len(row) else None
            if field in _NUMERIC_FIELDS:
                record[field] = _to_number(value)
            else:
                record[field] = str(value).strip() if value is not None else ""

        asset_type = record.get("asset_type", "")
        if not asset_type or record.get("shares") is None:
            continue

        if asset_type in _CASH_TYPES:
            cash.append({
                "name": record.get("name", ""),
                "amount": record["shares"],
                "currency": _currency_of(row, mapping, asset_type),
                "asset_type": asset_type,
            })
            continue

        record["currency"] = _currency_of(row, mapping, asset_type)
        record["quote_symbol"] = to_quote_symbol(record.get("code", ""), asset_type)
        holdings.append(record)

    if not holdings:
        raise SnapshotUnavailable(f"保有明細が0件です: {p}")

    modified = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
    age_hours = (datetime.now(timezone.utc) - modified).total_seconds() / 3600.0

    return {
        "holdings": holdings,
        "cash": cash,
        "fx": _parse_fx(rows),
        "source_path": str(p),
        "modified_at": modified.isoformat(timespec="seconds"),
        "age_hours": round(age_hours, 1),
        "row_count": len(holdings),
    }


def _currency_of(row: list[str], mapping: dict[int, str], asset_type: str) -> str:
    """明細行の通貨を決める。

    「平均取得価額」の直後の ［単位］ 列に "円" / "USD" が入っているので
    それを最優先で使い、無ければ種別から推定する。
    """
    cost_col = next((c for c, f in mapping.items() if f == "cost_price"), None)
    if cost_col is not None and cost_col + 1 < len(row):
        unit = str(row[cost_col + 1]).strip()
        if unit == "円":
            return "JPY"
        if re.match(r"^[A-Z]{3}$", unit):
            return unit
    # 現金行は「保有数量」の単位列に通貨が入る
    shares_col = next((c for c, f in mapping.items() if f == "shares"), None)
    if shares_col is not None and shares_col + 1 < len(row):
        unit = str(row[shares_col + 1]).strip()
        if re.match(r"^[A-Z]{3}$", unit):
            return unit
    return _TYPE_DEFAULT_CURRENCY.get(asset_type, "JPY")


def find_latest_csv(search_dirs: Optional[list[str | os.PathLike]] = None) -> Optional[Path]:
    """`assetbalance(all)_*.csv` の最新版を探す。

    既定は Downloads とリポジトリの data/rakuten/。見つからなければ None。
    """
    if search_dirs is None:
        search_dirs = [
            Path.home() / "Downloads",
            Path("data") / "rakuten",
        ]
    candidates: list[Path] = []
    for d in search_dirs:
        directory = Path(d)
        if not directory.is_dir():
            continue
        candidates.extend(directory.glob("assetbalance*.csv"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)
