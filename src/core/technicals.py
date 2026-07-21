"""テクニカル指標と過熱/売られすぎ判定 (週次レポート用).

価格系列(pandas Series or list)から、週次レポートに必要な指標を計算する。
データ不足時は None を返し、呼び出し側で「算出不可」と表示できるようにする
(欠損を 0 や False に潰すと、過熱していないのに「正常」と誤読させる)。
"""

from __future__ import annotations

from typing import Any, Optional, Sequence


def _series(values: Any) -> list[float]:
    """pandas Series / list / numpy array を float のリストに正規化する。"""
    if values is None:
        return []
    if hasattr(values, "dropna"):
        values = values.dropna().tolist()
    out: list[float] = []
    for v in values:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f == f:  # NaN を除く
            out.append(f)
    return out


def sma(values: Any, window: int) -> Optional[float]:
    """単純移動平均。データ不足なら None。"""
    data = _series(values)
    if window <= 0 or len(data) < window:
        return None
    return sum(data[-window:]) / window


def rsi(values: Any, period: int = 14) -> Optional[float]:
    """Wilder の RSI(最新値)。データ不足なら None。

    70超で買われすぎ、30未満で売られすぎが慣例。

    計算そのものは既存の ``src.core.screening.technicals.compute_rsi`` に委譲する
    (スクリーニングとレポートで RSI の定義がずれると、同じ銘柄に別の判定が出る)。
    pandas が使えない環境では下のフォールバックで同じ Wilder 平滑化を行う。
    """
    data = _series(values)
    if period <= 0 or len(data) < period + 1:
        return None

    try:
        import pandas as pd

        from src.core.screening.technicals import compute_rsi

        result = compute_rsi(pd.Series(data), period=period)
        last = result.dropna()
        if len(last):
            return float(last.iloc[-1])
    except Exception:
        pass

    return _rsi_fallback(data, period)


def _rsi_fallback(data: list[float], period: int) -> Optional[float]:
    """pandas 無し環境用の Wilder RSI。"""
    gains: list[float] = []
    losses: list[float] = []
    for prev, cur in zip(data[:-1], data[1:]):
        change = cur - prev
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def ema(values: Any, span: int) -> Optional[float]:
    """指数移動平均。"""
    data = _series(values)
    if span <= 0 or len(data) < span:
        return None
    k = 2.0 / (span + 1.0)
    result = sum(data[:span]) / span
    for v in data[span:]:
        result = v * k + result * (1 - k)
    return result


def macd(values: Any, fast: int = 12, slow: int = 26, signal: int = 9) -> Optional[dict]:
    """MACD。{"macd", "signal", "histogram"} を返す。データ不足なら None。"""
    data = _series(values)
    if len(data) < slow + signal:
        return None

    def _ema_series(seq: list[float], span: int) -> list[float]:
        k = 2.0 / (span + 1.0)
        out = [sum(seq[:span]) / span]
        for v in seq[span:]:
            out.append(v * k + out[-1] * (1 - k))
        return out

    fast_line = _ema_series(data, fast)
    slow_line = _ema_series(data, slow)
    # 長さを揃える(slow の方が短い)
    offset = len(fast_line) - len(slow_line)
    macd_line = [f - s for f, s in zip(fast_line[offset:], slow_line)]
    if len(macd_line) < signal:
        return None
    signal_line = _ema_series(macd_line, signal)

    macd_val = macd_line[-1]
    signal_val = signal_line[-1]
    return {
        "macd": macd_val,
        "signal": signal_val,
        "histogram": macd_val - signal_val,
    }


def bollinger(values: Any, window: int = 20, num_std: float = 2.0) -> Optional[dict]:
    """ボリンジャーバンド。{"upper","middle","lower","percent_b"} を返す。"""
    data = _series(values)
    if len(data) < window:
        return None

    upper = lower = mid = None
    try:
        import pandas as pd

        from src.core.screening.technicals import compute_bollinger_bands

        u, m, l = compute_bollinger_bands(pd.Series(data), period=window, std_dev=num_std)
        if len(u.dropna()):
            upper, mid, lower = float(u.iloc[-1]), float(m.iloc[-1]), float(l.iloc[-1])
    except Exception:
        pass

    if upper is None:
        recent = data[-window:]
        mid = sum(recent) / window
        variance = sum((x - mid) ** 2 for x in recent) / window
        std = variance ** 0.5
        upper = mid + num_std * std
        lower = mid - num_std * std

    last = data[-1]
    width = upper - lower
    percent_b = (last - lower) / width if width > 0 else 0.5
    return {
        "upper": upper,
        "middle": mid,
        "lower": lower,
        "percent_b": percent_b,
    }


def high_low_range(
    values: Any, window: int = 252, min_points: int = 20
) -> Optional[dict]:
    """52週(既定252営業日)の高値・安値と現在位置。

    ``min_points`` 未満のデータでは None を返す。3日分の値から「52週レンジの
    どこにいるか」を出すと、意味のない数字が過熱判定の材料として使われてしまう。
    """
    data = _series(values)
    if len(data) < min_points:
        return None
    recent = data[-window:] if len(data) > window else data
    high, low = max(recent), min(recent)
    last = data[-1]
    span = high - low
    return {
        "high": high,
        "low": low,
        "last": last,
        # 0=安値圏, 1=高値圏
        "position": (last - low) / span if span > 0 else 0.5,
        "from_high_pct": ((last - high) / high * 100.0) if high else None,
    }


