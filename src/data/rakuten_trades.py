"""楽天証券の取引履歴CSVを約定履歴として読む（提案5 執行監査の入力）。

## なぜ必要か

2026-08-01 の週次レポートはこう書いた:

> 決定生存率 — 測定できていない（執行率0%ではない）
> 約定履歴が取れず、決定生存率は90日間測定不能。

約定履歴の取得元が `moomoo_broker.fetch_executions()` **だけ**だったのが原因。
しかし moomoo 口座には資金が入っておらず、実際の売買は全て楽天証券にある。
**取得先が実態と食い違っていた**ので、原理的に永久に測定できない状態だった。

楽天証券は「取引履歴」をCSVで落とせるので、それを直接読む。
資格情報は不要で、ログインもしない（保有CSVの取り込みと同じ方式）。

## 使い方

楽天証券Web → マイメニュー → 取引履歴 → 期間を指定 → CSVで保存 →

    python scripts/import_rakuten_trades.py            # Downloads の最新を自動検出
    python scripts/import_rakuten_trades.py --dry-run  # 読めるかだけ確認

## 見出しが違ったら

楽天のCSVは商品区分（国内株式 / 米国株式 / 投資信託）で見出しが違い、
仕様変更もあり得る。列を特定できなかった場合は**推測しない**。
検出した見出しを列挙してエラーにする。黙って0件を返すと
「約定が無かった」と誤読され、執行率0%という嘘の成績が出る。
"""

from __future__ import annotations

import csv
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

#: Downloads から自動検出するときのファイル名パターン
DEFAULT_GLOBS = ("tradehistory*.csv", "取引履歴*.csv", "*torihiki*.csv")

#: 見出し -> 内部フィールド。楽天は商品区分ごとに見出しが違うので広めに取る。
_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "executed_at": ("約定日", "約定日時", "取引日", "受渡日"),
    "symbol": ("銘柄コード・ティッカー", "銘柄コード", "ティッカー", "コード"),
    "name": ("銘柄名", "銘柄", "ファンド名"),
    "side": ("売買区分", "取引区分", "取引", "売買"),
    "account": ("口座区分", "口座"),
    "shares": ("約定数量", "数量", "数量[株]", "口数", "約定口数"),
    "price": ("約定単価", "単価", "単価[円]", "約定単価[円]",
              "約定単価[USD]", "基準価額"),
    "fee": ("手数料", "手数料[円]", "手数料等"),
    "tax": ("税金", "税金[円]", "税額"),
    "amount": ("受渡金額", "受渡金額[円]", "約定代金", "受取金額"),
    "currency": ("通貨", "決済通貨"),
}

_NUMERIC = {"shares", "price", "fee", "tax", "amount"}

#: 売買区分の表記ゆれ。**分類できない行は捨てずに side=None で残す。**
_BUY_WORDS = ("買付", "買い", "買", "現物買", "特定買", "購入", "BUY")
_SELL_WORDS = ("売却", "売付", "売り", "売", "現物売", "解約", "SELL")


class TradeHistoryUnavailable(RuntimeError):
    """CSV が読めない・取引履歴の見出しが見つからない。"""


