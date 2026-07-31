"""限界寄与スクリーニング — 目的関数の反転 (土曜設計書 提案2)。

## 名指しする問題

現行の screen-stocks は銘柄を**単独で**採点する。一方 stock-portfolio は保有を分析する。
この二つは別々に動き、結果が結合されていない。

しかし保有者にとっての正しい問いは「この銘柄は良いか」ではない。

> **「この銘柄は、私が既に持っているものに何を足すか」**

両者は一致しない:

- 90点の優良割安株が、既存保有と同一の景気循環・金利感応度・為替方向を持つなら、
  **追加してもポートフォリオは改善しない**（むしろ集中が進む）。
- 65点の凡庸な銘柄が、既存保有に完全に欠けている因子を持つなら、
  **90点の銘柄より価値がある**。

## 設計原理

    限界スコア = 単独スコア × 補完係数

補完係数は「その銘柄が、現ポートフォリオに欠けている因子空間の方向をどれだけ埋めるか」
で決まる。**同じ銘柄でも、保有者が違えばスコアが違う** —— これが出力の型を変える。

## 暴走を止める三つの歯止め（設計書 提案2-⑧）

1. **単独スコアに下限**（既定60点）。分散のためだけに低品質銘柄を買わせない。
2. **因子推定が不安定な銘柄は補完係数を1に寄せる**。信用できない推定で順位を動かさない。
3. **相関は平時とストレス時の二本立て**。平時相関0.11でも暴落時0.68なら分散していない。
"""

from __future__ import annotations

from typing import Any, Optional

#: 単独スコアがこれ未満なら、補完係数が高くても推奨しない。
#: 「分散のための分散」で低品質銘柄を買う事故を防ぐ。
DEFAULT_MIN_STANDALONE = 60.0

#: 補完係数の上下限。これを超えて順位を動かさない（因子推定の誤差が支配しないよう）。
COMPLEMENT_MIN = 0.30
COMPLEMENT_MAX = 1.60

#: 因子双子とみなす相関
TWIN_CORRELATION = 0.85

#: 因子の偏りをこの絶対値で「強い傾斜」とみなす
TILT_THRESHOLD = 0.30


