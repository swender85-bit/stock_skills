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
    "position_value_jpy": 20_000_000.0,
    "days_held": 180.0,
}

_STATE_LABEL = {
    "met": "成立",
    "near": "接近中",
    "far": "不成立",
    "unknown": "判定不能",
    # トリガーを持たない意図的不作為。「判定不能」ではない —— 判定する対象が無いのが正解。
    "standing": "常時有効（意図的不作為）",
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

    if not evaluations and policy.get("intent") == "deliberate_inaction":
        # 反応しないことが中身の政策。「判定不能」と書くと取得失敗と混同される。
        state = "standing"
    elif "met" in states:
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


# ---------------------------------------------------------------------------
# 長期金利ゲート（改善6）
# ---------------------------------------------------------------------------

#: 「投入」を意味する応答の語。ここに当たる政策だけが金利ゲートの対象になる。
#: 売却・撤退の政策を止めてはいけない（下落時に売れなくなるのは危険側の誤り）。
_ENTRY_WORDS = ("投入", "買い", "買増", "買い増し", "取得", "購入", "エントリー", "トランシェ",
                "追加", "積み増し", "積増")


def _rate_thresholds() -> dict:
    from src.core._thresholds import th

    return {
        "ust30y_warning": th("rates", "ust30y_warning", 5.50),
        "ust10y_warning": th("rates", "ust10y_warning", 5.00),
        "ust30y_spike_1m": th("rates", "ust30y_spike_1m", 0.50),
        "gate_min_leverage": th("rates", "gate_min_leverage", 2),
        "rationale": th("rates", "rationale", "provisional"),
    }


def is_entry_policy(policy: dict) -> bool:
    """この政策の応答は「買う」側か。売り・撤退はゲートの対象外。"""
    response = str(policy.get("response") or "")
    return any(w in response for w in _ENTRY_WORDS)


def check_rate_gate(
    policy: dict, market_state: dict, leverage: Optional[float] = None
) -> dict:
    """長期金利による投入の拒否条件 (改善6).

    レバレッジETFは実質ロング・デュレーション資産で、**効くのは短期金利ではなく
    長期金利**。政策金利予想が緩んだ日に30年債が19年ぶり高水準まで売られ、
    株が大幅安になった実例がある。短期金利予想だけを見ると、この日を
    「緩和」と誤判定する。

    Returns
    -------
    dict
        {"blocked", "available", "reasons", "checked", "provisional"}
        `available=False` は「金利が取れなかった」であって
        **「問題なし」ではない**（§16-1）。取れない場合は blocked にしない
        （取得失敗を理由に投入を止めると、データ欠損が投資判断になる）。
    """
    cfg = _rate_thresholds()
    if not is_entry_policy(policy):
        return {"blocked": False, "available": True, "checked": [],
                "reasons": [], "provisional": False,
                "note": "売却・撤退側の政策なので金利ゲートの対象外です。"}

    if leverage is not None and float(leverage) < float(cfg["gate_min_leverage"]):
        return {"blocked": False, "available": True, "checked": [],
                "reasons": [], "provisional": False,
                "note": f"レバレッジ {leverage}倍はゲート対象外"
                        f"（{cfg['gate_min_leverage']}倍以上が対象）。"}

    checks = (
        ("ust30y", ">=", cfg["ust30y_warning"],
         "30年債利回りが警戒水準を超えています"),
        ("ust10y", ">=", cfg["ust10y_warning"],
         "10年債利回りが警戒水準を超えています"),
        ("ust30y_change_1m", ">=", cfg["ust30y_spike_1m"],
         "30年債利回りが1ヶ月で急騰しています"),
    )

    reasons: list[str] = []
    checked: list[dict] = []
    missing: list[str] = []
    for metric, op, threshold, label in checks:
        actual = market_state.get(metric)
        if actual is None:
            missing.append(metric)
            continue
        try:
            value = float(actual)
        except (TypeError, ValueError):
            missing.append(metric)
            continue
        hit = _compare(value, op, float(threshold))
        checked.append({"metric": metric, "actual": value,
                        "threshold": threshold, "hit": hit})
        if hit:
            reasons.append(f"{label}（{metric} {value:.2f} {op} {threshold:.2f}）")

    if not checked:
        return {
            "blocked": False, "available": False, "checked": [], "reasons": [],
            "provisional": cfg["rationale"] == "provisional", "missing": missing,
            "note": ("長期金利を取得できませんでした。**これは『金利に問題なし』ではありません。**"
                     "取得失敗を理由に投入を止めると、データ欠損が投資判断になるため"
                     "ゲートは通しますが、金利は未確認として扱ってください。"),
        }

    provisional = cfg["rationale"] == "provisional"
    note = None
    if reasons and provisional:
        note = ("⚠️ この閾値は暫定値（provisional）で、現在値からの距離で置いたものです。"
                "理論的根拠はありません。条件を増やすほど『永遠に買えない』リスクが"
                "上がるので、この条項が何回投入を止めたかは逸脱監査で追跡してください。")

    return {"blocked": bool(reasons), "available": True, "checked": checked,
            "reasons": reasons, "provisional": provisional,
            "missing": missing, "note": note}


def rate_state_from_yield_curve(curve: Optional[dict]) -> dict:
    """`market_dashboard.get_yield_curve()` の出力を金利ゲート用の状態にする。

    取れなかったテナーは**キーごと入れない**。None を入れると 0 と誤評価され、
    「金利0%＝安全」という最悪の誤読を生む。
    """
    state: dict[str, float] = {}
    if not isinstance(curve, dict):
        return state
    yields = curve.get("yields") or {}
    for key, tenor in (("ust10y", "10Y"), ("ust30y", "30Y")):
        value = yields.get(tenor)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            state[key] = float(value)
    change = curve.get("change_1m") or {}
    if isinstance(change.get("30Y"), (int, float)):
        state["ust30y_change_1m"] = float(change["30Y"])
    if isinstance(change.get("10Y"), (int, float)):
        state["ust10y_change_1m"] = float(change["10Y"])
    return state


def policy_response(
    symbol: str,
    market_state: dict,
    policies: Optional[list[dict]] = None,
    today: Optional[date] = None,
    base_dir: Optional[str] = None,
    leverage: Optional[float] = None,
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

    # 長期金利ゲート（改善6）— トリガー成立でも、投入側は金利条項で止まりうる
    by_id = {p.get("id"): p for p in policies}
    for a in active:
        policy = by_id.get(a["policy_id"]) or {}
        if a["state"] != "met" or not is_entry_policy(policy):
            continue
        gate = check_rate_gate(policy, market_state, leverage)
        a["rate_gate"] = gate
        a["blocked"] = bool(gate.get("blocked"))
        if gate.get("blocked"):
            a["label"] = "成立（ただし長期金利条項で保留）"

    met = [a for a in active if a["state"] == "met"]
    near = [a for a in active if a["state"] == "near"]
    standing = [a for a in active if a["state"] == "standing"]
    blocked = [a for a in met if a.get("blocked")]

    lines: list[str] = []
    for a in active:
        conds = ", ".join(
            f"{t['metric']} {t['op']} {t['value']}(現在 {t['actual']}) → {_STATE_LABEL[t['state']]}"
            for t in a["triggers"]
        ) or "条件なし（意図的不作為: どの状態変化にも反応しないことが政策の中身）"
        lines.append(
            f"政策 {a['policy_id']}: 応答は「{a['response']}」。条件: {conds}。"
            f"総合判定: {a['label']}（失効期限 {a['expires_on']}）"
        )
        gate = a.get("rate_gate") or {}
        for reason in gate.get("reasons") or []:
            lines.append(f"    ⛔ 長期金利条項: {reason}")
        if gate.get("note"):
            lines.append(f"    ℹ️ {gate['note']}")

    if blocked and len(blocked) == len(met):
        head = ("トリガーは成立していますが、**長期金利条項により投入を見送ります**。"
                "レバレッジETFは実質ロング・デュレーション資産で、効くのは短期金利ではなく"
                "長期金利です。")
    elif met:
        head = "政策のトリガーが成立しています。政策上の応答を実行してください。"
    elif near:
        head = "トリガー接近中ですが未成立です。政策上の応答は現状維持です。"
    elif standing and len(standing) == len(active):
        head = ("この銘柄は意図的不作為（deliberate_inaction）が確定しています。"
                "新たに判断せず、既定の応答を維持してください。")
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
        "rate_blocked": [a["policy_id"] for a in blocked],
    }
