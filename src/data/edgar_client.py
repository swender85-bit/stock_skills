"""SEC EDGAR -- **一次観測（primary_observation）の供給源**（米国株）.

## なぜこれが必要か

系譜会計（`src/core/provenance.py`）は主張を四系譜に分ける:

    一次観測 / 外部言説 / ユーザー言明 / 自己推論

そして `PRIMARY_DOMAINS` に `sec.gov` / `edinet-fsa.go.jp` / `tdnet.info` を並べ、
`AGGREGATOR_DOMAINS`（yahoo・finnhub）は**外部言説（深度1）**に落としている。

**ところが、一次情報を実際に取りに行くコードが1行も無かった。**

    $ grep -rn "PRIMARY" src/ --include=*.py | grep -v provenance.py
    (何も出ない)

    >>> len(load_claims())
    0

結果として:

- システムが持つ全データが**深度1の外部言説**（yahoo/finnhub の加工済み指標）
- `reground()` は錨に一次観測を要求するので、**再接地が原理的に不可能**
- 案C の深度会計・汚染度警告・再接地キューが**まるごと動かない飾り**だった

ここはその空白を埋める。**yfinance の代替ではなく、系譜の底を作るためのもの。**

## 何を取るか

| 用途 | エンドポイント | 系譜 |
|:---|:---|:---|
| 提出書類の一覧（10-K/10-Q/8-K） | `data.sec.gov/submissions/CIK*.json` | 一次観測 |
| XBRL 財務事実 | `data.sec.gov/api/xbrl/companyconcept/...` | 一次観測 |
| ティッカー→CIK | `www.sec.gov/files/company_tickers.json` | （索引） |

**開示原文そのもの**なので、yfinance の加工済み指標とは系譜上の位置が違う。

## 有効化

SEC は User-Agent に連絡先を含めることを要求している（無いと 403）。
**これを必須にすることで、opt-in のゲートも兼ねる。**

    SEC_EDGAR_UA="your-name your@email.com"

未設定なら `is_available()` が偽を返し、全関数が「取得できなかった」を返す。
**「開示が無かった」とは絶対に書かない。**

## 制限

- **米国株のみ。** 日本株（2802.T / 9843.T / 2737.T）は EDINET 側（`edinet_client`）。
- SEC のレートリミットは 10 req/秒。ここでは 1 req/0.15秒 に抑えている。
- ETF（SOXL/TECL/TQQQ）は財務三表を持たない。**「取得できなかった」ではなく
  「そもそも存在しない」**として区別する。
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

from src.core.env import load_env

# 🔴 モジュール読み込み時に一度だけ .env を反映する。
# これが無かった間、SEC_EDGAR_UA は .env に設定済みでも
# 「先に finnhub/grok を import したプロセスでだけ効く」という
# インポート順依存になっていた（2026-08-11 発見）。
# 関数内で呼ぶと実行時の環境変数削除を打ち消してしまうので、
# ここで1回だけ呼ぶ。
load_env()

_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_CONCEPT_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/us-gaap/{tag}.json"

_CACHE_DIR = "data/cache"
_TICKER_CACHE = "sec_ticker_map.json"
#: ティッカー→CIK の対応は日次で足りる（新規上場の反映が1日遅れても実害が無い）
_TICKER_CACHE_MAX_AGE_H = 24.0

#: SEC の要求は 10 req/秒。余裕を持って抑える。
_MIN_INTERVAL_SEC = 0.15
_last_call = [0.0]

#: 決算で見る XBRL タグ。**複数候補を順に試す**（企業により使うタグが違う）。
FINANCIAL_TAGS: dict[str, tuple[str, ...]] = {
    "revenue": ("RevenueFromContractWithCustomerExcludingAssessedTax",
                "Revenues", "SalesRevenueNet"),
    "operating_income": ("OperatingIncomeLoss",),
    "net_income": ("NetIncomeLoss",),
    "eps_diluted": ("EarningsPerShareDiluted",),
    "operating_cf": ("NetCashProvidedByUsedInOperatingActivities",),
    "assets": ("Assets",),
    # 企業によって使うタグが違う。1つ目で古い値しか無い場合があるため複数持つ
    # （QCOM は StockholdersEquity が 2019年で止まっていた）。
    "equity": ("StockholdersEquity",
               "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
               "CommonStockholdersEquity"),
}

#: 選んだ値がこれ以上遅れていたら、次のタグ候補を試す。
_STALE_RETRY_YEARS = 1.0

#: 取りに行く提出書類。8-K は適時開示に相当し、**急騰の理由**がここに出る。
DEFAULT_FORMS = ("10-K", "10-Q", "8-K")

_warned = [False]


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def user_agent() -> str:
    return (os.environ.get("SEC_EDGAR_UA") or "").strip()


def is_available() -> bool:
    """SEC を叩ける状態か。

    User-Agent（連絡先入り）は SEC の要求であり、無いと 403 になる。
    未設定を**「開示が無い」と誤読させない**ため、明示的にゲートする。
    """
    return bool(user_agent())


def unavailable(reason: str = "") -> dict:
    return {
        "available": False,
        "reason": reason or (
            "SEC_EDGAR_UA が未設定のため取得できませんでした。"
            "これは『開示が無かった』ではありません。"
            'SEC は連絡先入りの User-Agent を要求します（例: SEC_EDGAR_UA="name you@example.com"）。'
        ),
    }


def _throttle() -> None:
    delta = time.time() - _last_call[0]
    if delta < _MIN_INTERVAL_SEC:
        time.sleep(_MIN_INTERVAL_SEC - delta)
    _last_call[0] = time.time()


def _get(url: str, timeout: int = 20, quiet_404: bool = False) -> Optional[Any]:
    """GET して JSON を返す。失敗は None（**空データと区別する**）。

    `quiet_404`: XBRL のタグ探索では 404 が正常系（その企業がそのタグを
    使っていないだけ）。ここで警告を出すと、**期待通りの動作が障害に見える**。
    """
    if not is_available():
        return None
    _throttle()
    try:
        res = requests.get(
            url,
            headers={"User-Agent": user_agent(),
                     "Accept-Encoding": "gzip, deflate",
                     "Host": url.split("/")[2]},
            timeout=timeout,
        )
    except Exception as exc:
        if not _warned[0]:
            print(f"⚠️  SEC EDGAR に接続できませんでした: {type(exc).__name__}")
            _warned[0] = True
        return None
    if res.status_code != 200:
        if res.status_code == 404 and quiet_404:
            return None          # そのタグを使っていないだけ。障害ではない。
        if not _warned[0]:
            hint = ("SEC_EDGAR_UA に連絡先（氏名とメール）を入れてください。"
                    if res.status_code == 403 else
                    "しばらく待ってから再試行してください（レート制限の可能性）。")
            print(f"⚠️  SEC EDGAR HTTP {res.status_code}\n"
                  f"    URL: {url}\n"
                  f"    {hint}")
            _warned[0] = True
        return None
    try:
        return res.json()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# ティッカー → CIK
# ---------------------------------------------------------------------------


def _cache_path(name: str) -> Path:
    d = _repo_root() / _CACHE_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d / name


def _load_ticker_map() -> Optional[dict[str, str]]:
    path = _cache_path(_TICKER_CACHE)
    if path.exists():
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
            stamp = datetime.fromisoformat(str(blob.get("fetched_at")))
            age = (datetime.now(timezone.utc) - stamp).total_seconds() / 3600.0
            if age <= _TICKER_CACHE_MAX_AGE_H:
                return blob.get("map") or {}
        except Exception:
            pass

    raw = _get(_TICKER_MAP_URL)
    if not isinstance(raw, dict):
        return None

    mapping: dict[str, str] = {}
    for row in raw.values():
        if isinstance(row, dict) and row.get("ticker") and row.get("cik_str") is not None:
            mapping[str(row["ticker"]).upper()] = str(row["cik_str"]).zfill(10)
    if not mapping:
        return None
    try:
        path.write_text(json.dumps(
            {"fetched_at": datetime.now(timezone.utc).isoformat(), "map": mapping},
            ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return mapping


def resolve_cik(symbol: str) -> Optional[str]:
    """ティッカー → 10桁 CIK。米国上場でなければ None。

    `.T` 等のサフィックスが付くものは**米国株ではない**ので、
    SEC を叩かずに None を返す（無駄なリクエストと誤解を避ける）。
    """
    sym = str(symbol or "").strip().upper()
    if not sym or "." in sym:
        return None
    mapping = _load_ticker_map()
    if not mapping:
        return None
    return mapping.get(sym)


# ---------------------------------------------------------------------------
# 提出書類
# ---------------------------------------------------------------------------


def _filing_url(cik: str, accession: str, primary_doc: str) -> str:
    acc = accession.replace("-", "")
    cik_int = str(int(cik))
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc}/{primary_doc}"


def recent_filings(
    symbol: str,
    forms: tuple[str, ...] = DEFAULT_FORMS,
    limit: int = 10,
    since: Optional[date] = None,
) -> dict:
    """直近の提出書類を返す。

    Returns
    -------
    dict
        {"available", "symbol", "cik", "filings": [...], "reason"}

    各 filing は `url` が `sec.gov` を指すので、`classify_source()` が
    **一次観測（primary_observation）** として型付けする。
    """
    if not is_available():
        return {**unavailable(), "symbol": symbol, "filings": []}

    cik = resolve_cik(symbol)
    if not cik:
        return {"available": False, "symbol": symbol, "filings": [],
                "reason": f"{symbol} は SEC の登録企業として解決できませんでした"
                          "（米国上場でない可能性があります）。"}

    data = _get(_SUBMISSIONS_URL.format(cik=cik))
    if not isinstance(data, dict):
        return {"available": False, "symbol": symbol, "cik": cik, "filings": [],
                "reason": "SEC から提出書類の一覧を取得できませんでした。"
                          "これは『提出が無かった』ではありません。"}

    recent = ((data.get("filings") or {}).get("recent") or {})
    cols = ("form", "accessionNumber", "filingDate", "reportDate",
            "primaryDocument", "primaryDocDescription")
    series = {c: recent.get(c) or [] for c in cols}
    n = min((len(v) for v in series.values()), default=0)

    out: list[dict] = []
    for i in range(n):
        form = str(series["form"][i])
        if forms and form not in forms:
            continue
        filed = str(series["filingDate"][i])
        if since:
            try:
                if date.fromisoformat(filed) < since:
                    continue
            except ValueError:
                pass
        accession = str(series["accessionNumber"][i])
        out.append({
            "form": form,
            "accession": accession,
            "filed_at": filed,
            "period": str(series["reportDate"][i] or "") or None,
            "title": str(series["primaryDocDescription"][i] or "") or form,
            "url": _filing_url(cik, accession, str(series["primaryDocument"][i])),
            "source": "sec.gov",
        })
        if len(out) >= limit:
            break

    return {
        "available": True,
        "symbol": symbol,
        "cik": cik,
        "company": data.get("name"),
        "filings": out,
        "note": ("開示原文（一次観測）。yfinance の加工済み指標とは系譜上の位置が違う。"
                 if out else
                 "指定条件に合う提出書類はありませんでした（取得は成功しています）。"),
    }


# ---------------------------------------------------------------------------
# XBRL 財務事実
# ---------------------------------------------------------------------------


def _concept(cik: str, tag: str) -> Optional[dict]:
    return _get(_CONCEPT_URL.format(cik=cik, tag=tag), quiet_404=True)


#: 損益・CF は期間量（duration）、BS は時点量（instant）。**混ぜると壊れる。**
INSTANT_LABELS = ("assets", "equity")


def _latest_annual(concept: dict, instant: bool = False) -> Optional[dict]:
    """最新の年度値を取る。

    ## 期間量と時点量を分ける理由

    売上・営業利益・営業CF は「1年間の合計」（duration・`start` あり）。
    総資産・純資産は「ある時点の残高」（instant・`start` なし）。

    両方を同じ「fp==FY かつ form==10-K」で絞ると、時点量では該当が少なく
    **6年前の値が最新として選ばれる**事故が起きる（実際に純資産で 2019年の値が
    出た）。古い残高を最新として並べると、財務の読みがまるごと狂う。

    Returns
    -------
    dict | None
        `window` は "annual"（期間量）/ "instant"（時点量）。
        `stale_years` は最新の観測からの遅れ。**0 でなければ注意が要る。**
    """
    units = (concept.get("units") or {})
    rows = units.get("USD") or units.get("USD/shares") or []
    if not rows:
        return None

    def _end(r: dict) -> str:
        return str(r.get("end") or "")

    if instant:
        # 時点量: 直近の残高。年次報告を優先しつつ、無ければ四半期でも取る
        # （古い年次より新しい四半期のほうが実態に近い）。
        annual = [r for r in rows if r.get("start") is None
                  and r.get("form") in ("10-K", "20-F")]
        pool = annual or [r for r in rows if r.get("start") is None] or rows
    else:
        # 期間量: 年度（およそ350日以上）に限る。四半期と混ぜない。
        def _is_year(r: dict) -> bool:
            try:
                s = date.fromisoformat(str(r["start"])[:10])
                e = date.fromisoformat(str(r["end"])[:10])
                return (e - s).days >= 350
            except Exception:
                return False

        pool = [r for r in rows
                if _is_year(r) and r.get("form") in ("10-K", "20-F")]
        if not pool:
            pool = [r for r in rows if _is_year(r)]
    if not pool:
        return None

    pool.sort(key=_end)
    row = pool[-1]

    # 選んだ値が、その概念で観測できる最新からどれだけ遅れているか
    newest = max((_end(r) for r in rows), default="")
    stale_years = 0.0
    try:
        chosen = date.fromisoformat(_end(row)[:10])
        latest = date.fromisoformat(newest[:10])
        stale_years = round((latest - chosen).days / 365.0, 1)
    except Exception:
        pass

    return {
        "value": row.get("val"),
        "start": row.get("start"),
        "end": row.get("end"),
        "fy": row.get("fy"),
        "form": row.get("form"),
        "accession": row.get("accn"),
        "window": "instant" if instant else "annual",
        "stale_years": stale_years,
        "latest_observed_end": newest or None,
    }


def key_financials(symbol: str) -> dict:
    """主要財務項目の**年度**値を XBRL から取る。

    yfinance の成長率は四半期YoYのスパイクを返すことがあり、
    `growth_period_warning` という回避策が必要だった。ここは
    **開示原文の年度値**なので、その曖昧さが無い。
    """
    if not is_available():
        return {**unavailable(), "symbol": symbol, "facts": {}}

    cik = resolve_cik(symbol)
    if not cik:
        return {"available": False, "symbol": symbol, "facts": {},
                "reason": f"{symbol} は SEC の登録企業として解決できませんでした。"}

    facts: dict[str, Any] = {}
    missing: list[str] = []
    stale: list[str] = []
    for label, tags in FINANCIAL_TAGS.items():
        instant = label in INSTANT_LABELS
        candidates: list[dict] = []
        for tag in tags:
            concept = _concept(cik, tag)
            if not isinstance(concept, dict):
                continue
            found = _latest_annual(concept, instant=instant)
            if not found:
                continue
            found["tag"] = tag
            candidates.append(found)
            # 時点量（BS）は**全候補を見てから最新を選ぶ**。
            # 企業がタグを途中で乗り換えていると、最初の候補が数年前で止まって
            # いることがある（QCOM の StockholdersEquity は 2019年で終わっていた）。
            # 概念内では最新なので、その概念だけ見ても古さに気づけない。
            if not instant:
                break
        if candidates:
            facts[label] = max(candidates, key=lambda c: str(c.get("end") or ""))
        else:
            missing.append(label)

    # 会社全体の最新日を基準に古さを判定する。
    # **概念内の最新ではなく、他の項目と比べて遅れているか**を見ないと、
    # 廃止されたタグが「その概念では最新」として通ってしまう。
    newest = max((str(f.get("end") or "") for f in facts.values()), default="")
    for label, fact in facts.items():
        lag = 0.0
        try:
            lag = round((date.fromisoformat(newest[:10])
                         - date.fromisoformat(str(fact["end"])[:10])).days / 365.0, 1)
        except Exception:
            pass
        fact["stale_years"] = lag
        fact["company_latest_end"] = newest or None
        if lag >= _STALE_RETRY_YEARS:
            stale.append(f"{label}({fact['end']} / 他項目は {newest})")

    if not facts:
        return {"available": False, "symbol": symbol, "cik": cik, "facts": {},
                "reason": "XBRL の財務事実を取得できませんでした。"
                          "ETF・投信には財務三表が存在しないため、"
                          "**取得失敗ではなく『存在しない』**可能性があります。"}

    note = "開示原文の**年度**値。四半期YoYのスパイクとは別物。"
    if stale:
        note += (f" ⚠️ **古い値が混ざっています**: {', '.join(stale)}。"
                 "最新として読まないでください。")
    if missing:
        note += (f" 取得できなかった項目: {', '.join(missing)}"
                 "（そのタグを使っていない可能性。**0ではありません**）。")

    return {
        "available": True,
        "symbol": symbol,
        "cik": cik,
        "facts": facts,
        "missing": missing,
        "stale": stale,
        "source": "sec.gov (XBRL)",
        "note": note,
    }
