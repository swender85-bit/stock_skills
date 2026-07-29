"""物語量スナップショット — テーゼの希少性を測る土台 (土曜設計書 提案7)。

## なぜ「今すぐ記録だけ」始めるのか

設計書 提案7-⑩:

> スナップショットは記録開始以前に遡れないため、**着手の遅れがそのまま
> 恒久的な機能喪失になる**。

混雑度は `現在の物語量 / テーゼ記録時の物語量` で測る。分母が無ければ永久に測れない。
したがって分析機能（S7）より先に、**記録だけ**を動かす。

## 何を測るか — センチメントではない

設計書 第2章の禁則: GDELT を**センチメントスコアリングに使ってはならない**。
測るのは「どれだけ語られているか」であって「良い/悪い」ではない。

> 保有者にとって本当に重要な問いは「私のテーゼは、まだ少数派か」である。

センチメントが良いことは買い材料でも売り材料でもない。既に価格に入っている。
一方、**自分だけが見ていた話をみんなが見始めた**ことは、エッジの消滅を意味する。

## 混雑と混乱の分離

記事量は不祥事でも急増する。したがって記事量だけでなくトーンの分布も併せて
記録し、`crowding`（好意的言及の増加）と `turmoil`（否定的言及の急増）を
後から分離できるようにする。ここでもトーンを「評価」には使わない。
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

#: 蓄積先（gitignore 対象。銘柄ごとの追記専用 JSONL）
DEFAULT_STORE_DIR = "data/narrative"

_GDELT_DOC = "https://api.gdeltproject.org/api/v2/doc/doc"
_TIMEOUT = 12.0
_UA = "stock-skills/1.0 (narrative snapshot; weekly)"


def _enabled() -> bool:
    """既定で有効。`NARRATIVE_ENABLED=off` で無効化できる。"""
    return os.environ.get("NARRATIVE_ENABLED", "on").strip().lower() not in (
        "off", "0", "false", "no")


def _store_dir(base_dir: str = DEFAULT_STORE_DIR) -> Path:
    p = Path(base_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _key(symbol: Optional[str], name: Optional[str] = None) -> str:
    raw = (symbol or name or "unknown").strip()
    return "".join(ch if ch.isalnum() else "_" for ch in raw)[:48] or "unknown"


# ---------------------------------------------------------------------------
# GDELT — 記事量とトーン
# ---------------------------------------------------------------------------


#: GDELT は短間隔の連続リクエストを 429 で弾く。実測で 5 秒空ければ通る。
#: 週次バッチ（保有×2リクエスト）なら数分で終わるので、待つ方が正しい。
_MIN_INTERVAL = float(os.environ.get("NARRATIVE_MIN_INTERVAL", "5.5"))
_last_call: list[float] = [0.0]


def _throttle() -> None:
    import time

    wait = _MIN_INTERVAL - (time.monotonic() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.monotonic()


def _http_json(url: str, retries: int = 2) -> Optional[Any]:
    """GDELT を叩く。429 は待って数回だけ再試行し、それ以外は即諦める。

    諦めた場合は None を返し、呼び出し側は「取得できなかった」として扱う。
    **記事量ゼロと取得失敗を混同しない**（設計書の縮退原則）。
    """
    import time

    for attempt in range(retries + 1):
        _throttle()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                time.sleep(_MIN_INTERVAL * (attempt + 2))
                continue
            return None
        except Exception:
            return None
        try:
            return json.loads(raw)
        except Exception:
            # GDELT はエラー時に JSON でない本文を返すことがある
            return None
    return None


def _gdelt_query(name: Optional[str], symbol: Optional[str]) -> Optional[str]:
    """検索クエリ。会社名が最も効く。無ければティッカーで代用する。

    ティッカー単独は誤検出が多い（`MDT` 等）。名前が取れないときは
    `quality` を落として記録し、後から比較する際に注意できるようにする。
    """
    if name and len(name.strip()) >= 3:
        return f'"{name.strip()}"'
    if symbol:
        base = str(symbol).split(".")[0]
        return f'"{base}"' if len(base) >= 3 else None
    return None


#: GDELT が連続でこの回数落ちたら、このプロセスでは以後試さない。
#: 429 が続く回線では、保有数×リトライ×待機で週次バッチが何分も止まるため。
_GDELT_FAIL_LIMIT = 2
_gdelt_fails = [0]


def gdelt_circuit_open() -> bool:
    return _gdelt_fails[0] >= _GDELT_FAIL_LIMIT


def reset_circuit() -> None:
    _gdelt_fails[0] = 0


def fetch_volume(name: Optional[str], symbol: Optional[str],
                 timespan: str = "1w") -> dict:
    """GDELT から直近の記事量とトーンを取る。取れなければ available=False。"""
    if gdelt_circuit_open():
        return {"available": False, "source": "gdelt",
                "error": f"GDELT を {_GDELT_FAIL_LIMIT}回連続で取得できず、以後スキップ"}

    query = _gdelt_query(name, symbol)
    if not query:
        return {"available": False, "error": "検索クエリを組めませんでした",
                "source": "gdelt"}

    base = {"query": query, "format": "json", "timespan": timespan}

    vol = _http_json(f"{_GDELT_DOC}?" + urllib.parse.urlencode(
        {**base, "mode": "timelinevolraw"}))
    articles = _sum_timeline(vol)

    tone = _http_json(f"{_GDELT_DOC}?" + urllib.parse.urlencode(
        {**base, "mode": "timelinetone"}))
    avg_tone = _avg_timeline(tone)

    # トーンだけ取れても物語量にはならない。**記事量が取れて初めて available**。
    # ここを緩めると articles=None のスナップショットが「取得成功」として溜まり、
    # 後から混雑度の分母に使えないゴミになる。
    if articles is None:
        _gdelt_fails[0] += 1
        return {"available": False, "source": "gdelt", "query": query,
                "error": ("GDELT から記事量を取得できません"
                          + ("（トーンのみ応答）" if avg_tone is not None else ""))}

    _gdelt_fails[0] = 0
    return {
        "available": True,
        "source": "gdelt",
        "query": query,
        "timespan": timespan,
        "articles": articles,
        "avg_tone": avg_tone,
        "quality": "name" if (name and len(name.strip()) >= 3) else "ticker",
    }


def _sum_timeline(payload: Any) -> Optional[float]:
    series = _first_series(payload)
    if series is None:
        return None
    vals = [p.get("value") for p in series if isinstance(p.get("value"), (int, float))]
    return float(sum(vals)) if vals else None


def _avg_timeline(payload: Any) -> Optional[float]:
    series = _first_series(payload)
    if series is None:
        return None
    vals = [p.get("value") for p in series if isinstance(p.get("value"), (int, float))]
    return round(sum(vals) / len(vals), 3) if vals else None


def _first_series(payload: Any) -> Optional[list[dict]]:
    if not isinstance(payload, dict):
        return None
    tl = payload.get("timeline")
    if not isinstance(tl, list) or not tl:
        return None
    data = tl[0].get("data") if isinstance(tl[0], dict) else None
    return data if isinstance(data, list) else None


# ---------------------------------------------------------------------------
# アナリストカバレッジ — 混雑の代理指標
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 物語量の多重ソース
# ---------------------------------------------------------------------------
#
# GDELT は無料だが IP 単位で強く絞られ、429 が続くことがある（実測）。
# 単一ソースに賭けると、記録が途切れて**分母が永久に欠ける**。
# そこで複数ソースを持ち、取れたものを記録する。
#
# ただし **異なるソースの記事量を比で比較してはならない**（GDELT の全世界記事数と
# Finnhub の企業ニュース件数は桁も母集団も違う）。スナップショットには必ず
# `volume_source` を刻み、`crowding()` は同一ソース同士でしか比を作らない。


#: Finnhub の取得上限。低すぎると人気銘柄が全部この値に張り付いて比が死ぬ。
_FINNHUB_LIMIT = int(os.environ.get("NARRATIVE_FINNHUB_LIMIT", "1000"))


def fetch_volume_finnhub(symbol: Optional[str], days: int = 7) -> dict:
    """Finnhub の企業ニュース件数。米国株向け（日本株は無料枠で非対応）。"""
    if not symbol:
        return {"available": False, "error": "シンボルなし", "source": "finnhub"}
    try:
        from src.data import finnhub_client as fc

        if not fc.is_available():
            return {"available": False, "error": "FINNHUB_API_KEY 未設定",
                    "source": "finnhub"}
        articles = fc.get_company_news(symbol, days=days, limit=_FINNHUB_LIMIT) or []
    except Exception as e:
        return {"available": False, "error": f"{type(e).__name__}", "source": "finnhub"}

    if not articles:
        # 0件は「材料なし」ではなく「この銘柄は company-news 非対応」の可能性がある。
        return {"available": False, "error": "記事0件（非対応の可能性）",
                "source": "finnhub"}
    truncated = len(articles) >= _FINNHUB_LIMIT
    return {"available": True, "source": "finnhub", "articles": float(len(articles)),
            "avg_tone": None, "timespan": f"{days}d", "quality": "ticker",
            # 上限に張り付いた件数は比の分子・分母として使えない（飽和している）。
            # 記録はするが、混雑度側で警告できるようフラグを残す。
            "truncated": truncated}


def fetch_volume_yahoo(symbol: Optional[str]) -> dict:
    """yfinance のニュース件数。最後の砦。件数が少なく解像度は低い。"""
    if not symbol:
        return {"available": False, "error": "シンボルなし", "source": "yfinance"}
    try:
        import yfinance as yf

        news = yf.Ticker(symbol).news or []
    except Exception as e:
        return {"available": False, "error": f"{type(e).__name__}", "source": "yfinance"}
    if not news:
        return {"available": False, "error": "記事0件", "source": "yfinance"}
    return {"available": True, "source": "yfinance", "articles": float(len(news)),
            "avg_tone": None, "timespan": "recent", "quality": "ticker",
            "truncated": True}


#: 試す順序。環境変数 `NARRATIVE_SOURCES` でカンマ区切り指定可。
DEFAULT_VOLUME_SOURCES = ("gdelt", "finnhub", "yfinance")


def fetch_volume_multi(name: Optional[str], symbol: Optional[str]) -> dict:
    """使えるソースを順に試し、最初に取れたものを返す。

    全滅した場合も `available=False` と各ソースの理由を返す。
    **取得失敗を記事0件と混同しない。**
    """
    raw = os.environ.get("NARRATIVE_SOURCES", "").strip()
    order = [s.strip() for s in raw.split(",") if s.strip()] or list(
        DEFAULT_VOLUME_SOURCES)

    attempts: list[dict] = []
    for src in order:
        if src == "gdelt":
            r = fetch_volume(name, symbol)
        elif src == "finnhub":
            r = fetch_volume_finnhub(symbol)
        elif src == "yfinance":
            r = fetch_volume_yahoo(symbol)
        else:
            continue
        attempts.append({"source": src, "available": r.get("available"),
                         "error": r.get("error")})
        if r.get("available"):
            r["attempts"] = attempts
            return r
    return {"available": False, "source": None, "attempts": attempts,
            "error": "全ソースで物語量を取得できませんでした"}


def fetch_coverage(symbol: Optional[str]) -> dict:
    """カバーしているアナリスト数。増加は混雑の代理指標（設計書 提案7-④）。

    `get_stock_detail` にしか `number_of_analyst_opinions` が無いのでそちらを使う。
    ETF・投信では取れない（None）が、それは異常ではない。
    """
    if not symbol:
        return {"available": False, "error": "シンボルなし"}

    detail: dict = {}
    info: dict = {}
    try:
        from src.data import yahoo_client as yc

        detail = yc.get_stock_detail(symbol) or {}
        if not detail:
            info = yc.get_stock_info(symbol) or {}
    except Exception as e:
        return {"available": False, "error": f"{type(e).__name__}"}

    src = detail or info
    n = src.get("number_of_analyst_opinions")
    return {
        "available": n is not None,
        "analyst_count": n,
        "recommendation_mean": src.get("recommendation_mean"),
        "name": src.get("name"),
        "source": "yfinance",
        "error": None if n is not None else "アナリスト数なし（ETF/投信では通常）",
    }


# ---------------------------------------------------------------------------
# 記録
# ---------------------------------------------------------------------------


def capture(symbol: Optional[str], name: Optional[str] = None,
            *, occasion: str = "weekly", store: bool = True,
            base_dir: str = DEFAULT_STORE_DIR) -> dict:
    """1銘柄の物語量スナップショットを取り、追記保存する。

    Args:
        occasion: 'weekly'（定期）/ 'thesis'（テーゼ記録時＝基準点）/ 'manual'

    `occasion='thesis'` のスナップショットが混雑度の**分母**になる。
    """
    if not _enabled():
        return {"available": False, "error": "NARRATIVE_ENABLED=off", "symbol": symbol}

    cov = fetch_coverage(symbol)
    resolved_name = name or cov.get("name")
    vol = fetch_volume_multi(resolved_name, symbol)

    snap = {
        "symbol": symbol,
        "name": resolved_name,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "captured_date": date.today().isoformat(),
        "occasion": occasion,
        "articles": vol.get("articles"),
        "volume_source": vol.get("source"),
        "volume_timespan": vol.get("timespan"),
        "truncated": vol.get("truncated"),
        "avg_tone": vol.get("avg_tone"),
        "analyst_count": cov.get("analyst_count"),
        "query": vol.get("query"),
        "quality": vol.get("quality"),
        "attempts": vol.get("attempts"),
        # アナリスト数だけでも取れていれば記録する価値がある（混雑の代理指標）
        "available": bool(vol.get("available") or cov.get("available")),
        "errors": [e for e in (vol.get("error"), cov.get("error")) if e],
    }
    if store and snap["available"]:
        append_snapshot(snap, base_dir=base_dir)
    return snap


def append_snapshot(snap: dict, base_dir: str = DEFAULT_STORE_DIR) -> Optional[Path]:
    """追記専用。過去のスナップショットは絶対に書き換えない。"""
    try:
        path = _store_dir(base_dir) / f"{_key(snap.get('symbol'), snap.get('name'))}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(snap, ensure_ascii=False) + "\n")
        return path
    except Exception:
        return None


def load_snapshots(symbol: Optional[str], name: Optional[str] = None,
                   base_dir: str = DEFAULT_STORE_DIR) -> list[dict]:
    path = Path(base_dir) / f"{_key(symbol, name)}.jsonl"
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        return []
    out.sort(key=lambda s: str(s.get("captured_at") or ""))
    return out


def capture_many(holdings: list[dict], occasion: str = "weekly",
                 base_dir: str = DEFAULT_STORE_DIR) -> dict:
    """保有全体のスナップショットを取る（週次の入口）。

    1銘柄でも落ちたら他も止まる、という作りにはしない。
    """
    results: list[dict] = []
    for h in holdings or []:
        if not isinstance(h, dict):
            continue
        sym = h.get("symbol") or h.get("quote_symbol")
        nm = h.get("name")
        if not sym and not nm:
            continue
        try:
            results.append(capture(sym, nm, occasion=occasion, base_dir=base_dir))
        except Exception as e:
            results.append({"symbol": sym, "name": nm, "available": False,
                            "errors": [f"{type(e).__name__}"]})
    ok = [r for r in results if r.get("available")]
    return {
        "captured": len(ok),
        "attempted": len(results),
        "occasion": occasion,
        "results": results,
        "note": ("記事量は記録開始以降しか比較できません。"
                 "混雑度の分析は基準点が溜まってから有効になります。"),
    }


# ---------------------------------------------------------------------------
# 混雑度（S7で本格運用。ここでは基本形だけ用意し、基準が無ければ黙る）
# ---------------------------------------------------------------------------


def crowding(symbol: Optional[str], name: Optional[str] = None,
             base_dir: str = DEFAULT_STORE_DIR) -> dict:
    """テーゼ記録時点に対する現在の物語量の倍率。

    基準（occasion='thesis'）が無ければ最古のスナップショットで代用し、
    その旨を `baseline_kind` に明示する。**基準が無いのに数字を作らない。**
    """
    all_snaps = load_snapshots(symbol, name, base_dir)
    numeric = [s for s in all_snaps if isinstance(s.get("articles"), (int, float))]

    # 異なるソースの記事量を比にしない（母集団が違うので比が無意味になる）。
    # 最新スナップショットと同じソースの系列だけで比較する。
    latest_all = numeric[-1] if numeric else None
    src = latest_all.get("volume_source") if latest_all else None
    snaps = [s for s in numeric if s.get("volume_source") == src] if src else numeric

    if len(snaps) < 2:
        return {"available": False,
                "reason": ("同一ソースの基準スナップショットがまだありません（記録蓄積中）"
                           if numeric else
                           "スナップショットがまだありません（記録蓄積中）"),
                "samples": len(snaps),
                "volume_source": src,
                "analyst_from": _first_coverage(all_snaps),
                "analyst_to": _last_coverage(all_snaps)}

    baselines = [s for s in snaps if s.get("occasion") == "thesis"]
    baseline = baselines[0] if baselines else snaps[0]
    latest = snaps[-1]

    b_art = baseline.get("articles") or 0.0
    l_art = latest.get("articles") or 0.0
    ratio = (l_art / b_art) if b_art else None

    b_cov = baseline.get("analyst_count")
    l_cov = latest.get("analyst_count")

    return {
        "available": ratio is not None,
        "baseline_kind": "thesis" if baselines else "oldest_snapshot",
        "baseline_date": baseline.get("captured_date"),
        "baseline_articles": b_art,
        "latest_date": latest.get("captured_date"),
        "latest_articles": l_art,
        "ratio": round(ratio, 2) if ratio else None,
        "volume_source": src,
        "analyst_from": b_cov,
        "analyst_to": l_cov,
        "tone_from": baseline.get("avg_tone"),
        "tone_to": latest.get("avg_tone"),
        "samples": len(snaps),
        "caveat": ("記事量の急増は混雑（コンセンサス化）と混乱（不祥事）の両方で起きます。"
                   "トーンの変化と併せて読んでください。"
                   "混雑度は単独で売り推奨を作りません — テーゼ書き直しの議題です。"),
    }


def _first_coverage(snaps: list[dict]) -> Optional[float]:
    for s in snaps:
        if isinstance(s.get("analyst_count"), (int, float)):
            return s["analyst_count"]
    return None


def _last_coverage(snaps: list[dict]) -> Optional[float]:
    for s in reversed(snaps):
        if isinstance(s.get("analyst_count"), (int, float)):
            return s["analyst_count"]
    return None
