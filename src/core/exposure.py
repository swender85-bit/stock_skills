"""因子エクスポージャー — セクターラベル以外の分散軸 (土曜設計書 提案2)。

## 名指しする問題

現行の分散は**資産空間**の HHI（セクター・地域・通貨・規模）でしか測られていない。
そこには三つの不可視構造がある。

**(a) 因子双子（factor twin）** — 業種コードが違っても値動きが同一の組。
「半導体製造装置」と「精密機器」、「メガバンク」と「地銀」は、セクター分散上は
別だが金利感応度では同一である。**セクターHHIはこれを分散とみなす。**

**(b) 通貨の二重ロング** — 日本の投資家が米国株を持つと暗黙に USD ロングを持つ。
加えて日本の輸出企業を持つと、**円安で儲かる資産を二重に持っている**。
この投資家は「日米に分散した」つもりで、実際には**円安という単一シナリオに集中投資**
している。セクター・地域HHIはこれを全く検出しない。

**(c) ルックスルー未計算** — ETF の中身の個別株を間接保有している。
ETF比率が高いほど、表示されている分散は虚構になる。

## 設計方針

因子は外部提供のスマートベータではなく**自前で最小限を構築する**。
目的は因子モデルの高度化ではなく、**セクターラベル以外の分散軸を一つでも持つこと**。

因子は5個以内に固定し増やさない（設計書 提案2-⑧）。日次リターンの重回帰で足りる。

## 推定の不安定さを隠さない

因子推定は推定期間で大きく変わる。60/120/250日で並走させ、
符号が割れる銘柄には **`unstable=True`** を立てる。
安定して見せるために期間を1つに絞る、ということはしない。
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Optional

#: 自前で構築する最小因子集合。**5個以内に固定し、増やさない。**
#:
#: market      … 市場ベータ（S&P500）
#: usdjpy      … 円安で儲かるか（日本の個人投資家の最大の隠れ集中）
#: rates       … 金利感応度（デュレーション代理）
#: oil         … 原油感応度
#: semis       … 半導体シクリカリティ（このPFの主軸なので独立させる）
FACTORS: tuple[str, ...] = ("market", "usdjpy", "rates", "oil", "semis")

FACTOR_LABELS: dict[str, str] = {
    "market": "市場ベータ",
    "usdjpy": "USDJPY感応度",
    "rates": "金利感応度",
    "oil": "原油感応度",
    "semis": "半導体感応度",
}

#: 因子系列に使うティッカー。yahoo_client 経由で取得する。
FACTOR_TICKERS: dict[str, str] = {
    "market": "^GSPC",
    "usdjpy": "JPY=X",
    "rates": "^TNX",
    "oil": "CL=F",
    "semis": "^SOX",
}

#: 推定期間（営業日）。1つに絞らない — 不安定さを見せるため並走させる。
ESTIMATION_WINDOWS: tuple[int, ...] = (60, 120, 250)

#: 最低これだけサンプルが無ければ推定しない
MIN_SAMPLES = 40

#: 期間間で符号が割れたら不安定と判定する
_SIGN_FLIP_UNSTABLE = True

#: 相関がこれを超えたら「因子双子」候補
TWIN_CORRELATION = 0.85

#: 決定係数がこれ未満なら、βの符号自体がノイズとみなす。
#: 個別銘柄固有の値動きが大きい銘柄（特に日本の中小型株）はここに落ちる。
LOW_R2 = 0.20


# ---------------------------------------------------------------------------
# リターン系列
# ---------------------------------------------------------------------------


def _closes(history: Any) -> list[tuple[str, float]]:
    """価格履歴を (日付, 終値) のリストに均す。"""
    if history is None:
        return []
    try:
        series = history["Close"].dropna()
    except Exception:
        return []
    out: list[tuple[str, float]] = []
    for idx, value in series.items():
        try:
            day = str(idx)[:10]
            out.append((day, float(value)))
        except Exception:
            continue
    return out


def daily_returns(history: Any) -> dict[str, float]:
    """日次リターン（日付 → 変化率）。"""
    closes = _closes(history)
    out: dict[str, float] = {}
    for (_, prev), (day, cur) in zip(closes, closes[1:]):
        if prev:
            out[day] = (cur - prev) / prev
    return out


def fetch_returns(symbol: str, period: str = "2y") -> dict[str, float]:
    """1銘柄の日次リターン。取得できなければ空 dict。"""
    try:
        from src.data import yahoo_client as yc

        return daily_returns(yc.get_price_history(symbol, period=period))
    except Exception:
        return {}


def weekly_factor_moves(days: int = 5,
                        tickers: Optional[dict] = None) -> dict[str, float]:
    """直近N営業日の因子の累積変化率（%）。

    模型監査（提案10）の予測に使う。実現リターンが**週次**なので、
    因子も週次で揃えないと予測と実現が別の単位になる。
    指数ウォッチの `percent_change` は**日次**なので流用してはいけない。

    取れなかった因子は含めない（0%として扱うと「動かなかった」と誤読される）。
    """
    tickers = tickers or FACTOR_TICKERS
    out: dict[str, float] = {}
    for name, ticker in tickers.items():
        series = fetch_returns(ticker, period="3mo")
        if not series:
            continue
        recent = [series[d] for d in sorted(series)[-days:]]
        if not recent:
            continue
        cumulative = 1.0
        for r in recent:
            cumulative *= (1.0 + r)
        out[name] = round((cumulative - 1.0) * 100.0, 4)
    return out


def build_factor_returns(period: str = "2y",
                         tickers: Optional[dict] = None) -> dict[str, dict]:
    """因子系列の日次リターン。取れなかった因子は**含めない**。

    取れない因子を 0 で埋めると「感応度なし」と誤読される。
    """
    tickers = tickers or FACTOR_TICKERS
    out: dict[str, dict[str, float]] = {}
    for name, ticker in tickers.items():
        series = fetch_returns(ticker, period=period)
        if len(series) >= MIN_SAMPLES:
            out[name] = series
    return out


# ---------------------------------------------------------------------------
# 回帰
# ---------------------------------------------------------------------------


#: 米国市場に対して1日ラグを掛ける因子。日本株の当日終値は**前日の米国市場**を
#: 織り込んで始まるため、同日で回帰すると感応度を大きく過小評価する。
_US_FACTORS = ("market", "rates", "oil", "semis")


def needs_lag(symbol: Optional[str]) -> bool:
    """この銘柄に米国因子のラグ調整が要るか（＝アジア市場の銘柄か）。"""
    s = str(symbol or "").upper()
    return any(s.endswith(sfx) for sfx in (".T", ".JP", ".HK", ".SS", ".SZ",
                                           ".KS", ".TW", ".SI"))


def _shift_one_day(series: dict[str, float], days: list[str]) -> dict[str, float]:
    """系列を1営業日ぶん後ろにずらす（前日の値を当日に割り当てる）。"""
    ordered = sorted(series)
    prev_by_day: dict[str, float] = {}
    for earlier, later in zip(ordered, ordered[1:]):
        prev_by_day[later] = series[earlier]
    return {d: prev_by_day[d] for d in days if d in prev_by_day}


def _align(target: dict, factors: dict[str, dict], window: int,
           lag_us: bool = False):
    """共通の日付で揃え、直近 window 日ぶんを返す。

    `lag_us=True` のとき、米国由来の因子を1日ずらす。
    日本株を米国因子に同日で当てると、時差のぶん感応度が消えてしまう。
    """
    if not target or not factors:
        return [], {}

    aligned: dict[str, dict[str, float]] = {}
    for name, series in factors.items():
        if lag_us and name in _US_FACTORS:
            aligned[name] = _shift_one_day(series, sorted(set(target)))
        else:
            aligned[name] = series

    days = set(target)
    for series in aligned.values():
        days &= set(series)
    ordered = sorted(days)[-window:]
    if len(ordered) < MIN_SAMPLES:
        return [], {}
    y = [target[d] for d in ordered]
    x = {name: [series[d] for d in ordered] for name, series in aligned.items()}
    return y, x


def _ols(y: list[float], x: dict[str, list[float]]) -> Optional[dict]:
    """重回帰（切片あり）。numpy が無い/特異行列なら None。"""
    try:
        import numpy as np
    except Exception:
        return None
    if not y or not x:
        return None

    names = list(x)
    try:
        design = np.column_stack([np.ones(len(y))] + [np.array(x[n]) for n in names])
        target = np.array(y)
        beta, *_ = np.linalg.lstsq(design, target, rcond=None)
    except Exception:
        return None

    fitted = design @ beta
    resid = target - fitted
    ss_res = float(np.sum(resid ** 2))
    ss_tot = float(np.sum((target - target.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot else None

    return {
        "alpha": float(beta[0]),
        "betas": {n: float(b) for n, b in zip(names, beta[1:])},
        "r2": round(r2, 3) if r2 is not None else None,
        "samples": len(y),
    }


def estimate_exposure(symbol: str,
                      factor_returns: Optional[dict] = None,
                      target_returns: Optional[dict] = None) -> dict:
    """1銘柄の因子エクスポージャーを複数期間で推定する。

    Returns:
        {"symbol", "available", "betas", "windows", "unstable", "r2", "note"}

    `unstable=True` は「この銘柄の因子は信用しないでほしい」という表示であり、
    **推定を隠すのではなく不安定であることを開示する**。
    """
    factors = factor_returns if factor_returns is not None else build_factor_returns()
    target = target_returns if target_returns is not None else fetch_returns(symbol)

    if not target:
        return {"symbol": symbol, "available": False, "betas": {},
                "reason": "価格履歴を取得できませんでした"}
    if not factors:
        return {"symbol": symbol, "available": False, "betas": {},
                "reason": "因子系列を取得できませんでした"}

    lag_us = needs_lag(symbol)
    windows: dict[str, dict] = {}
    for w in ESTIMATION_WINDOWS:
        y, x = _align(target, factors, w, lag_us=lag_us)
        fit = _ols(y, x) if y else None
        if fit:
            windows[f"{w}d"] = fit

    if not windows:
        return {"symbol": symbol, "available": False, "betas": {},
                "reason": f"共通サンプルが {MIN_SAMPLES}日に満たず推定できません"}

    # 代表値は中庸の期間（120日）を優先し、無ければ最長を使う
    primary = windows.get("120d") or windows[sorted(windows)[-1]]
    unstable, reasons, unstable_factors = _stability(windows)

    # 説明力が低いと、βの符号自体がノイズになる。数字が出たことを
    # 「推定できた」と誤読させないため、低R2も不安定側に倒す。
    # この場合は**全因子**が信用できない。
    r2 = primary.get("r2")
    low_r2 = isinstance(r2, (int, float)) and r2 < LOW_R2
    if low_r2:
        unstable = True
        reasons.append(f"説明力が低い（R²={r2:.2f} < {LOW_R2}）")
        unstable_factors = sorted(set(unstable_factors) | set(primary["betas"]))

    return {
        "symbol": symbol,
        "available": True,
        "betas": primary["betas"],
        "alpha": primary["alpha"],
        "r2": primary["r2"],
        "samples": primary["samples"],
        "windows": {k: v["betas"] for k, v in windows.items()},
        "unstable": unstable,
        "instability_reasons": reasons,
        # 因子単位の信用度。補完評価はこれを見て、不安定な因子だけを割り引く。
        "unstable_factors": unstable_factors,
        "low_r2": low_r2,
        "us_factors_lagged": lag_us,
        "estimated_at": datetime.now(timezone.utc).isoformat(),
        "note": ("因子推定は期間で大きく変わります。"
                 + ("この銘柄は期間によって符号が反転する因子があり、"
                    "エクスポージャーを信用しないでください。" if unstable else
                    "60/120/250日で符号は一致しています。")
                 + ("（アジア市場の銘柄のため、米国因子は1日ラグで当てています）"
                    if lag_us else "")),
    }


def _stability(windows: dict[str, dict]) -> tuple[bool, list[str], list[str]]:
    """期間間で符号が割れる因子を探す。

    **因子単位で返す**のが肝心。「原油の符号が反転した」を理由に
    半導体感応度まで信用しない、という粗い扱いをすると補完評価が全部死ぬ。

    Returns:
        (全体が不安定か, 理由, 不安定な因子名のリスト)
    """
    if len(windows) < 2:
        return False, [], []
    reasons: list[str] = []
    flipped: list[str] = []
    names = set()
    for w in windows.values():
        names |= set(w["betas"])
    for name in sorted(names):
        signs = {(1 if w["betas"].get(name, 0.0) > 0 else
                  -1 if w["betas"].get(name, 0.0) < 0 else 0)
                 for w in windows.values() if name in w["betas"]}
        signs.discard(0)
        if _SIGN_FLIP_UNSTABLE and len(signs) > 1:
            flipped.append(name)
            reasons.append(f"{FACTOR_LABELS.get(name, name)}の符号が期間で反転")
    return bool(reasons), reasons, flipped


def estimate_many(symbols: list[str], period: str = "2y") -> dict[str, dict]:
    """複数銘柄。因子系列は1回だけ取得して使い回す。"""
    factors = build_factor_returns(period=period)
    out: dict[str, dict] = {}
    for s in symbols or []:
        if not s:
            continue
        try:
            out[s] = estimate_exposure(s, factor_returns=factors)
        except Exception as e:
            out[s] = {"symbol": s, "available": False, "betas": {},
                      "reason": f"{type(e).__name__}"}
    return out


# ---------------------------------------------------------------------------
# ポートフォリオ集約
# ---------------------------------------------------------------------------


def portfolio_exposure(holdings: list[dict],
                       exposures: dict[str, dict]) -> dict:
    """保有の評価額加重で PF 全体の因子エクスポージャーを出す。

    レバレッジETFは**倍率を掛ける**。3xを1xとして扱うと実効エクスポージャーが
    3分の1に見え、リスクを大幅に過小評価する。
    """
    weighted: dict[str, float] = {}
    total_weight = 0.0
    covered: list[str] = []
    missing: list[str] = []

    for h in holdings or []:
        sym = h.get("symbol")
        w = h.get("weight_pct")
        if not sym or not isinstance(w, (int, float)):
            if sym:
                missing.append(sym)
            continue
        e = exposures.get(sym) or {}
        if not e.get("available"):
            missing.append(sym)
            continue
        lev = h.get("leverage")
        lev = float(lev) if isinstance(lev, (int, float)) and lev else 1.0
        effective = float(w) * lev
        total_weight += effective
        covered.append(sym)
        for name, beta in (e.get("betas") or {}).items():
            weighted[name] = weighted.get(name, 0.0) + effective * float(beta)

    if not total_weight:
        return {"available": False, "betas": {}, "covered": [],
                "missing": missing,
                "reason": "因子を推定できた保有がありません"}

    betas = {k: round(v / total_weight, 3) for k, v in weighted.items()}
    coverage = len(covered) / (len(covered) + len(missing)) * 100 if (
        covered or missing) else None

    return {
        "available": True,
        "betas": betas,
        "covered": covered,
        "missing": missing,
        "coverage_pct": round(coverage, 1) if coverage is not None else None,
        "effective_weight_pct": round(total_weight, 1),
        "note": ("レバレッジETFは倍率を掛けた実効エクスポージャーです。"
                 + (f"因子を推定できなかった保有が{len(missing)}件あり、"
                    "その分はこの数字に含まれていません。" if missing else "")),
    }


def describe_tilt(pf_exposure: dict) -> list[str]:
    """PF の因子偏りを日本語で説明する。

    特に**通貨の二重ロング**は日本の個人投資家の最大の隠れ集中であり、
    セクター・地域HHIでは全く検出できない。
    """
    if not pf_exposure.get("available"):
        return []
    betas = pf_exposure.get("betas") or {}
    out: list[str] = []

    usdjpy = betas.get("usdjpy")
    if isinstance(usdjpy, (int, float)) and abs(usdjpy) >= 0.3:
        if usdjpy > 0:
            out.append(
                f"USDJPY感応度 {usdjpy:+.2f} — **円安で儲かる方向に傾斜している**。"
                "米国株と日本の輸出企業を同時に持つと、地域は分散していても"
                "為替は分散していない。円高シナリオで大半が同時に不利になる。")
        else:
            out.append(f"USDJPY感応度 {usdjpy:+.2f} — 円高で有利な方向に傾斜している。")

    rates = betas.get("rates")
    if isinstance(rates, (int, float)) and abs(rates) >= 0.2:
        direction = "上昇に弱い" if rates < 0 else "上昇に強い"
        out.append(f"金利感応度 {rates:+.2f} — 金利{direction}構成。")

    semis = betas.get("semis")
    if isinstance(semis, (int, float)) and semis >= 1.0:
        out.append(f"半導体感応度 {semis:+.2f} — 半導体シクリカルにほぼ全面的に連動する。")

    market = betas.get("market")
    if isinstance(market, (int, float)) and market >= 1.5:
        out.append(f"市場ベータ {market:+.2f} — 指数の動きを増幅する構成。")

    return out


# ---------------------------------------------------------------------------
# 因子双子
# ---------------------------------------------------------------------------


def correlation(a: dict[str, float], b: dict[str, float]) -> Optional[float]:
    """共通日付での相関係数。サンプル不足なら None。"""
    days = sorted(set(a) & set(b))
    if len(days) < MIN_SAMPLES:
        return None
    xs = [a[d] for d in days]
    ys = [b[d] for d in days]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    vy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if not vx or not vy:
        return None
    return cov / (vx * vy)


def find_factor_twins(candidate: str,
                      holdings_returns: dict[str, dict],
                      candidate_returns: Optional[dict] = None,
                      threshold: float = TWIN_CORRELATION) -> list[dict]:
    """候補が既存保有の「因子双子」でないかを調べる。

    セクターが違っても値動きが同一なら、それは**実質的な買い増し**であり
    分散は改善しない。
    """
    target = (candidate_returns if candidate_returns is not None
              else fetch_returns(candidate))
    if not target:
        return []
    out: list[dict] = []
    for sym, series in (holdings_returns or {}).items():
        if sym == candidate:
            continue
        corr = correlation(target, series)
        if corr is None:
            continue
        if corr >= threshold:
            out.append({
                "symbol": sym, "correlation": round(corr, 3),
                "message": (f"保有中の {sym} と相関 {corr:.2f}。"
                            "実質的な買い増しであり、分散は改善しません。"),
            })
    out.sort(key=lambda r: -r["correlation"])
    return out


def stress_correlation(a: dict[str, float], b: dict[str, float],
                       drawdown_days: Optional[list[str]] = None) -> Optional[float]:
    """ストレス時の相関。

    設計書 提案2-⑧: 相関は暴落時に1へ収束するため平時の相関は過信できない。
    平時とストレス時の二本立てで表示するための片割れ。
    """
    if not drawdown_days:
        return None
    sub_a = {d: v for d, v in a.items() if d in set(drawdown_days)}
    sub_b = {d: v for d, v in b.items() if d in set(drawdown_days)}
    return correlation(sub_a, sub_b)


def worst_days(market_returns: dict[str, float], pct: float = 10.0) -> list[str]:
    """市場が最も悪かった日（下位N%）。ストレス時相関の対象日。"""
    if not market_returns:
        return []
    ordered = sorted(market_returns.items(), key=lambda kv: kv[1])
    n = max(MIN_SAMPLES // 4, int(len(ordered) * pct / 100))
    return [d for d, _ in ordered[:n]]
