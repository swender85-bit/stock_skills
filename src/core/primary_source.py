"""一次観測の供給 -- 系譜会計の底を作る (2026-08-06).

## 埋めている空白

`src/core/provenance.py` は四系譜（一次観測 / 外部言説 / ユーザー言明 / 自己推論）
を型として持ち、深度が閾値を超えた主張は**一次情報からの再導出（再接地）なしに
新しい解釈へ使えない**という設計になっていた。

**ところが一次情報を取りに行くコードが1行も無かった。** 実測:

    $ grep -rn "PRIMARY" src/ --include=*.py | grep -v provenance.py
    (何も出ない)
    >>> len(load_claims())
    0

つまり:

- システムの全データが **深度1の外部言説**（yahoo / finnhub は `AGGREGATOR_DOMAINS`）
- `reground()` は錨に一次観測を要求する → **再接地が原理的に不可能**
- 汚染度警告・再接地キュー・深度閾値が**まるごと動かない飾り**

ここが `edgar_client`（米国）と `edinet_client`（日本）を系譜台帳につなぐ層。

## 設計上の一線

**取得元ドメインで機械判定し、自己申告を認めない**（`classify_source()`）。

- `sec.gov` / `disclosure2.edinet-fsa.go.jp` → 一次観測（深度0）
- それ以外 → 外部言説（深度1）

FD 等の**再配信サービスから取った場合でも、そのサービスのドメインでは一次にしない。**
又聞きを一次と偽装できると、深度会計そのものが無意味になる。
再配信でも、応答に含まれる**原本の URL（sec.gov 等）を根拠にするときだけ**一次になる。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional


def _market_of(symbol: str) -> str:
    return "JP" if str(symbol or "").upper().endswith(".T") else "US"


def fetch_filings(
    symbol: str,
    days: int = 30,
    limit: int = 8,
    today: Optional[date] = None,
) -> dict:
    """その銘柄の一次開示を取る。市場で取得元を振り分ける。

    Returns
    -------
    dict
        {"available", "symbol", "market", "source", "filings", "reason", "note"}

    **`available=False` は「開示が無かった」ではなく「取得できなかった」。**
    """
    market = _market_of(symbol)

    if market == "JP":
        from src.data import edinet_client

        result = edinet_client.recent_filings(symbol, days=days, today=today)
        result["market"] = "JP"
        result["source"] = "EDINET"
        result["filings"] = (result.get("filings") or [])[:limit]
        return result

    from src.data import edgar_client

    since = (today or date.today())
    from datetime import timedelta

    result = edgar_client.recent_filings(
        symbol, limit=limit, since=since - timedelta(days=days))
    result["market"] = "US"
    result["source"] = "SEC EDGAR"
    return result


def claims_from_filings(filings_result: dict) -> list[dict]:
    """開示を系譜つきの主張（Claim）に変換する。

    **これがシステムで初めて `primary_observation`（深度0）を作る経路。**
    ここが動き出すまで、`reground()` は使える錨を1つも持てなかった。

    型付けは URL のドメインで機械判定する（`classify_source`）。
    """
    from src.core.provenance import PRIMARY, build_claim, classify_source

    if not filings_result.get("available"):
        return []

    symbol = filings_result.get("symbol", "")
    claims: list[dict] = []
    for f in filings_result.get("filings") or []:
        url = str(f.get("url") or "")
        provenance = classify_source(url)
        label = " ".join(str(x) for x in (
            f.get("form") or f.get("doc_type_label"),
            f.get("title"),
            f.get("filed_at") or f.get("submitted_at"),
        ) if x)
        claim = build_claim(
            text=label.strip(),
            provenance=provenance,
            symbol=symbol,
            source=url,
        )
        claim["filing"] = {
            "form": f.get("form") or f.get("doc_type_label"),
            "filed_at": f.get("filed_at") or f.get("submitted_at"),
            "url": url,
            "accession": f.get("accession") or f.get("doc_id"),
        }
        claims.append(claim)

    # 一次観測が1件も作れなかったのに空リストを返すと、呼び出し側が
    # 「一次根拠がある」と誤読しうる。作れた件数は呼び出し側で数える。
    _ = PRIMARY
    return claims


def build_primary_section(
    symbols: list[str],
    days: int = 30,
    limit_per_symbol: int = 5,
    today: Optional[date] = None,
    persist: bool = False,
) -> dict:
    """保有銘柄の一次開示をまとめる（ブリーフィングパック用）。

    Returns
    -------
    dict
        {"available", "by_symbol", "claims", "primary_count", "unavailable_symbols",
         "sources", "note"}

    ⚠️ `unavailable_symbols` に載った銘柄は**開示が無いのではなく見えていない**。
    """
    by_symbol: dict[str, dict] = {}
    all_claims: list[dict] = []
    unavailable_symbols: list[str] = []
    sources: set[str] = set()

    for symbol in symbols or []:
        if not symbol:
            continue
        try:
            result = fetch_filings(symbol, days=days, limit=limit_per_symbol,
                                   today=today)
        except Exception as exc:
            result = {"available": False, "symbol": symbol, "filings": [],
                      "reason": f"{type(exc).__name__}: {exc}"}
        by_symbol[symbol] = result
        if result.get("available"):
            sources.add(str(result.get("source") or ""))
            all_claims.extend(claims_from_filings(result))
        else:
            unavailable_symbols.append(symbol)

    from src.core.provenance import PRIMARY

    primary_count = sum(1 for c in all_claims if c.get("provenance") == PRIMARY)

    if persist and all_claims:
        try:
            from src.core.provenance import save_claim

            for claim in all_claims:
                save_claim(claim)
        except Exception:
            pass

    available = bool(all_claims) or bool(sources)
    note_parts: list[str] = []
    if primary_count:
        note_parts.append(
            f"一次観測（開示原文）を {primary_count}件取得しました。"
            "**これがシステムで唯一、深度0の錨になる材料です。**")
    else:
        note_parts.append(
            "一次観測を1件も取得できませんでした。"
            "**この週の解釈は全て外部言説（深度1）と自己推論の上に立っています。**")
    if unavailable_symbols:
        note_parts.append(
            f"⚠️ {', '.join(unavailable_symbols)} は開示を取得できませんでした。"
            "**『開示が無かった』ではありません。**")

    return {
        "available": available,
        "by_symbol": by_symbol,
        "claims": all_claims,
        "primary_count": primary_count,
        "unavailable_symbols": unavailable_symbols,
        "sources": sorted(s for s in sources if s),
        "days": days,
        "note": " ".join(note_parts),
    }


def source_status() -> dict:
    """一次情報源が使える状態かを一覧する（診断用）。"""
    from src.data import edgar_client, edinet_client

    return {
        "sec_edgar": {
            "available": edgar_client.is_available(),
            "market": "US",
            "env": "SEC_EDGAR_UA",
            "cost": "無料（APIキー不要。連絡先入り User-Agent が必須）",
            "reason": None if edgar_client.is_available()
                      else edgar_client.unavailable()["reason"],
        },
        "edinet": {
            "available": edinet_client.is_available(),
            "market": "JP",
            "env": "EDINET_API_KEY",
            "cost": "無料（要・無料登録）",
            "reason": None if edinet_client.is_available()
                      else edinet_client.unavailable()["reason"],
        },
    }