def _decode(raw: bytes) -> str:
    for enc in ("cp932", "utf-8-sig", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise TradeHistoryUnavailable(
        "文字コードを判別できませんでした（cp932/utf-8 いずれでもない）")


def _to_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    s = str(value).strip().replace(",", "").replace("+", "")
    if not s or s in ("-", "―", "--", "未取得"):
        return None
    m = re.match(r"^-?\d+(?:\.\d+)?", s)
    try:
        return float(m.group(0)) if m else None
    except ValueError:
        return None


def _to_date(value: Any) -> Optional[str]:
    """'2026/07/23' '2026-07-23' '20260723' を ISO 日付に。"""
    s = str(value or "").strip()
    if not s:
        return None
    s = s.split()[0]
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%Y%m%d", "%y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def normalize_side(value: Any) -> Optional[str]:
    """売買区分を buy/sell に正規化する。判定できなければ None。"""
    s = str(value or "").strip().upper()
    if not s:
        return None
    # 「売」は「買」を含まないので売を先に見る（"売買"のような語の誤判定を避ける）
    for w in _SELL_WORDS:
        if w.upper() in s:
            return "sell"
    for w in _BUY_WORDS:
        if w.upper() in s:
            return "buy"
    return None


def _normalize_header(cell: Any) -> Optional[str]:
    text = str(cell or "").strip()
    if not text:
        return None
    for field, aliases in _COLUMN_ALIASES.items():
        if text in aliases:
            return field
    return None


def _find_header(rows: list[list[str]]) -> tuple[int, dict[int, str]]:
    """見出し行を探す。約定日と売買区分の両方が要る（保有CSVとの誤認を防ぐ）。"""
    best: tuple[int, dict[int, str]] = (-1, {})
    for idx, row in enumerate(rows):
        if not row:
            continue
        mapping = {i: f for i, cell in enumerate(row)
                   if (f := _normalize_header(cell)) is not None}
        fields = set(mapping.values())
        if "executed_at" in fields and "side" in fields and "symbol" in fields:
            if len(mapping) > len(best[1]):
                best = (idx, mapping)
    if best[0] < 0:
        seen = sorted({str(c).strip() for r in rows[:40] for c in r if str(c).strip()})
        raise TradeHistoryUnavailable(
            "取引履歴の見出し行が見つかりませんでした（約定日・売買区分・銘柄コードが必要）。"
            "**これは「取引が0件」という意味ではありません。**"
            f" 検出した見出し候補: {', '.join(seen[:30])}")
    return best


def parse_trades(raw: Any) -> dict:
    """取引履歴CSV（bytes / str / パス）を約定履歴に変換する。

    Returns:
        {"available", "executions", "count", "source", "detected_fields", "skipped"}
    """
    if isinstance(raw, (str, Path)) and os.path.exists(str(raw)):
        path = str(raw)
        text = _decode(Path(path).read_bytes())
    elif isinstance(raw, bytes):
        path, text = None, _decode(raw)
    else:
        path, text = None, str(raw)

    rows = list(csv.reader(text.splitlines()))
    header_idx, mapping = _find_header(rows)

    executions: list[dict] = []
    skipped: list[dict] = []
    for row in rows[header_idx + 1:]:
        if not row or not any(str(c).strip() for c in row):
            continue
        rec: dict[str, Any] = {}
        for i, field in mapping.items():
            if i < len(row):
                rec[field] = row[i]

        executed_at = _to_date(rec.get("executed_at"))
        symbol = str(rec.get("symbol") or "").strip()
        side = normalize_side(rec.get("side"))
        if not executed_at or not symbol:
            # 合計行・注記行などはここで落ちる。落とした事実は残す。
            skipped.append({"reason": "約定日または銘柄が読めない", "row": row[:6]})
            continue

        out: dict[str, Any] = {
            "symbol": _normalize_symbol(symbol),
            "raw_symbol": symbol,
            "name": (rec.get("name") or "").strip() or None,
            "side": side,
            "executed_at": executed_at,
            "account": (rec.get("account") or "").strip() or None,
            "currency": (rec.get("currency") or "").strip() or None,
            "source": "rakuten-csv",
        }
        for f in _NUMERIC:
            if f in rec:
                out[f] = _to_number(rec.get(f))
        if side is None:
            out["side_unknown_raw"] = str(rec.get("side") or "").strip()
        executions.append(out)

    return {
        "available": True,
        "source": "rakuten-csv",
        "path": path,
        "executions": executions,
        "count": len(executions),
        "detected_fields": sorted(set(mapping.values())),
        "skipped": skipped,
        "unknown_side": sum(1 for e in executions if e.get("side") is None),
    }


def _normalize_symbol(raw: str) -> str:
    """国内株の4桁コードには .T を付ける。米国ティッカーはそのまま。"""
    s = str(raw or "").strip().upper()
    if re.fullmatch(r"\d{4}", s):
        return f"{s}.T"
    return s


def find_latest(download_dir: Optional[str] = None) -> Optional[str]:
    """Downloads から最新の取引履歴CSVを探す。"""
    base = Path(download_dir or (Path.home() / "Downloads"))
    if not base.is_dir():
        return None
    hits: list[Path] = []
    for pattern in DEFAULT_GLOBS:
        hits.extend(base.glob(pattern))
    if not hits:
        return None
    return str(max(hits, key=lambda p: p.stat().st_mtime))


def load_trades(path: Optional[str] = None,
                *, days: Optional[int] = None) -> dict:
    """取引履歴を読む。パス省略時は Downloads から自動検出。

    見つからない場合は **例外ではなく** `available: False` を返す。
    「取れなかった」と「0件だった」を呼び出し側が区別できるようにする。
    """
    target = path or find_latest()
    if not target:
        return {"available": False, "executions": [], "count": 0,
                "source": "rakuten-csv",
                "error": ("取引履歴CSVが見つかりません。楽天証券Web → マイメニュー"
                          " → 取引履歴 からCSVを保存してください。"
                          "**これは「取引が0件」という意味ではありません。**")}
    try:
        result = parse_trades(target)
    except TradeHistoryUnavailable as e:
        return {"available": False, "executions": [], "count": 0,
                "source": "rakuten-csv", "path": target, "error": str(e)}

    if days:
        cutoff = _cutoff(days)
        result["executions"] = [e for e in result["executions"]
                                if (e.get("executed_at") or "") >= cutoff]
        result["count"] = len(result["executions"])
        result["window_days"] = days
    return result


def _cutoff(days: int) -> str:
    from datetime import timedelta

    return (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
