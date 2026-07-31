"""限界寄与スクリーニングの配線 (土曜設計書 提案2-⑤)。

screen-stocks と stock-portfolio は今まで別々に動き、結果が結合されていなかった。
このモジュールが両者を**共通の言語（因子ベクトル）で会話させる**。

保有が無い/因子が取れない場合は静かに縮退し、従来どおり単独スコア順を返す。
**非破壊**: `--standalone` を付けた場合と、この層が縮退した場合の出力は
従来と完全に一致する。
"""

from __future__ import annotations

from typing import Any, Optional


def load_portfolio_holdings() -> list[dict]:
    """週次の保有定義から、因子計算に必要な最小の保有ビューを作る。

    価格取得を伴うので失敗し得る。失敗したら空を返す（限界評価は縮退する）。
    """
    try:
        from src.core.portfolio.weekly import build_report_data, load_holdings_config

        base = build_report_data(load_holdings_config())
    except Exception:
        return []

    total = base.get("total_jpy") or 0.0
    if not total:
        return []

    out: list[dict] = []
    for a in base.get("analyses") or []:
        sym = a.get("symbol")
        value = a.get("value_jpy")
        if not sym or not isinstance(value, (int, float)):
            continue
        out.append({
            "symbol": sym,
            "name": a.get("name"),
            "weight_pct": value / total * 100.0,
            "leverage": a.get("leverage"),
        })
    return out


def build_marginal_view(
    candidates: list[dict],
    holdings: Optional[list[dict]] = None,
    *,
    include_twins: bool = True,
    min_standalone: Optional[float] = None,
) -> dict:
    """候補群に限界スコアを付け、PF の因子偏りと併せて返す。

    Returns:
        {"available", "pf_exposure", "tilt", "ranked", "reason"}

    `available=False` のとき、呼び出し側は**従来の単独スコア出力に戻す**こと。
    """
    result: dict[str, Any] = {"available": False, "pf_exposure": None,
                              "tilt": [], "ranked": None, "reason": None}

    if not candidates:
        result["reason"] = "候補がありません"
        return result

    holdings = holdings if holdings is not None else load_portfolio_holdings()
    if not holdings:
        result["reason"] = ("保有データが取得できないため、限界寄与を評価できません"
                            "（単独スコアで表示します）")
        return result

    try:
        from src.core.exposure import (
            build_factor_returns,
            describe_tilt,
            estimate_many,
            portfolio_exposure,
        )
        from src.core.screening.marginal import (
            DEFAULT_MIN_STANDALONE,
            rank_candidates,
        )
    except Exception as e:
        result["reason"] = f"因子モジュールを読み込めません: {type(e).__name__}"
        return result

    try:
        factors = build_factor_returns()
        if not factors:
            result["reason"] = ("因子系列を取得できませんでした"
                                "（単独スコアで表示します）")
            return result

        held = sorted({h["symbol"] for h in holdings if h.get("symbol")})
        held_exposures = estimate_many(held)
        pf = portfolio_exposure(holdings, held_exposures)
        if not pf.get("available"):
            result["reason"] = pf.get("reason")
            result["pf_exposure"] = pf
            return result

        cand_symbols = [c.get("symbol") for c in candidates if c.get("symbol")]
        cand_exposures = estimate_many(cand_symbols)

        holdings_returns = _holdings_returns(held) if include_twins else None

        ranked = rank_candidates(
            candidates, pf, cand_exposures,
            holdings_returns=holdings_returns,
            min_standalone=(min_standalone if min_standalone is not None
                            else DEFAULT_MIN_STANDALONE))

        result.update({"available": True, "pf_exposure": pf,
                       "tilt": describe_tilt(pf), "ranked": ranked})
    except Exception as e:
        result["reason"] = f"限界評価に失敗しました: {type(e).__name__}: {e}"
    return result


def _holdings_returns(symbols: list[str]) -> dict[str, dict]:
    """因子双子の判定に使う保有の日次リターン。"""
    try:
        from src.core.exposure import fetch_returns
    except Exception:
        return {}
    out: dict[str, dict] = {}
    for s in symbols:
        series = fetch_returns(s)
        if series:
            out[s] = series
    return out


def render(view: dict, limit: int = 10) -> str:
    """限界寄与セクションを文字列にする。縮退時は短い注記だけ返す。"""
    if not view.get("available"):
        reason = view.get("reason")
        return (f"\n※ 限界寄与（保有考慮）評価は行われていません: {reason}\n"
                if reason else "")
    try:
        from src.output.marginal_formatter import format_marginal_section

        return format_marginal_section(view["pf_exposure"], view["ranked"],
                                       tilt_lines=view.get("tilt"), limit=limit)
    except Exception as e:
        return f"\n※ 限界寄与の整形に失敗しました: {type(e).__name__}\n"
