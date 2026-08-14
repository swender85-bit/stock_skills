"""監視計画の自動導出 -- 「今の保有」から、見るべきものを毎回決める.

## 名指しする問題

何を取りに行くかが**手書きの静的設定**に固定されていた。

    config/competitors.yaml の peers:
      SOXL, TECL, TQQQ, QCOM, MDT, 2802.T, 9843.T, **2737.T**
                                                    ↑ 8/7 に売却済みなのに残っている

保有が変わっても設定は変わらない。**売った銘柄の競合を追い、
新しく買った銘柄の競合は追わない**という状態が静かに続く。

さらに、実効エクスポージャー表に **INTC** が並んでいたことで
「持っていない銘柄がポートフォリオ扱いされている」と読まれた。
データ上は `direct_pct: 0.0 / via_etf_pct: 6.34`（ETF経由のみ）で正しいが、
**保有と ETF経由の曝露を同じ見た目で並べたのが誤り**だった。

## この層がやること

**毎回、いまの保有だけを入力にして、見るべきものを導出する。**

1. **保有の分類** — 直接保有 / ETF経由のみ / 非保有（競合）を明示的に分ける
2. **指数** — 保有の市場・セクターから必要な指数を決める
   （日本株があれば日経、半導体があればSOX、レバレッジがあれば長期金利…）
3. **競合** — 設定にあるものは保有分だけ採用し、無い銘柄はセクターから自動導出
4. **陳腐化検出** — 設定に残っている非保有銘柄を名指しする

**保有が変われば、翌週の監視対象は自動で変わる。** 設定の更新漏れで
古い銘柄を追い続けることがなくなる。
"""

from __future__ import annotations

from typing import Any, Optional

#: 保有の性質 → 見るべき指数
#: 「日本株を持っているのに日経を見ていない」を設定漏れで起こさない。
MARKET_INDICES: dict[str, list[tuple[str, str]]] = {
    "JP": [("^N225", "日経225"), ("^TPX", "TOPIX")],
    "US": [("^GSPC", "S&P500"), ("^NDX", "Nasdaq100")],
}

SECTOR_INDICES: dict[str, list[tuple[str, str]]] = {
    "semiconductor": [("^SOX", "SOX半導体")],
    "technology": [("^NDX", "Nasdaq100")],
}

#: 常に見る（PF の性質に関わらず地合いを決める）
BASE_INDICES: list[tuple[str, str]] = [("^VIX", "VIX")]

#: レバレッジ商品を持っているときに追加で見るもの。
#: レバETFは実質ロング・デュレーション資産なので長期金利が効く。
LEVERAGE_WATCH: list[tuple[str, str]] = [
    ("^TYX", "米30年債利回り"),
    ("^TNX", "米10年債利回り"),
]

#: 外貨建て資産があるときに見るもの
FX_WATCH: list[tuple[str, str]] = [("JPY=X", "ドル円")]

#: 半導体とみなすセクター/キーワード
_SEMI_HINTS = ("semiconductor", "半導体", "soxl", "soxx", "sox")


def _market_of(symbol: Optional[str], name: Optional[str] = None) -> str:
    if str(symbol or "").upper().endswith(".T"):
        return "JP"
    if not symbol and name and any(
            c in str(name) for c in ("ｉ", "i", "e")) and "FANG" in str(name).upper():
        return "US"          # 円建て投信だが中身は米国
    return "US" if symbol else "JP"


