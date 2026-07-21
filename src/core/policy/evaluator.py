"""政策評価器 -- 現在市況と全政策のトリガー距離を計算する (案A P2).

急変時の質問に対して、分析を再実行するのではなく既定政策を参照して返すための中核。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

from src.core.policy.ledger import is_expired


#: 閾値までの残り幅が「基準スケール」の何割以内なら接近とみなすか
NEAR_TRIGGER_RATIO = 0.35

#: 指標ごとの接近判定に使う基準スケール(絶対量)
_NEAR_SCALE: dict[str, float] = {
    "price_change_pct": 20.0,
    "drawdown_pct": 20.0,
    "rsi": 30.0,
    "per": 10.0,
    "pbr": 2.0,
    "dividend_yield": 3.0,
    "operating_margin": 10.0,
    "position_weight_pct": 20.0,
    "days_held": 180.0,
}

_STATE_LABEL = {
    "met": "成立",
    "near": "接近中",
    "far": "不成立",
    "unknown": "判定不能",
}


def _compare(actual: float, op: str, threshold: float) -> bool:
    if op == "<":
        return actual < threshold
    if op == "<=":
        return actual <= threshold
    if op == ">":
        return actual > threshold
    if op == ">=":
        return actual >= threshold
    if op == "==":
        return actual == threshold
    return False


def trigger_distance(trigger: dict, market_state: dict) -> Optional[float]:
    """トリガー閾値までの残り幅。成立済みなら 0、指標が無ければ None。"""
    metric = trigger.get("metric", "")
    if metric not in market_state or market_state.get(metric) is None:
        return None
    try:
        actual = float(market_state[metric])
    except (TypeError, ValueError):
        return None
    threshold = float(trigger["value"])
    if _compare(actual, trigger["op"], threshold):
        return 0.0
    return abs(actual - threshold)


def evaluate_trigger(trigger: dict, market_state: dict) -> dict:
    """トリガー1件を評価する。

    Returns
    -------
    dict
        {"metric", "op", "value", "actual", "state", "distance"}
        state は met / near / far / unknown。
    """
    metric = trigger.get("metric", "")
    actual = market_state.get(metric)
    distance = trigger_distance(trigger, market_state)

    if distance is None:
        state = "unknown"
    elif distance == 0.0:
        state = "met"
    else:
        scale = _NEAR_SCALE.get(metric)
        if scale is None:
            try:
                scale = abs(float(trigger["value"])) or 1.0
            except (TypeError, ValueError):
                scale = 1.0
        state = "near" if distance <= scale * NEAR_TRIGGER_RATIO else "far"

    return {
        "metric": metric,
        "op": trigger.get("op", ""),
        "value": trigger.get("value"),
        "actual": actual,
        "state": state,
        "distance": distance,
    }


def evaluate_policy(
    policy: dict, market_state: dict, today: Optional[date] = None
) -> dict:
    """政策1本を現在市況に照らして評価する。

    Returns
    -------
    dict
        {"policy_id", "symbol", "state", "label", "response", "triggers", "expired"}
        state は met(いずれか成立) / near(接近) / far / unknown。
    """
    expired = is_expired(policy, today)
    evaluations = [evaluate_trigger(t, market_state) for t in policy.get("triggers", [])]
    states = {e["state"] for e in evaluations}

    if "met" in states:
        state = "met"
    elif "near" in states:
        state = "near"
    elif "far" in states:
        state = "far"
    else:
        state = "unknown"

    return {
        "policy_id": policy.get("id", ""),
        "symbol": policy.get("symbol", ""),
        "intent": policy.get("intent", ""),
        "state": state,
        "label": _STATE_LABEL.get(state, state),
        "response": policy.get("response", ""),
        "expires_on": policy.get("expires_on"),
        "expired": expired,
        "triggers": evaluations,
        # 成立したトリガーだけ抜き出す(複数同時成立に対応)
        "met_triggers": [e for e in evaluations if e["state"] == "met"],
    }


def policy_response(
    symbol: str,
    market_state: dict,
    policies: Optional[list[dict]] = None,
    today: Optional[date] = None,
    base_dir: Optional[str] = None,
) -> dict:
    """急変時の質問に返すべき「政策上の応答」を組み立てる (案A P2 / 具体例12).

    分析を再実行せず、平時に確定した政策を参照する。

    Returns
    -------
    dict
        {"symbol", "has_policy", "assessments", "answer", "requires_cooling",
         "expired_policies"}
    """
    if policies is None:
        from src.core.policy.ledger import list_policies

        kwargs = {"symbol": symbol, "active_only": False, "today": today}
        if base_dir is not None:
            kwargs["base_dir"] = base_dir
        policies = list_policies(**kwargs)

    assessments = [evaluate_policy(p, market_state, today) for p in policies]
    active = [a for a in assessments if not a["expired"]]
    expired = [a for a in assessments if a["expired"]]

    if not active:
        answer = (
            f"{symbol} に有効な政策がありません。"
            + ("失効済みの政策があります。再審査してください。" if expired else "")
        )
        return {
            "symbol": symbol,
            "has_policy": False,
            "assessments": assessments,
            "answer": answer,
            "requires_cooling": False,
            "expired_policies": expired,
        }

    met = [a for a in active if a["state"] == "met"]
    near = [a for a in active if a["state"] == "near"]

    lines: list[str] = []
    for a in active:
        conds = ", ".join(
            f"{t['metric']} {t['op']} {t['value']}(現在 {t['actual']}) → {_STATE_LABEL[t['state']]}"
            for t in a["triggers"]
        )
        lines.append(
            f"政策 {a['policy_id']}: 応答は「{a['response']}」。条件: {conds}。"
            f"総合判定: {a['label']}（失効期限 {a['expires_on']}）"
        )

    if met:
        head = "政策のトリガーが成立しています。政策上の応答を実行してください。"
    elif near:
        head = "トリガー接近中ですが未成立です。政策上の応答は現状維持です。"
    else:
        head = "条件不成立です。政策上の応答は現状維持です。"

    if met or near:
        head += " いま政策を改訂する場合、冷却期間が適用されます。政策を破る場合は逸脱として記録されます。"

    return {
        "symbol": symbol,
        "has_policy": True,
        "assessments": assessments,
        "answer": head + "\n" + "\n".join(lines),
        "requires_cooling": bool(met or near),
        "expired_policies": expired,
    }