def _num(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


# ---------------------------------------------------------------------------
# 補完係数
# ---------------------------------------------------------------------------


#: 不安定と判定された因子の寄与をどれだけ割り引くか
UNSTABLE_FACTOR_WEIGHT = 0.35


def complement_factor(candidate_betas: dict, pf_betas: dict,
                      *, tilt_threshold: float = TILT_THRESHOLD,
                      unstable_factors: Optional[list] = None) -> dict:
    """候補が PF の因子偏りをどれだけ打ち消すかを 0.3〜1.6 の係数で返す。

    PF が強く傾斜している因子について、候補が**逆符号**なら加点、
    **同符号でさらに強める**なら減点する。傾斜していない因子は無視する
    （偏っていない方向を埋めても分散は改善しない）。

    `unstable_factors` に入っている因子は寄与を割り引く。
    **因子単位で割り引く**のが肝心 — 「原油の符号が反転した」ことを理由に
    半導体感応度の評価まで捨てると、補完評価そのものが機能しなくなる。
    """
    unstable = set(unstable_factors or [])
    if not pf_betas:
        return {"factor": 1.0, "available": False,
                "reason": "PFの因子エクスポージャーが無いため補完を評価できません",
                "contributions": []}
    if not candidate_betas:
        return {"factor": 1.0, "available": False,
                "reason": "候補の因子エクスポージャーが無いため補完を評価できません",
                "contributions": []}

    contributions: list[dict] = []
    score = 0.0
    tilted = 0

    for name, pf_beta in pf_betas.items():
        pfb = _num(pf_beta)
        if pfb is None or abs(pfb) < tilt_threshold:
            continue
        cand = _num(candidate_betas.get(name))
        if cand is None:
            continue
        tilted += 1
        # PFの傾斜を打ち消す方向なら正、強める方向なら負。
        # 大きさは「PFの傾斜の強さ」で重み付けする。
        direction = -1.0 if (pfb > 0) == (cand > 0) else 1.0
        magnitude = min(abs(cand), 2.0) / 2.0
        weight = min(abs(pfb), 2.0) / 2.0
        confidence = UNSTABLE_FACTOR_WEIGHT if name in unstable else 1.0
        delta = direction * magnitude * weight * confidence
        score += delta
        contributions.append({
            "factor": name, "pf_beta": round(pfb, 2),
            "candidate_beta": round(cand, 2),
            "effect": "補完" if direction > 0 else "増幅",
            "delta": round(delta, 3),
            "unstable": name in unstable,
        })

    if not tilted:
        return {"factor": 1.0, "available": True, "contributions": [],
                "reason": "PFに強い因子傾斜がありません（補完の余地が測れません）"}

    factor = 1.0 + score / tilted * 0.6
    factor = max(COMPLEMENT_MIN, min(COMPLEMENT_MAX, factor))
    return {"factor": round(factor, 3), "available": True,
            "contributions": sorted(contributions,
                                    key=lambda c: -abs(c["delta"])),
            "reason": None}


# ---------------------------------------------------------------------------
# 限界スコア
# ---------------------------------------------------------------------------


def marginal_score(
    candidate: dict,
    pf_exposure: dict,
    candidate_exposure: Optional[dict] = None,
    *,
    twins: Optional[list[dict]] = None,
    min_standalone: float = DEFAULT_MIN_STANDALONE,
    stress_correlations: Optional[dict] = None,
) -> dict:
    """候補1件の限界スコアを計算する。

    Args:
        candidate: スクリーニング結果の1行（`value_score` / `symbol` を使う）
        pf_exposure: `exposure.portfolio_exposure()` の戻り
        candidate_exposure: `exposure.estimate_exposure()` の戻り
        twins: `exposure.find_factor_twins()` の戻り

    Returns:
        単独スコア・補完係数・限界スコア・警告。
        **補完が評価できない場合は係数1.0（＝単独スコアのまま）** にする。
        推定できないことを理由に順位を動かさない。
    """
    symbol = candidate.get("symbol")
    standalone = _num(candidate.get("value_score") or candidate.get("score"))

    if standalone is None:
        return {"symbol": symbol, "available": False,
                "standalone_score": None, "marginal_score": None,
                "reason": "単独スコアがありません"}

    warnings: list[str] = []
    cand_betas = (candidate_exposure or {}).get("betas") or {}
    unstable = bool((candidate_exposure or {}).get("unstable"))
    unstable_factors = (candidate_exposure or {}).get("unstable_factors") or []

    # 因子単位で割り引く。全体を一律に潰すと補完評価が機能しなくなる。
    comp = complement_factor(cand_betas, (pf_exposure or {}).get("betas") or {},
                             unstable_factors=unstable_factors)
    factor = comp["factor"]

    if (candidate_exposure or {}).get("low_r2"):
        warnings.append(
            "この銘柄は因子モデルの説明力が低く（R²が閾値未満）、"
            "全因子の推定を割り引いています。補完係数を信用しすぎないでください。")
    elif unstable_factors:
        labels = ", ".join(str(f) for f in unstable_factors[:3])
        warnings.append(f"因子 {labels} は期間で符号が反転するため寄与を割り引きました。")

    twin_hits = list(twins or [])
    if twin_hits:
        worst = twin_hits[0]
        # 因子双子は実質的な買い増し。補完係数を強制的に下げる。
        factor = min(factor, 0.45)
        warnings.append(
            f"保有中の {worst['symbol']} の因子双子（相関 {worst['correlation']:.2f}）。"
            "実質的な買い増しであり、分散は改善しません。")

    stress = _stress_note(stress_correlations)
    if stress:
        warnings.append(stress)

    marginal = round(standalone * factor, 1)
    below_floor = standalone < min_standalone

    return {
        "symbol": symbol,
        "name": candidate.get("name"),
        "available": True,
        "standalone_score": round(standalone, 1),
        "complement_factor": round(factor, 3),
        "marginal_score": marginal,
        "contributions": comp.get("contributions") or [],
        "twins": twin_hits,
        "unstable_exposure": unstable,
        "below_quality_floor": below_floor,
        "recommendable": not below_floor,
        "warnings": warnings,
        "note": (comp.get("reason") if not comp.get("available") else None),
        "floor_note": (
            f"単独スコア {standalone:.0f} が下限 {min_standalone:.0f} 未満のため、"
            "限界スコアが高くても推奨しません（分散のための分散を避けるため）。"
            if below_floor else None),
    }


def _stress_note(stress_correlations: Optional[dict]) -> Optional[str]:
    """平時とストレス時の相関が乖離している場合の警告。

    相関は暴落時に1へ収束する。平時相関だけを見て「分散した」と判断させない。
    """
    if not stress_correlations:
        return None
    worst = None
    for sym, pair in stress_correlations.items():
        calm, stress = pair.get("calm"), pair.get("stress")
        if not isinstance(calm, (int, float)) or not isinstance(stress, (int, float)):
            continue
        if stress - calm >= 0.25:
            if worst is None or stress > worst[2]:
                worst = (sym, calm, stress)
    if not worst:
        return None
    sym, calm, stress = worst
    return (f"{sym} との相関は平時 {calm:.2f} だがストレス時 {stress:.2f}。"
            "暴落時には分散効果が大きく失われます。")


def rank_candidates(
    candidates: list[dict],
    pf_exposure: dict,
    exposures: Optional[dict] = None,
    *,
    holdings_returns: Optional[dict] = None,
    min_standalone: float = DEFAULT_MIN_STANDALONE,
) -> dict:
    """候補群を限界スコアで並べ替える。

    因子が取れない環境では単独スコア順のまま返す（**壊れずに縮退する**）。
    """
    exposures = exposures or {}
    scored: list[dict] = []

    for c in candidates or []:
        sym = c.get("symbol")
        twins = None
        if holdings_returns and sym:
            try:
                from src.core.exposure import find_factor_twins

                twins = find_factor_twins(sym, holdings_returns)
            except Exception:
                twins = None
        row = marginal_score(c, pf_exposure, exposures.get(sym),
                             twins=twins, min_standalone=min_standalone)
        row["source_row"] = c
        scored.append(row)

    usable = [r for r in scored if r.get("available")]
    degraded = not (pf_exposure or {}).get("available")

    key = ((lambda r: -(r.get("standalone_score") or 0)) if degraded else
           (lambda r: (r.get("below_quality_floor", False),
                       -(r.get("marginal_score") or 0))))
    usable.sort(key=key)

    return {
        "ranked": usable,
        "unscored": [r for r in scored if not r.get("available")],
        "degraded": degraded,
        "sorted_by": "standalone" if degraded else "marginal",
        "note": ("因子エクスポージャーが取得できないため、単独スコア順で表示しています。"
                 if degraded else
                 "限界スコア順。**同じ銘柄でも、保有者が違えばスコアが違います。**"),
    }


# ---------------------------------------------------------------------------
# ルックスルー（ETF の中身）
# ---------------------------------------------------------------------------


def lookthrough_exposure(holdings: list[dict],
                         etf_holdings: Optional[dict] = None) -> dict:
    """ETF の中身を通した実質保有比率。

    ETF比率が高いほど、表示されている分散は虚構になる。
    構成情報が無ければ**推定しない**（`available=False`）。
    推定できないことを「分散している」と誤読させない。
    """
    if not etf_holdings:
        return {"available": False, "effective": {},
                "reason": ("ETFの構成情報が無いため、ルックスルー比率を計算できません。"
                           "表示上の分散が実態より大きい可能性があります。")}

    direct: dict[str, float] = {}
    via_etf: dict[str, float] = {}

    for h in holdings or []:
        sym, w = h.get("symbol"), _num(h.get("weight_pct"))
        if not sym or w is None:
            continue
        components = etf_holdings.get(sym)
        if not components:
            direct[sym] = direct.get(sym, 0.0) + w
            continue
        for inner, share in components.items():
            s = _num(share)
            if s is None:
                continue
            via_etf[inner] = via_etf.get(inner, 0.0) + w * s

    effective: dict[str, dict] = {}
    for sym in set(direct) | set(via_etf):
        d = round(direct.get(sym, 0.0), 2)
        i = round(via_etf.get(sym, 0.0), 2)
        effective[sym] = {"direct_pct": d, "via_etf_pct": i,
                          "effective_pct": round(d + i, 2)}

    hidden = {s: v for s, v in effective.items()
              if v["via_etf_pct"] >= 0.5 and v["direct_pct"] > 0}
    return {
        "available": True,
        "effective": effective,
        "hidden_amplification": hidden,
        "note": ("直接保有とETF経由の合計が実質エクスポージャーです。"
                 + (f"{len(hidden)}銘柄で、表示比率より実質比率が大きくなっています。"
                    if hidden else "")),
    }