def classify_holdings(holdings: list[dict],
                      lookthrough: Optional[dict] = None) -> dict:
    """保有・ETF経由・非保有を明示的に分ける。

    **INTC のように「持っていないが ETF 経由で曝露がある」銘柄を、
    保有と同じ見た目で並べない**ための土台。
    """
    direct: dict[str, dict] = {}
    for h in holdings or []:
        key = h.get("symbol") or f"name:{h.get('name')}"
        row = direct.setdefault(key, {
            "symbol": h.get("symbol"), "name": h.get("name"),
            "weight_pct": 0.0, "leverage": h.get("leverage") or 1,
            "market": _market_of(h.get("symbol"), h.get("name")),
        })
        try:
            row["weight_pct"] += float(h.get("weight_pct") or 0.0)
        except (TypeError, ValueError):
            pass

    direct_symbols = {r["symbol"] for r in direct.values() if r["symbol"]}

    via_only: list[dict] = []
    for r in (lookthrough or {}).get("effective") or []:
        sym = r.get("symbol")
        if not sym or sym in direct_symbols:
            continue
        via_only.append({
            "symbol": sym,
            "effective_pct": r.get("effective_pct"),
            "via": r.get("sources") or [],
            "holding_type": "etf_only",
            "label": "ETF経由のみ（直接保有なし）",
        })
    via_only.sort(key=lambda r: -(r.get("effective_pct") or 0))

    return {
        "direct": sorted(direct.values(), key=lambda r: -(r["weight_pct"] or 0)),
        "direct_symbols": sorted(direct_symbols),
        "etf_only": via_only,
        "note": (
            f"直接保有 {len(direct)}件 / ETF経由のみ {len(via_only)}件。"
            "**ETF経由のみの銘柄は保有ではありません。** "
            "曝露はあるが売買の対象ではなく、混ぜて並べると誤読されます。"
        ),
    }


def _has_leverage(holdings: list[dict]) -> bool:
    return any(float(h.get("leverage") or 1) > 1 for h in holdings or [])


def _has_foreign(holdings: list[dict]) -> bool:
    return any(_market_of(h.get("symbol"), h.get("name")) == "US"
               for h in holdings or [])


def _is_semi(row: dict) -> bool:
    blob = " ".join(str(row.get(k) or "") for k in ("symbol", "name", "sector")).lower()
    return any(h in blob for h in _SEMI_HINTS)


def derive_indices(holdings: list[dict],
                   lookthrough: Optional[dict] = None) -> list[dict]:
    """保有から見るべき指数を決める。**設定に書かない。**

    「日本株を持っているのに日経を見ていない」を設定漏れで起こさないため、
    保有の市場・性質から機械的に導く。
    """
    wanted: dict[str, dict] = {}

    def add(symbol: str, label: str, reason: str) -> None:
        row = wanted.setdefault(symbol, {"symbol": symbol, "label": label,
                                         "reasons": []})
        if reason not in row["reasons"]:
            row["reasons"].append(reason)

    for symbol, label in BASE_INDICES:
        add(symbol, label, "地合いの基礎指標")

    markets = {_market_of(h.get("symbol"), h.get("name")) for h in holdings or []}
    for market in markets:
        for symbol, label in MARKET_INDICES.get(market, []):
            add(symbol, label, f"{market} 市場の保有があるため")

    # 半導体は保有そのものと、ETF経由の中身の両方から判定する
    semi = any(_is_semi(h) for h in holdings or [])
    if not semi:
        for r in (lookthrough or {}).get("resolved_etfs") or []:
            if _is_semi(r) or "半導体" in str(r.get("underlying") or ""):
                semi = True
                break
    if semi:
        for symbol, label in SECTOR_INDICES["semiconductor"]:
            add(symbol, label, "半導体への曝露があるため")

    if _has_leverage(holdings):
        for symbol, label in LEVERAGE_WATCH:
            add(symbol, label,
                "レバレッジ商品は実質ロング・デュレーション資産で長期金利が効くため")

    if _has_foreign(holdings):
        for symbol, label in FX_WATCH:
            add(symbol, label, "外貨建て資産があるため")

    return list(wanted.values())