def volatility(values: Any, window: int = 20, annualize: bool = True) -> Optional[float]:
    """日次リターンの標準偏差。annualize=True で年率換算(%)。"""
    data = _series(values)
    if len(data) < window + 1:
        return None
    recent = data[-(window + 1):]
    rets = [
        (cur - prev) / prev
        for prev, cur in zip(recent[:-1], recent[1:])
        if prev != 0
    ]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    std = var ** 0.5
    if annualize:
        std *= (252 ** 0.5)
    return std * 100.0


# ---------------------------------------------------------------------------
# 過熱 / 売られすぎ 判定
# ---------------------------------------------------------------------------

OVERBOUGHT = "overbought"
OVERSOLD = "oversold"
NEUTRAL = "neutral"
UNKNOWN = "unknown"

_HEAT_LABEL = {
    OVERBOUGHT: "買われすぎ",
    OVERSOLD: "売られすぎ",
    NEUTRAL: "中立",
    UNKNOWN: "判定不能",
}


def heat_label(state: str) -> str:
    return _HEAT_LABEL.get(state, state)


def assess_heat(indicators: dict) -> dict:
    """複数指標を合議して過熱/売られすぎを判定する。

    単一指標(RSIだけ)では騙されるので、RSI・%B・52週位置・移動平均乖離の
    4つで多数決を取る。算出できた指標が1つも無ければ ``unknown``。

    Parameters
    ----------
    indicators : dict
        ``rsi`` / ``percent_b`` / ``range_position`` / ``sma_deviation_pct`` を含む dict。

    Returns
    -------
    dict
        {"state", "label", "score", "signals": [...], "available": int}
    """
    signals: list[str] = []
    score = 0
    available = 0

    r = indicators.get("rsi")
    if r is not None:
        available += 1
        if r >= 70:
            score += 1
            signals.append(f"RSI {r:.1f} — 買われすぎ圏(70超)")
        elif r <= 30:
            score -= 1
            signals.append(f"RSI {r:.1f} — 売られすぎ圏(30未満)")
        else:
            signals.append(f"RSI {r:.1f} — 中立")

    pb = indicators.get("percent_b")
    if pb is not None:
        available += 1
        if pb >= 1.0:
            score += 1
            signals.append(f"%B {pb:.2f} — ボリンジャー上限超え")
        elif pb <= 0.0:
            score -= 1
            signals.append(f"%B {pb:.2f} — ボリンジャー下限割れ")
        else:
            signals.append(f"%B {pb:.2f} — バンド内")

    pos = indicators.get("range_position")
    if pos is not None:
        available += 1
        if pos >= 0.9:
            score += 1
            signals.append(f"52週レンジ上位 {pos * 100:.0f}% — 高値圏")
        elif pos <= 0.1:
            score -= 1
            signals.append(f"52週レンジ下位 {pos * 100:.0f}% — 安値圏")

    dev = indicators.get("sma_deviation_pct")
    if dev is not None:
        available += 1
        if dev >= 15:
            score += 1
            signals.append(f"200日線から +{dev:.1f}% 上方乖離 — 過熱")
        elif dev <= -15:
            score -= 1
            signals.append(f"200日線から {dev:.1f}% 下方乖離 — 過度な売られ")

    if available == 0:
        state = UNKNOWN
    elif score >= 2:
        state = OVERBOUGHT
    elif score <= -2:
        state = OVERSOLD
    else:
        state = NEUTRAL

    return {
        "state": state,
        "label": heat_label(state),
        "score": score,
        "signals": signals,
        "available": available,
    }


def analyze_prices(closes: Any) -> dict:
    """終値系列から週次レポート用のテクニカル一式を計算する。"""
    data = _series(closes)
    last = data[-1] if data else None

    sma20 = sma(data, 20)
    sma50 = sma(data, 50)
    sma200 = sma(data, 200)
    bb = bollinger(data)
    hl = high_low_range(data)
    rsi14 = rsi(data)
    macd_vals = macd(data)

    deviation = None
    if last is not None and sma200:
        deviation = (last - sma200) / sma200 * 100.0

    heat = assess_heat(
        {
            "rsi": rsi14,
            "percent_b": bb["percent_b"] if bb else None,
            "range_position": hl["position"] if hl else None,
            "sma_deviation_pct": deviation,
        }
    )

    trend = None
    if sma50 is not None and sma200 is not None:
        trend = "上昇トレンド(50日>200日)" if sma50 > sma200 else "下降トレンド(50日<200日)"

    return {
        "last": last,
        "sma20": sma20,
        "sma50": sma50,
        "sma200": sma200,
        "sma200_deviation_pct": deviation,
        "rsi14": rsi14,
        "macd": macd_vals,
        "bollinger": bb,
        "range_52w": hl,
        "volatility_pct": volatility(data),
        "trend": trend,
        "heat": heat,
        "data_points": len(data),
    }
