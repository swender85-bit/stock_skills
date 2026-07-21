"""政策台帳の CRUD と実行可能性審査 (案A P1).

政策 = 「どの状態になったら、何をするか」の事前確定。

設計上の要求:
  - 失効期限は必須(無期限政策の禁止)。期限切れは再審査を強制する。
  - トリガーは測定可能でなければ登録できない。「なんか下がったら」は
    将来の自分が従えないので拒否する(Review段階の実行可能性審査を前倒し)。
  - 改訂は常に自由。ただしトリガー接近・成立中の改訂だけ冷却期間を課す
    (平時の柔軟性と有事の拘束の分離)。
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from src.core.temporal import market_of, parse_instant, stamp_for_symbol


_POLICIES_DIR = "data/policies"

#: 測定可能な指標のみをトリガーに使える。曖昧語はここに無いので弾かれる。
MEASURABLE_METRICS: dict[str, str] = {
    "price": "株価(現地通貨)",
    "price_change_pct": "取得来 or 基準比の騰落率(%)",
    "drawdown_pct": "直近高値からの下落率(%)",
    "rsi": "RSI(14)",
    "per": "PER(倍)",
    "pbr": "PBR(倍)",
    "dividend_yield": "配当利回り(%)",
    "operating_cf": "営業キャッシュフロー",
    "operating_margin": "営業利益率(%)",
    "position_weight_pct": "PF内の構成比(%)",
    "days_held": "保有日数",
}

#: 比較演算子
OPERATORS = ("<", "<=", ">", ">=", "==")

#: トリガー接近とみなす距離比(閾値までの残り幅がレンジの何割か)
DEFAULT_COOLING_HOURS = 24


class AmbiguousTriggerError(ValueError):
    """将来の自分が従えない曖昧なトリガー。"""


class PolicyExpiredError(RuntimeError):
    """失効済み政策の適用試行。"""


class CoolingPeriodError(RuntimeError):
    """トリガー接近・成立中の改訂に対する冷却期間。"""


def _policies_dir(base_dir: str = _POLICIES_DIR) -> Path:
    d = Path(base_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def validate_trigger(trigger: Any) -> dict:
    """トリガーが機械判定可能かを審査し、正規化して返す。

    受け付ける形:
        {"metric": "drawdown_pct", "op": "<=", "value": -15}

    Raises
    ------
    AmbiguousTriggerError
        自由文・未知の指標・不正な演算子・非数値の閾値。
    """
    if isinstance(trigger, str):
        raise AmbiguousTriggerError(
            f"自由文のトリガーは登録できません: 「{trigger}」。"
            f"{{'metric': ..., 'op': ..., 'value': ...}} の形で、"
            f"測定可能な指標({', '.join(sorted(MEASURABLE_METRICS))})を使ってください。"
        )
    if not isinstance(trigger, dict):
        raise AmbiguousTriggerError("トリガーは dict で指定してください。")

    metric = str(trigger.get("metric", "")).strip()
    if metric not in MEASURABLE_METRICS:
        raise AmbiguousTriggerError(
            f"測定不能な指標です: 「{metric or '(未指定)'}」。"
            f"使える指標: {', '.join(sorted(MEASURABLE_METRICS))}"
        )

    op = str(trigger.get("op", "")).strip()
    if op not in OPERATORS:
        raise AmbiguousTriggerError(
            f"不正な演算子です: 「{op or '(未指定)'}」。使える演算子: {', '.join(OPERATORS)}"
        )

    value = trigger.get("value")
    try:
        value = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise AmbiguousTriggerError(
            f"閾値が数値ではありません: 「{trigger.get('value')}」。"
        ) from None

    return {"metric": metric, "op": op, "value": value}


def _check_trigger_conflicts(triggers: list[dict]) -> None:
    """同一指標に矛盾する条件が同居していないか検査する。"""
    by_metric: dict[str, list[dict]] = {}
    for t in triggers:
        by_metric.setdefault(t["metric"], []).append(t)

    for metric, group in by_metric.items():
        lowers = [t["value"] for t in group if t["op"] in ("<", "<=")]
        uppers = [t["value"] for t in group if t["op"] in (">", ">=")]
        # 「x <= -15 かつ x >= -5」は同時に成立しない
        if lowers and uppers and max(uppers) > min(lowers):
            raise AmbiguousTriggerError(
                f"矛盾するトリガーです({metric}): "
                f"{metric} <= {min(lowers)} と {metric} >= {max(uppers)} は同時に成立しません。"
            )


def build_policy(
    symbol: str,
    response: str,
    triggers: list[Any],
    expires_on: Any,
    rationale: str = "",
    intent: str = "conditional_commit",
    created_at: Any = None,
) -> dict:
    """政策を構築する (案A P1).

    Parameters
    ----------
    symbol : str
        対象ティッカー。
    response : str
        トリガー成立時の応答(例: "全株売却", "追加検討", "何もしない")。
    triggers : list
        トリガー条件のリスト。全て measurable でなければ拒否される。
    expires_on : str | date
        失効期限。**必須**。無期限政策は登録できない。
    intent : str
        意思決定状態: conditional_commit(条件付きコミット) /
        awaiting_trigger(トリガー待機) / deliberate_inaction(意図的不作為)。

    Raises
    ------
    ValueError
        期限が無い、応答が空、期限が過去。
    AmbiguousTriggerError
        トリガーが測定不能または矛盾。
    """
    if not symbol:
        raise ValueError("symbol is required")
    if not response or not str(response).strip():
        raise ValueError("政策には応答(response)が必要です。")
    if not expires_on:
        raise ValueError(
            "失効期限(expires_on)は必須です。無期限の政策は登録できません"
            "(期限切れ時の再審査が案Aの硬直化抑制策です)。"
        )

    ts = stamp_for_symbol(symbol, at=created_at)
    created_day = date.fromisoformat(ts["utc"][:10])
    expiry = expires_on if isinstance(expires_on, date) else date.fromisoformat(str(expires_on)[:10])
    if expiry <= created_day:
        raise ValueError(f"失効期限が過去または当日です: {expiry.isoformat()}")

    if not triggers:
        raise AmbiguousTriggerError("政策にはトリガーが最低1つ必要です。")
    normalized = [validate_trigger(t) for t in triggers]
    _check_trigger_conflicts(normalized)

    return {
        "id": f"pol_{ts['utc'][:10]}_{symbol.replace('.', '_')}_{uuid.uuid4().hex[:8]}",
        "symbol": symbol,
        "market": market_of(symbol),
        "intent": intent,
        "response": str(response).strip(),
        "rationale": rationale,
        "triggers": normalized,
        "created_at": ts,
        "expires_on": expiry.isoformat(),
        "status": "active",
        "revision": 1,
        "history": [],
    }


def is_expired(policy: dict, today: Optional[date] = None) -> bool:
    """政策が失効しているか。"""
    if policy.get("status") == "expired":
        return True
    ref = today or date.today()
    try:
        return ref > date.fromisoformat(str(policy.get("expires_on"))[:10])
    except (TypeError, ValueError):
        return False


def revise_policy(
    policy: dict,
    response: Optional[str] = None,
    triggers: Optional[list[Any]] = None,
    expires_on: Any = None,
    rationale: str = "",
    market_state: Optional[dict] = None,
    force: bool = False,
    at: Any = None,
    cooling_hours: int = DEFAULT_COOLING_HOURS,
) -> dict:
    """政策を改訂する。トリガー接近・成立中は冷却期間を課す (案A 抑制設計).

    平時の改訂は自由。有事(トリガーが近い・成立している)の改訂だけが
    冷却期間の対象になる。これがストレス下の即興的な政策破棄を防ぐ。

    Raises
    ------
    CoolingPeriodError
        トリガー接近・成立中で、冷却期間が明けていない場合。
    """
    from src.core.policy.evaluator import evaluate_policy

    now = parse_instant(at) or datetime.now(timezone.utc)

    if market_state is not None and not force:
        assessment = evaluate_policy(policy, market_state)
        if assessment["state"] in ("met", "near"):
            last = parse_instant(policy.get("cooling_started_at"))
            if last is None:
                policy["cooling_started_at"] = now.isoformat(timespec="seconds")
                raise CoolingPeriodError(
                    f"トリガー{'成立' if assessment['state'] == 'met' else '接近'}中のため、"
                    f"{cooling_hours}時間の冷却期間が適用されます。"
                    f"期間経過後に再度改訂してください。"
                )
            if now - last < timedelta(hours=cooling_hours):
                remaining = timedelta(hours=cooling_hours) - (now - last)
                raise CoolingPeriodError(
                    f"冷却期間中です。あと約{int(remaining.total_seconds() // 3600)}時間後に改訂できます。"
                )

    # 改訂前の内容を履歴に残す(心変わりの時系列)
    policy.setdefault("history", []).append(
        {
            "revision": policy.get("revision", 1),
            "response": policy.get("response"),
            "triggers": list(policy.get("triggers", [])),
            "expires_on": policy.get("expires_on"),
            "revised_at": now.isoformat(timespec="seconds"),
            "rationale": rationale,
        }
    )

    if response is not None:
        if not str(response).strip():
            raise ValueError("応答を空にはできません。")
        policy["response"] = str(response).strip()
    if triggers is not None:
        normalized = [validate_trigger(t) for t in triggers]
        _check_trigger_conflicts(normalized)
        policy["triggers"] = normalized
    if expires_on is not None:
        expiry = (
            expires_on if isinstance(expires_on, date)
            else date.fromisoformat(str(expires_on)[:10])
        )
        policy["expires_on"] = expiry.isoformat()
        policy["status"] = "active"

    policy["revision"] = policy.get("revision", 1) + 1
    policy.pop("cooling_started_at", None)
    return policy


def expire_policy(policy: dict, reason: str = "") -> dict:
    """政策を明示的に失効させる。"""
    policy["status"] = "expired"
    policy["expired_reason"] = reason
    return policy


def save_policy(policy: dict, base_dir: str = _POLICIES_DIR) -> Path:
    path = _policies_dir(base_dir) / f"{policy['id']}.json"
    path.write_text(json.dumps(policy, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_policy(policy_id: str, base_dir: str = _POLICIES_DIR) -> Optional[dict]:
    path = Path(base_dir) / f"{policy_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_policies(
    symbol: Optional[str] = None,
    active_only: bool = True,
    today: Optional[date] = None,
    base_dir: str = _POLICIES_DIR,
) -> list[dict]:
    """政策を一覧する。既定では有効なものだけを返す。"""
    d = Path(base_dir)
    if not d.exists():
        return []
    out: list[dict] = []
    for path in d.glob("pol_*.json"):
        try:
            pol = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if symbol and pol.get("symbol") != symbol:
            continue
        if active_only and is_expired(pol, today):
            continue
        out.append(pol)
    out.sort(key=lambda p: p.get("created_at", {}).get("utc", ""), reverse=True)
    return out