def derive_peers(holdings: list[dict], cfg: Optional[dict] = None) -> dict:
    """競合を決める。**保有していない銘柄の競合は追わない。**

    設定にある銘柄は保有分だけ採用し、設定に無い保有はセクターから自動導出する。
    設定に残っている非保有銘柄は `stale` として名指しする。
    """
    from src.core.research.competitors import _load_config, peers_for

    cfg = cfg or _load_config()
    configured = set((cfg.get("peers") or {}).keys())

    held = [h for h in holdings or [] if h.get("symbol")]
    held_symbols = {h["symbol"] for h in held}

    plan: dict[str, dict] = {}
    auto: list[str] = []
    for h in held:
        symbol = h["symbol"]
        sector = (h.get("fundamentals") or {}).get("sector") or h.get("sector")
        peers = peers_for(symbol, sector)
        if not peers:
            continue
        source = "config" if symbol in configured else "sector(自動導出)"
        if source.startswith("sector"):
            auto.append(symbol)
        plan[symbol] = {"peers": peers, "source": source}

    stale = sorted(configured - held_symbols)
    return {
        "plan": plan,
        "auto_derived": auto,
        "stale_config": stale,
        "note": (
            f"競合を追う保有 {len(plan)}件"
            + (f"（うち {len(auto)}件はセクターから自動導出）" if auto else "")
            + "。"
            + (f"⚠️ **設定に残っている非保有銘柄: {', '.join(stale)}**。"
               "売却済みの競合を追い続けないよう、設定から外すか無視されます。"
               if stale else "")
        ),
    }


def build_watch_plan(holdings: list[dict],
                     lookthrough: Optional[dict] = None) -> dict:
    """今の保有から、今週見るべきものを全部導出する。

    **保有が変われば監視対象も自動で変わる。** 設定の更新漏れで
    売却済み銘柄を追い続けることがなくなる。
    """
    classes = classify_holdings(holdings, lookthrough)
    indices = derive_indices(holdings, lookthrough)
    peers = derive_peers(holdings)

    constituents = [r["symbol"] for r in (lookthrough or {}).get("effective") or []
                    if (r.get("effective_pct") or 0) >= 1.0]

    return {
        "generated_from": "current_holdings",
        "holdings": classes,
        "indices": indices,
        "peers": peers,
        "constituents": constituents,
        "note": (
            f"監視計画は**今の保有 {len(classes['direct'])}件から自動導出**しました。"
            f" 指数 {len(indices)}件 / 競合を追う銘柄 {len(peers['plan'])}件"
            f" / ETF構成銘柄 {len(constituents)}件。"
            + (" " + peers["note"] if peers.get("stale_config") else "")
        ),
    }


def format_watch_plan(plan: Optional[dict]) -> str:
    if not plan:
        return ""
    lines = ["### 監視計画（今の保有から自動導出）", "", plan["note"], ""]

    etf_only = plan["holdings"]["etf_only"]
    if etf_only:
        lines += [
            "**⚠️ 以下は保有していません。ETF経由の曝露のみです。**", "",
            "| 銘柄 | 実効% | 経由 |", "|:---|---:|:---|",
        ]
        for r in etf_only[:10]:
            lines.append(f"| {r['symbol']} | {r['effective_pct']:.2f}% "
                         f"| {', '.join(r['via'])} |")
        lines += ["", "売買の対象ではありません。**保有と混ぜて読まないこと。**", ""]

    lines += ["**見る指数（保有の性質から導出）**", "",
              "| 指数 | 理由 |", "|:---|:---|"]
    for i in plan["indices"]:
        lines.append(f"| {i['label']} ({i['symbol']}) | {'／'.join(i['reasons'])} |")

    if plan["peers"]["plan"]:
        lines += ["", "**競合を追う銘柄**", "", "| 保有 | 競合 | 出所 |", "|:---|:---|:---|"]
        for sym, v in plan["peers"]["plan"].items():
            lines.append(f"| {sym} | {', '.join(v['peers'][:5])} | {v['source']} |")

    if plan["peers"].get("stale_config"):
        lines += ["", f"⚠️ 設定に残っている非保有銘柄: "
                      f"**{', '.join(plan['peers']['stale_config'])}**"
                      "（無視されます）"]
    return "\n".join(lines) + "\n"
