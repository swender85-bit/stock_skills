"""EDINET -- **一次観測（primary_observation）の供給源**（日本株）.

## なぜこれが必要か

`edgar_client` と対になる。SEC EDGAR は米国株しか扱わないが、
**このPFの約20%は日本株**（2802.T / 9843.T / 2737.T）で、
しかも 2026-08-06 に実際に困ったのは日本株のほうだった:

> トーメンデバイス(2737.T) が4営業日で +120%（4連続ストップ高）。
> **その理由が取得できなかった。** Grok は 403、yfinance の英語ニュースは9ヶ月前。

EDINET は金商法の法定開示を提供する。ここで効くのは特に:

| 書類 | 何が分かるか |
|:---|:---|
| **大量保有報告書（5%ルール）** | **誰が大量に買ったか。** 急騰の需給要因が名前で出る |
| 変更報告書 | その後の買い増し・売り抜け |
| 臨時報告書 | 重要な事実の発生 |
| 有価証券報告書 / 四半期報告書 | 財務の開示原文 |

**+120% の急騰に大量保有報告書が出ていれば、それは需給要因の一次証拠になる。**
「材料が分からない」という今日の穴に、直接効く。

## 限界（正直に）

- **適時開示（TDnet）は含まない。** 決算短信・業績予想修正・提携発表などの
  プレスリリースは EDINET ではなく TDnet 側で、無償の公式APIが無い。
  つまり EDINET だけでは急騰理由を**必ず**特定できるわけではない。
- 大量保有報告書は**取得から5営業日以内**の提出。急騰と同時には出ない。
- 検索は**日付単位**（企業単位の索引が無い）ので、直近N日を走査して
  証券コードで絞り込む。日数を増やすとリクエストが線形に増える。

## 有効化

EDINET API v2 は購読キーを要求する（**登録は無料**）。

    EDINET_API_KEY=...

未設定なら `is_available()` が偽を返し、全関数が「取得できなかった」を返す。
**「開示が無かった」とは絶対に書かない。**

取得: https://api.edinet-fsa.go.jp/ の利用登録
"""

from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import requests

_DOCS_URL = "https://api.edinet-fsa.go.jp/api/v2/documents.json"
_DOC_URL = "https://api.edinet-fsa.go.jp/api/v2/documents/{doc_id}"
_VIEW_URL = "https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?{doc_id}"

_MIN_INTERVAL_SEC = 0.25
_last_call = [0.0]
_warned = [False]

#: 書類種別コード → 読み下し。急騰・急落の説明力が高い順に並べてある。
DOC_TYPES: dict[str, str] = {
    "350": "大量保有報告書",
    "360": "変更報告書（大量保有）",
    "140": "臨時報告書",
    "160": "臨時報告書（訂正）",
    "120": "有価証券報告書",
    "130": "有価証券報告書（訂正）",
    "043": "四半期報告書",
    "180": "公開買付届出書",
    "220": "自己株券買付状況報告書",
}

#: 需給の急変を説明しうる書類。**大量保有＝誰が買ったかが名前で出る。**
SUPPLY_DEMAND_TYPES = ("350", "360", "180", "220")

_warned_types: set[str] = set()


def api_key() -> str:
    return (os.environ.get("EDINET_API_KEY") or "").strip()


def is_available() -> bool:
    return bool(api_key())


def unavailable(reason: str = "") -> dict:
    return {
        "available": False,
        "reason": reason or (
            "EDINET_API_KEY が未設定のため取得できませんでした。"
            "これは『開示が無かった』ではありません。"
            "https://api.edinet-fsa.go.jp/ で無料登録すると購読キーが得られます。"
        ),
    }


def _throttle() -> None:
    delta = time.time() - _last_call[0]
    if delta < _MIN_INTERVAL_SEC:
        time.sleep(_MIN_INTERVAL_SEC - delta)
    _last_call[0] = time.time()


def _sec_code(symbol: str) -> Optional[str]:
    """`2737.T` → `27370`（EDINET の証券コードは5桁で末尾に 0 が付く）。"""
    sym = str(symbol or "").strip().upper()
    if not sym.endswith(".T"):
        return None
    core = sym[:-2]
    if not core.isdigit():
        return None
    return core.zfill(4) + "0"


def _get_documents(day: date, timeout: int = 20) -> Optional[list[dict]]:
    if not is_available():
        return None
    _throttle()
    try:
        res = requests.get(
            _DOCS_URL,
            params={"date": day.isoformat(), "type": 2,
                    "Subscription-Key": api_key()},
            timeout=timeout,
        )
    except Exception as exc:
        if not _warned[0]:
            print(f"⚠️  EDINET に接続できませんでした: {type(exc).__name__}")
            _warned[0] = True
        return None
    if res.status_code != 200:
        if not _warned[0]:
            print(f"⚠️  EDINET HTTP {res.status_code}\n"
                  "    401/403 の場合は EDINET_API_KEY を確認してください。")
            _warned[0] = True
        return None
    try:
        body = res.json()
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    # status 404 = その日は提出なし（土日祝など）。**これは失敗ではない。**
    return body.get("results") or []


