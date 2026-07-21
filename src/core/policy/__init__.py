"""Policy ledger -- 判断と執行の時間分離 (Fable5 第2弾 案A).

冷静な時点で「状態→応答」の政策を確定し、事態成立時は政策を機械的に参照する。
出力の型を「行為の推奨」から「政策」へ変える層。全体が opt-in。
"""

from src.core.policy.ledger import (
    AmbiguousTriggerError,
    CoolingPeriodError,
    MEASURABLE_METRICS,
    OPERATORS,
    PolicyExpiredError,
    build_policy,
    expire_policy,
    list_policies,
    load_policy,
    revise_policy,
    save_policy,
    validate_trigger,
)
from src.core.policy.evaluator import (
    NEAR_TRIGGER_RATIO,
    evaluate_policy,
    evaluate_trigger,
    policy_response,
    trigger_distance,
)
from src.core.policy.deviation import (
    coverage_rate,
    detect_deviations,
    record_deviation,
)

__all__ = [
    "AmbiguousTriggerError",
    "CoolingPeriodError",
    "MEASURABLE_METRICS",
    "OPERATORS",
    "PolicyExpiredError",
    "build_policy",
    "expire_policy",
    "list_policies",
    "load_policy",
    "revise_policy",
    "save_policy",
    "validate_trigger",
    "NEAR_TRIGGER_RATIO",
    "evaluate_policy",
    "evaluate_trigger",
    "policy_response",
    "trigger_distance",
    "coverage_rate",
    "detect_deviations",
    "record_deviation",
]
