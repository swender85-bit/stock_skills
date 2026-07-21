"""ポートフォリオ推移予測 — 短期/中期/長期 (週次レポート用).

**これは予言ではない。** ボラティリティから導いたレンジであり、
「この範囲に収まる確率が概ね◯%」という統計的な幅を示すもの。
点推定を1つ出すと予言に見えるので、必ず下限・中央・上限の3点で返す。

レバレッジETFはボラティリティ・ドラッグ(日次リバランスによる減衰)を持つため、
原資産の期待リターンを単純に倍にしてはいけない。ここでは減衰項を明示的に引く。
"""

from __future__ import annotations

import math
from typing import Optional


#: 予測ホライズン(営業日)
HORIZONS = {
    "short": ("短期", 21),     # 約1ヶ月
    "mid": ("中期", 126),      # 約6ヶ月
    "long": ("long", 252 * 3),  # 約3年
}

HORIZON_LABELS = {
    "short": "短期(約1ヶ月)",
    "mid": "中期(約6ヶ月)",
    "long": "長期(約3年)",
}

#: レンジの幅(標準偏差の倍数)。1.28σ ≒ 片側10%
_SIGMA = 1.2816


def volatility_drag(annual_vol_pct: float, leverage: int = 1) -> float:
    """レバレッジETFのボラティリティ・ドラッグ(年率%)。

    日次リバランス型の L 倍ETFは、原資産のボラティリティ σ に対して
    概ね ``L(L-1)σ²/2`` の減衰を受ける。σ=40%, L=3 なら年率約 -48%。
    3xを「3倍儲かる」と扱うと予測が壊れるので明示的に引く。
    """
    if leverage <= 1:
        return 0.0
    sigma = annual_vol_pct / 100.0
    return leverage * (leverage - 1) * (sigma ** 2) / 2.0 * 100.0


def project_value(
    current_value: float,
    annual_return_pct: float,
    annual_vol_pct: float,
    days: int,
    leverage: int = 1,
) -> dict:
    """幾何ブラウン運動を仮定した価値のレンジ予測。

    **``annual_return_pct`` / ``annual_vol_pct`` は「原資産」の前提を渡すこと。**
    L倍ETFの挙動はここから導出する:

        実効ボラティリティ  σ_L = L · σ_u
        対数ドリフト        L · μ_u − σ_L² / 2

    第2項が対数正規の中央値補正で、``L(L-1)σ_u²/2`` のドラッグはこの式に
    すでに織り込まれている(``L·(μ_u − σ_u²/2)`` との差分がちょうどドラッグ)。
    ドラッグを別途引くと二重計上になり、中央値が不当に沈む。
    ``drag_pct`` は表示用に再掲するだけで、計算には足し引きしない。

    Returns
    -------
    dict
        {"low", "mid", "high", "horizon_days", "drag_pct", "effective_vol_pct"}
    """
    lev = max(int(leverage or 1), 1)
    sigma_u = annual_vol_pct / 100.0
    sigma_eff = lev * sigma_u
    drag = volatility_drag(annual_vol_pct, lev)

    if current_value <= 0 or days <= 0:
        return {
            "low": current_value, "mid": current_value, "high": current_value,
            "horizon_days": days, "drag_pct": drag,
            "effective_vol_pct": sigma_eff * 100.0,
        }

    years = days / 252.0
    mu_u = annual_return_pct / 100.0

    # 対数ドリフト: L倍の期待リターンから、実効ボラティリティ由来の中央値補正を引く
    drift = (lev * mu_u - sigma_eff ** 2 / 2.0) * years
    spread = sigma_eff * math.sqrt(years) * _SIGMA

    return {
        "low": current_value * math.exp(drift - spread),
        "mid": current_value * math.exp(drift),
        "high": current_value * math.exp(drift + spread),
        "horizon_days": days,
        "drag_pct": drag,
        "effective_vol_pct": sigma_eff * 100.0,
    }


def project_portfolio(
    positions: list[dict],
    cash_value: float = 0.0,
    monthly_contribution: float = 0.0,
) -> dict:
    """PF全体の短期/中期/長期の推移予測。

    Parameters
    ----------
    positions : list of dict
        各要素は ``value``(評価額) / ``annual_return_pct`` / ``annual_vol_pct`` /
        ``leverage`` を持つ。
    cash_value : float
        現金(予測上は変動しないものとして扱う)。
    monthly_contribution : float
        毎月の積立額。ホライズンに応じて元本に加算する。

    Returns
    -------
    dict
        horizon key -> {"label", "low", "mid", "high", "current", "change_pct", ...}
    """
    invested = sum(float(p.get("value") or 0.0) for p in positions)
    current_total = invested + cash_value

    out: dict = {"current_total": current_total, "invested": invested, "cash": cash_value}

    for key, (_, days) in HORIZONS.items():
        low = mid = high = 0.0
        for p in positions:
            value = float(p.get("value") or 0.0)
            if value <= 0:
                continue
            proj = project_value(
                value,
                float(p.get("annual_return_pct") or 0.0),
                float(p.get("annual_vol_pct") or 0.0),
                days,
                int(p.get("leverage") or 1),
            )
            low += proj["low"]
            mid += proj["mid"]
            high += proj["high"]

        contributions = monthly_contribution * (days / 21.0)
        low += cash_value + contributions
        mid += cash_value + contributions
        high += cash_value + contributions

        out[key] = {
            "label": HORIZON_LABELS[key],
            "horizon_days": days,
            "low": low,
            "mid": mid,
            "high": high,
            "current": current_total,
            "change_pct": ((mid - current_total) / current_total * 100.0)
            if current_total > 0 else 0.0,
            "contributions": contributions,
        }

    return out


def drawdown_scenario(
    positions: list[dict], underlying_drop_pct: float, cash_value: float = 0.0
) -> dict:
    """原資産が指定%下落したときのPF評価額(レバレッジ倍率を反映).

    3xETFは原資産-20%で概ね-60%になる。損切りしない運用方針では、
    この数字を平時に見ておくことが政策(案A)の前提になる。
    """
    total_after = cash_value
    detail: list[dict] = []
    for p in positions:
        value = float(p.get("value") or 0.0)
        lev = int(p.get("leverage") or 1)
        after = value * (1 + underlying_drop_pct / 100.0 * lev)
        after = max(after, 0.0)
        total_after += after
        detail.append(
            {
                "name": p.get("name", ""),
                "before": value,
                "after": after,
                "change_pct": ((after - value) / value * 100.0) if value else 0.0,
            }
        )

    before_total = sum(float(p.get("value") or 0.0) for p in positions) + cash_value
    return {
        "underlying_drop_pct": underlying_drop_pct,
        "before": before_total,
        "after": total_after,
        "change_pct": ((total_after - before_total) / before_total * 100.0)
        if before_total > 0 else 0.0,
        "positions": detail,
    }