def recent_filings(
    symbol: str,
    days: int = 14,
    doc_types: Optional[tuple[str, ...]] = None,
    today: Optional[date] = None,
) -> dict:
    """直近N日の EDINET 開示から、その銘柄のものを拾う。

    Returns
    -------
    dict
        {"available", "symbol", "sec_code", "filings", "scanned_days",
         "failed_days", "reason"}

    ⚠️ `failed_days` が 0 でないとき、その日の開示は**見えていない**。
    「開示が無かった」と読んではいけない。
    """
    if not is_available():
        return {**unavailable(), "symbol": symbol, "filings": []}

    code = _sec_code(symbol)
    if not code:
        return {"available": False, "symbol": symbol, "filings": [],
                "reason": f"{symbol} は日本株として解釈できませんでした"
                          "（EDINET は国内の法定開示のみ）。"}

    ref = today or date.today()
    filings: list[dict] = []
    scanned = 0
    failed: list[str] = []

    for i in range(days):
        day = ref - timedelta(days=i)
        if day.weekday() >= 5:          # 土日は提出が無い
            continue
        rows = _get_documents(day)
        if rows is None:
            failed.append(day.isoformat())
            continue
        scanned += 1
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("secCode") or "") != code:
                continue
            dtype = str(row.get("docTypeCode") or "")
            if doc_types and dtype not in doc_types:
                continue
            doc_id = str(row.get("docID") or "")
            filings.append({
                "doc_id": doc_id,
                "doc_type": dtype,
                "doc_type_label": DOC_TYPES.get(dtype, f"その他({dtype})"),
                "title": row.get("docDescription"),
                "filer": row.get("filerName"),
                "submitted_at": row.get("submitDateTime") or day.isoformat(),
                "period_end": row.get("periodEnd"),
                "supply_demand": dtype in SUPPLY_DEMAND_TYPES,
                "url": _VIEW_URL.format(doc_id=doc_id),
                "source": "disclosure2.edinet-fsa.go.jp",
            })

    filings.sort(key=lambda f: str(f.get("submitted_at") or ""), reverse=True)

    note = f"直近{days}日のうち {scanned}営業日を走査。"
    if failed:
        note += (f"⚠️ **{len(failed)}日分は取得できませんでした**"
                 f"（{', '.join(failed[:3])}…）。その日の開示は見えていません。")
    if not filings and not failed:
        note += "該当する開示はありませんでした（取得は成功しています）。"
    if any(f["supply_demand"] for f in filings):
        note += " 🔴 **需給に効く開示（大量保有等）が含まれます。**"

    return {
        "available": True,
        "symbol": symbol,
        "sec_code": code,
        "filings": filings,
        "scanned_days": scanned,
        "failed_days": failed,
        "note": note,
    }


def explain_move(symbol: str, days: int = 14, today: Optional[date] = None) -> dict:
    """急騰・急落の**需給側の説明**を探す。

    大量保有報告書があれば「誰が買ったか」が名前で出る。
    2026-08-06 の 2737.T（4営業日 +120%）で欲しかったのはこれ。

    **見つからないことは「需給要因ではない」の証明ではない。**
    大量保有報告書は取得から5営業日以内の提出なので、急騰と同時には出ない。
    また適時開示（TDnet）は EDINET に含まれないので、決算・提携・業績修正は
    ここでは捉えられない。
    """
    result = recent_filings(symbol, days=days, today=today)
    if not result.get("available"):
        return {**result, "explained": False}

    supply = [f for f in result["filings"] if f["supply_demand"]]
    result["supply_demand_filings"] = supply
    result["explained"] = bool(supply)
    if supply:
        names = ", ".join(sorted({str(f.get("filer") or "?") for f in supply}))
        result["explanation"] = (
            f"🔴 需給に効く開示が {len(supply)}件あります（提出者: {names}）。"
            "大量保有報告書は**誰が大量に買った/売ったか**を名前で示します。"
        )
    else:
        result["explanation"] = (
            "需給に効く開示（大量保有・公開買付等）は見つかりませんでした。"
            "⚠️ **ただしこれは『需給要因ではない』の証明ではありません。** "
            "大量保有報告書は取得から5営業日以内の提出なので急騰と同時には出ず、"
            "また決算短信・業績予想修正・提携発表などの**適時開示（TDnet）は "
            "EDINET に含まれません**。"
        )
    return result
