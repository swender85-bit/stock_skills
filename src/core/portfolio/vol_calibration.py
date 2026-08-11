"""予測前提ボラティリティの較正 — 前提と実測を**比較可能な形で**突き合わせる。

## 名指しする問題

2026-08-01 の週次レポートはこう書いた:

> トーメンデバイスの乖離（30% 想定 vs 実測 129.8%）は4倍以上で、
> この銘柄の予測寄与は事実上意味を持たない。

しかしこの「実測 129.8%」は `technicals.volatility_pct`、すなわち
**直近20日のリターン標準偏差を年率換算した値**である。一方の「想定 30%」は
数年スパンの構造的な前提。**両者は別の量であり、比較してはいけない。**

20日窓＝観測20個の推定量なので、決算ギャップが1つ入るだけで倍近く跳ねる。
実測すると乖離の姿はまるで違った:

| 銘柄 | 20日 | 250日 | 前提(実効) | 実際 |
|:---|---:|---:|---:|:---|
| TECL | 97.9% | 76.2% | 78% | **ほぼ一致**（誤警報だった） |
| TQQQ | 70.8% | 57.5% | 66% | 前提の方が保守的 |
| SOXL | 178.8% | 131.0% | 105% | 1.25倍 |
| 2737.T | 129.8% | 69.0% | 30% | **2.3倍（本物）** |
| 2802.T | 31.5% | 42.9% | 22% | **1.95倍（本物）** |

つまり警告の大半は窓の不一致による水増しで、その裏に
**「日本株の前提 22%/30% が構造的に低すぎる」という本物の問題**が隠れていた。
ノイズで騒ぐと、本当の乖離が埋もれる。

## ここでやること

1. 前提と比べてよい**長窓（既定250日）の実測**を計算する。
2. 20日窓は「足元のボラ」として**別の名前で**持ち、前提とは突き合わせない。
3. 置き換えではなく**縮小推定（shrinkage）で混ぜる**。実測ボラは平均回帰するので、
   直近の水準をそのまま将来の前提にすると予測がノイズを追いかける。
4. 前提・実測・採用値の3つを必ず開示する。**黙って差し替えない。**

## やらないこと

- 観測数が足りないときに数字をひねり出すこと（`available: False` を返す）。
- 「実測の方が高いときだけ採用する」ような非対称な操作。
  それは較正ではなく、都合のよい方を選ぶこと。
"""

from __future__ import annotations

from typing import Any, Optional

#: 前提（構造的な水準）と突き合わせてよい窓。1年ぶんの営業日。
STRUCTURAL_WINDOW = 250

#: 「足元のボラ」用の短窓。**前提との比較には使わない。**
SPOT_WINDOW = 20

#: 較正に必要な最低観測数。これを下回るなら較正しない。
MIN_OBSERVATIONS = 120

#: 実測に置く重み（残りは前提に置く）。
#: 250観測あれば推定精度自体は高いが、ボラティリティは平均回帰するため
#: 直近の水準をそのまま将来の前提にはしない。0.6 は「実測を主としつつ
#: 前提の長期的な情報を捨てない」ための配分。
REALIZED_WEIGHT = 0.6

#: 実測/前提 がこの比を超えたら、前提は水準として妥当でないと判定する。
ELEVATED_RATIO = 1.3
UNRELIABLE_RATIO = 2.0


def realized_vol(closes: Any, window: int = STRUCTURAL_WINDOW) -> Optional[float]:
    """年率換算の実測ボラティリティ(%)。観測が足りなければ None。

    ⚠️ **窓を満たさないときは窓を縮める。** `volatility()` は
    `len < window+1` で None を返すため、窓を 250 に固定すると
    **東証銘柄は永久に較正されない**（日本の年間営業日は約245日で、
    1年分の履歴が 244本しか無い＝251本に届かない）。

    実際これが起きていた。このモジュールの docstring が
    「2802.T の 250日実測は 42.9%、前提 22% は 1.95倍で本物の乖離」と
    書いているのに、パイプラインは一度もその較正に到達せず、
    **日本株だけが前提σのまま**レンジを出し続けていた。

    ``MIN_OBSERVATIONS`` を下回るところまでは縮めない（そこは本当に観測不足）。

    ⚠️ 下限は ``min(MIN_OBSERVATIONS, window)`` であって ``MIN_OBSERVATIONS``
    ではない。**呼び出し側が意図的に短い窓を要求している場合があるため**
    （``SPOT_WINDOW=20`` の足元ボラ）。ここを 120 固定にすると
    スポットσが常に None になり、「窓の不一致で騒がない」というこのモジュールの
    存在理由そのものが消える。
    """
    try:
        from src.core.technicals import volatility

        n = _count(closes)
        effective = min(window, max(n - 1, 0))
        if effective < min(MIN_OBSERVATIONS, window):
            return None
        return volatility(closes, window=effective, annualize=True)
    except Exception:
        return None


