"""ETF ルックスルー — 中身の企業のイベントに、自分が何%曝されているか。

## なぜこれがあるか

2026-08-01 の週次レポートが自ら指摘した穴:

> SOXL・TECL・TQQQ の3銘柄について、翌週の日程が取得できなかった。
> ETF自体に決算はないが、**構成銘柄の決算・マクロイベントは3倍で効く。**
> PFの過半（56.9%）については翌週何が起きるかを把握していない状態でこの週に入る。

ETF に決算は無い。それは「予定なし」でも「取得失敗」でもなく、
**ETF には決算という事象が存在しない**という第三の状態である。
本当に見るべきは中身の企業の決算であり、レバレッジならその倍率で効く。

## レバレッジETFは proxy を経由する

レバレッジETFはスワップ・先物で複製するため、開示される保有が実体を反映しない。
実測: `TQQQ` の `top_holdings` は `IQMM` 単独で、ナスダック100構成銘柄が出てこない。
したがって `config/etf_lookthrough.yaml` の `proxies` で 1x ETF に読み替える。

## 実質エクスポージャー

    実質% = ETFのPF比率 × 構成銘柄のETF内ウェイト × レバレッジ

同じ銘柄が複数のETFに入っていれば合算する（SOXL と TECL の両方に NVDA がいる）。
**直接保有があればそれも足す。** これがルックスルーの本体。

## 使ってはいけない用途

構成銘柄を「個別に売買する対象」として扱わない。
目的は**保有ETF経由でどのイベントに曝されているかを知ること**の一点。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_CONFIG = "config/etf_lookthrough.yaml"
DEFAULT_CACHE = "data/cache/etf_holdings.json"

#: 構成が取れないETF。取れないことを「中身が無い」と混同しない。
UNRESOLVED = "unresolved"


def _num(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------


_config_cache: dict[str, dict] = {}


def load_config(path: str = DEFAULT_CONFIG) -> dict:
    if path in _config_cache:
        return _config_cache[path]
    try:
        import yaml

        p = Path(path)
        cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {} if p.exists() else {}
    except Exception:
        cfg = {}
    cfg.setdefault("proxies", {})
    cfg.setdefault("fund_proxies", {})
    cfg.setdefault("settings", {})
    cfg["settings"].setdefault("top_n", 10)
    cfg["settings"].setdefault("min_effective_pct", 0.5)
    cfg["settings"].setdefault("cache_hours", 168)
    _config_cache[path] = cfg
    return cfg


def reset_config_cache() -> None:
    _config_cache.clear()


def resolve_proxy(symbol: Optional[str], name: Optional[str] = None,
                  cfg: Optional[dict] = None) -> dict:
    """この保有の中身を見るために、どのティッカーを調べればよいか。

    レバレッジETFは 1x proxy に読み替える（スワップ複製で保有が実体を反映しないため）。
    投信は近似ETFに読み替え、**近似であることを必ず明示する**。
    """
    cfg = cfg or load_config()
    sym = str(symbol or "").strip().upper()

    entry = (cfg.get("proxies") or {}).get(sym)
    if entry:
        return {"lookup": str(entry.get("proxy") or sym).upper(),
                "leverage": float(entry.get("leverage") or 1.0),
                "underlying": entry.get("underlying"),
                "kind": "leveraged_etf", "approximate": False}

    if not sym and name:
        for key, e in (cfg.get("fund_proxies") or {}).items():
            if str(key).strip() and str(key).strip() in str(name):
                return {"lookup": str(e.get("proxy") or "").upper(),
                        "leverage": 1.0, "underlying": e.get("note"),
                        "kind": "fund_proxy", "approximate": True}
        return {"lookup": None, "leverage": 1.0, "underlying": None,
                "kind": "unmapped_fund", "approximate": True}

    return {"lookup": sym or None, "leverage": 1.0, "underlying": None,
            "kind": "direct", "approximate": False}


# ---------------------------------------------------------------------------
# 構成銘柄
# ---------------------------------------------------------------------------


def _load_cache(path: str = DEFAULT_CACHE) -> dict:
    try:
        p = Path(path)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return {}


def _save_cache(cache: dict, path: str = DEFAULT_CACHE) -> None:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cache, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    except Exception:
        pass


def fetch_holdings(ticker: str, *, cfg: Optional[dict] = None,
                   cache_path: str = DEFAULT_CACHE,
                   use_cache: bool = True) -> dict:
    """ETF の上位構成銘柄と内部ウェイトを返す。

    構成は日々変わらないので既定1週間キャッシュする。
    取れなければ `available=False`。**空を「中身が無い」と扱わない。**
    """
    cfg = cfg or load_config()
    settings = cfg.get("settings") or {}
    top_n = int(settings.get("top_n") or 10)
    ttl = float(settings.get("cache_hours") or 168) * 3600.0
    key = str(ticker or "").strip().upper()
    if not key:
        return {"ticker": ticker, "available": False, "holdings": [],
                "error": "ティッカーが空です"}

    cache = _load_cache(cache_path) if use_cache else {}
    hit = cache.get(key)
    if hit and (time.time() - float(hit.get("fetched_ts") or 0)) < ttl:
        return {**hit, "from_cache": True}

    try:
        import yfinance as yf

        data = yf.Ticker(key).funds_data.top_holdings
    except Exception as e:
        return {"ticker": key, "available": False, "holdings": [],
                "error": f"{type(e).__name__}: {str(e)[:120]}"}

    rows: list[dict] = []
    try:
        for sym, row in list(data.iterrows())[:top_n]:
            weight = None
            for col in ("Holding Percent", "holdingPercent", "percent"):
                try:
                    if col in row.index:
                        weight = _num(row[col])
                        break
                except Exception:
                    continue
            rows.append({"symbol": str(sym).upper(),
                         # yfinance は 0.07 形式（＝7%）で返す
                         "weight_pct": round(weight * 100.0, 3)
                         if weight is not None and weight <= 1.5
                         else (round(weight, 3) if weight is not None else None)})
    except Exception as e:
        return {"ticker": key, "available": False, "holdings": [],
                "error": f"構成の解釈に失敗: {type(e).__name__}"}

    if not rows:
        return {"ticker": key, "available": False, "holdings": [],
                "error": "構成銘柄が取得できませんでした（材料なしではなく取得不可）"}

    result = {
        "ticker": key, "available": True, "holdings": rows,
        "coverage_pct": round(sum(r["weight_pct"] for r in rows
                                  if r["weight_pct"] is not None), 2),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "fetched_ts": time.time(),
    }
    if use_cache:
        cache[key] = result
        _save_cache(cache, cache_path)
    return result


# ---------------------------------------------------------------------------
# 実質エクスポージャー
# ---------------------------------------------------------------------------


def build_lookthrough(holdings: list[dict], *,
                      cfg: Optional[dict] = None,
                      use_cache: bool = True) -> dict:
    """保有を「中身の企業」まで展開し、実質エクスポージャーを合算する。

        実質% = ETFのPF比率 × 構成銘柄の内部ウェイト × レバレッジ

    直接保有があればそれも足す。同じ銘柄が複数ETFに入っていれば合算する。
    """
    cfg = cfg or load_config()
    min_pct = float((cfg.get("settings") or {}).get("min_effective_pct") or 0.5)

    effective: dict[str, dict] = {}
    resolved: list[dict] = []
    unresolved: list[dict] = []

    for h in holdings or []:
        sym = h.get("symbol")
        name = h.get("name")
        weight = _num(h.get("weight_pct"))
        if weight is None:
            continue

        target = resolve_proxy(sym, name, cfg)
        lev = float(h.get("leverage") or target.get("leverage") or 1.0)

        if target["kind"] == "direct":
            # 個別株はそのまま自分自身への曝露
            key = str(sym or "").upper()
            if key:
                _add(effective, key, weight * lev, "direct", sym or key)
            continue

        lookup = target.get("lookup")
        if not lookup:
            unresolved.append({"symbol": sym, "name": name, "weight_pct": weight,
                               "reason": "中身を見るための proxy が未定義"})
            continue

        comp = fetch_holdings(lookup, cfg=cfg, use_cache=use_cache)
        if not comp.get("available"):
            unresolved.append({"symbol": sym, "name": name, "weight_pct": weight,
                               "lookup": lookup, "reason": comp.get("error")})
            continue

        for row in comp["holdings"]:
            inner_w = _num(row.get("weight_pct"))
            if inner_w is None:
                continue
            _add(effective, row["symbol"], weight * inner_w / 100.0 * lev,
                 "via_etf", sym or name)

        resolved.append({
            "symbol": sym, "name": name, "weight_pct": weight,
            "lookup": lookup, "leverage": lev,
            "underlying": target.get("underlying"),
            "approximate": target.get("approximate"),
            "coverage_pct": comp.get("coverage_pct"),
            "components": len(comp["holdings"]),
        })

    rows = sorted(effective.values(), key=lambda r: -r["effective_pct"])
    shown = [r for r in rows if r["effective_pct"] >= min_pct]

    return {
        "available": bool(resolved or effective),
        "effective": shown,
        "folded": [r for r in rows if r["effective_pct"] < min_pct],
        "resolved_etfs": resolved,
        "unresolved": unresolved,
        "min_effective_pct": min_pct,
        "note": _lookthrough_note(resolved, unresolved),
    }


def _add(store: dict, symbol: str, pct: float, kind: str, source: Any) -> None:
    row = store.setdefault(symbol, {
        "symbol": symbol, "effective_pct": 0.0,
        "direct_pct": 0.0, "via_etf_pct": 0.0, "sources": [],
    })
    row["effective_pct"] = round(row["effective_pct"] + pct, 3)
    if kind == "direct":
        row["direct_pct"] = round(row["direct_pct"] + pct, 3)
    else:
        row["via_etf_pct"] = round(row["via_etf_pct"] + pct, 3)
    label = str(source)
    if label not in row["sources"]:
        row["sources"].append(label)


def _lookthrough_note(resolved: list, unresolved: list) -> str:
    parts = []
    if resolved:
        approx = [r for r in resolved if r.get("approximate")]
        parts.append(f"{len(resolved)}件のETF/投信を中身まで展開しました。")
        if approx:
            names = ", ".join(str(a.get("name") or a.get("symbol")) for a in approx)
            parts.append(f"うち {names} は**近似**（正確な構成ではありません）。")
        parts.append("レバレッジは実質エクスポージャーに掛けてあります。")
    if unresolved:
        parts.append(f"{len(unresolved)}件は中身を展開できませんでした"
                     "（中身が無いのではなく、構成を取得できていません）。")
    return " ".join(parts) if parts else "展開対象がありません。"


# ---------------------------------------------------------------------------
# 前方イベントとの合流（提案4の穴を埋める）
# ---------------------------------------------------------------------------


def lookthrough_events(lookthrough: dict, *, as_of=None,
                       events_by_symbol: Optional[dict] = None,
                       min_effective_pct: Optional[float] = None) -> dict:
    """展開した構成銘柄の翌週イベントを、実質エクスポージャー付きで返す。

    ETF は「決算が無い」のであって「日程が不明」なのではない。
    見るべきは中身の企業の決算であり、レバレッジなら倍率で効く。
    """
    from src.core.risk.forward_events import (
        _parse,
        _safe_fetch_events,
        _jp_label,
        next_week_range,
    )

    rows = lookthrough.get("effective") or []
    threshold = (min_effective_pct if min_effective_pct is not None
                 else lookthrough.get("min_effective_pct") or 0.5)
    targets = [r for r in rows
               if r.get("via_etf_pct") and r["effective_pct"] >= threshold]
    if not targets:
        return {"available": False, "events": [],
                "reason": "ETF経由の構成銘柄が展開できていません"}

    start, end = next_week_range(as_of)
    symbols = [r["symbol"] for r in targets]
    events_by_symbol = (events_by_symbol if events_by_symbol is not None
                        else _safe_fetch_events(symbols))

    by_symbol = {r["symbol"]: r for r in targets}
    events: list[dict] = []
    unavailable: list[str] = []

    for sym in symbols:
        ev = events_by_symbol.get(sym) or {}
        if not ev.get("available"):
            unavailable.append(sym)
            continue
        for iso in ev.get("earnings_dates") or []:
            d = _parse(iso)
            if not d or not (start <= d <= end):
                continue
            row = by_symbol[sym]
            events.append({
                "kind": "lookthrough_earnings", "date": iso,
                "day_label": _jp_label(d), "symbol": sym,
                "effective_pct": row["effective_pct"],
                "via": row.get("sources"),
                "title": f"{sym} 決算（保有ETF経由・実質 {row['effective_pct']:.1f}%）",
                "source": ev.get("source"), "fetched_at": ev.get("fetched_at"),
            })

    events.sort(key=lambda e: (e["date"], -e["effective_pct"]))
    total = round(sum(e["effective_pct"] for e in events), 1)

    return {
        "available": True,
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "events": events,
        "unavailable_symbols": unavailable,
        "total_effective_pct": total,
        "message": (
            f"保有ETFの中身のうち、実質 {total:.1f}% 相当の企業が翌週に決算を通過します。"
            "レバレッジ分を含めた実効エクスポージャーです。" if events else
            "保有ETFの構成銘柄で翌週に決算を迎えるものはありません。"),
        "caveat": ("構成上位のみを見ています。これは個別に売買する対象のリストでは"
                   "なく、**自分が何に曝されているか**を知るためのものです。"),
    }