def calibrate(
    symbol: Optional[str],
    assumption: dict,
    closes: Any,
    *,
    leverage: float = 1.0,
    realized_weight: float = REALIZED_WEIGHT,
) -> dict:
    """1銘柄の前提ボラを実測で較正する。

    Args:
        assumption: `annual_vol_pct` を持つ前提 dict（**原資産ベース**）
        closes: 実際に売買している商品の終値列（レバレッジETFならETF自体）
        leverage: 倍率。実測はETF側で測るので、原資産に戻すために割る。

    Returns:
        前提・実測・採用値・判定を含む dict。較正できなければ `available: False`。
    """
    assumed = assumption.get("annual_vol_pct")
    lev = float(leverage or 1.0) or 1.0
    out: dict[str, Any] = {
        "symbol": symbol,
        "available": False,
        "assumed_underlying_vol_pct": assumed,
        "leverage": lev,
        "window": STRUCTURAL_WINDOW,
        "spot_window": SPOT_WINDOW,
    }

    n = _count(closes)
    out["observations"] = n
    # 足元のボラは参考として常に出すが、前提とは比較しない。
    spot = realized_vol(closes, window=SPOT_WINDOW)
    out["spot_vol_pct"] = _round(spot)
    out["spot_note"] = (
        f"直近{SPOT_WINDOW}日の年率換算。観測が{SPOT_WINDOW}個しかないため"
        "決算ギャップ1つで大きく振れる。**前提σと直接比較してはいけない。**")

    if n < MIN_OBSERVATIONS:
        out["reason"] = (f"観測 {n}日 < 較正に必要な {MIN_OBSERVATIONS}日。"
                         "前提をそのまま使います。")
        out["used_underlying_vol_pct"] = assumed
        out["verdict"] = "insufficient_data"
        return out

    # 実際に使った窓を開示する。日本株は約244本しか無いので 250 では測れず、
    # ここが 244 等に縮む。**縮めたことを黙らない。**
    out["effective_window"] = min(STRUCTURAL_WINDOW, max(n - 1, 0))
    realized = realized_vol(closes, window=STRUCTURAL_WINDOW)
    if realized is None or not isinstance(assumed, (int, float)) or assumed <= 0:
        out["reason"] = "実測または前提が取得できず、較正できません。"
        out["used_underlying_vol_pct"] = assumed
        out["verdict"] = "insufficient_data"
        return out

    # 実測は商品側で測る。前提は原資産ベースなので倍率で割って揃える。
    implied = realized / lev
    ratio = implied / float(assumed)
    used = realized_weight * implied + (1.0 - realized_weight) * float(assumed)

    out.update({
        "available": True,
        "realized_vol_pct": _round(realized),
        "implied_underlying_vol_pct": _round(implied),
        "ratio": _round(ratio, 2),
        "used_underlying_vol_pct": _round(used),
        "realized_weight": realized_weight,
        "effective_assumed_vol_pct": _round(float(assumed) * lev),
        "effective_used_vol_pct": _round(used * lev),
    })

    if ratio >= UNRELIABLE_RATIO:
        out["verdict"] = "unreliable"
        out["message"] = (
            f"前提 {assumed:.0f}% に対し実測（{STRUCTURAL_WINDOW}日）は "
            f"{implied:.0f}% で {ratio:.1f}倍。**前提が水準として妥当でない。**"
            f" 較正後 {used:.0f}% を使いますが、この銘柄の予測レンジは"
            "参考値として扱ってください。")
    elif ratio >= ELEVATED_RATIO:
        out["verdict"] = "elevated"
        out["message"] = (
            f"前提 {assumed:.0f}% に対し実測 {implied:.0f}%（{ratio:.1f}倍）。"
            f"較正後 {used:.0f}% を使います。")
    elif ratio <= 1.0 / ELEVATED_RATIO:
        out["verdict"] = "conservative"
        out["message"] = (
            f"前提 {assumed:.0f}% は実測 {implied:.0f}% より高く、"
            "前提の方が保守的です。較正後 "
            f"{used:.0f}%。")
    else:
        out["verdict"] = "ok"
        out["message"] = (f"前提 {assumed:.0f}% と実測 {implied:.0f}% は整合的"
                          f"（{ratio:.2f}倍）。")
    return out


def calibrate_positions(positions: list[dict]) -> dict:
    """保有ポジション群をまとめて較正する。

    各要素は `symbol` / `assumption` / `closes` / `leverage` を持つ想定。
    """
    rows: list[dict] = []
    for p in positions or []:
        try:
            rows.append(calibrate(
                p.get("symbol"), p.get("assumption") or {}, p.get("closes"),
                leverage=float(p.get("leverage") or 1.0)))
        except Exception:
            continue

    calibrated = [r for r in rows if r.get("available")]
    unreliable = [r for r in calibrated if r.get("verdict") == "unreliable"]
    elevated = [r for r in calibrated if r.get("verdict") == "elevated"]

    if not rows:
        summary = "較正対象がありません。"
    elif not calibrated:
        summary = (f"{len(rows)}件すべてで観測が足りず較正できませんでした。"
                   "前提をそのまま使っています。")
    else:
        bits = [f"{len(calibrated)}/{len(rows)}件を較正"]
        if unreliable:
            bits.append(f"うち {len(unreliable)}件は前提が妥当でない水準")
        if elevated:
            bits.append(f"{len(elevated)}件は実測が前提を上回る")
        summary = "・".join(bits) + "。"

    return {
        "rows": rows,
        "calibrated": len(calibrated),
        "total": len(rows),
        "unreliable": [r.get("symbol") for r in unreliable],
        "elevated": [r.get("symbol") for r in elevated],
        "summary": summary,
        "caveat": (
            f"実測は{STRUCTURAL_WINDOW}日窓。テクニカルの `volatility_pct`"
            f"（{SPOT_WINDOW}日窓）とは別の量であり、前提σと比較してよいのは"
            "こちらだけです。較正は置き換えではなく縮小推定で、"
            f"実測に {REALIZED_WEIGHT:.0%} の重みを置いています。"),
    }


def _count(closes: Any) -> int:
    try:
        return len(list(closes))
    except Exception:
        return 0


def _round(v: Any, digits: int = 1) -> Optional[float]:
    return round(float(v), digits) if isinstance(v, (int, float)) else None
