"""Decision packages -- 可知集合の凍結と過程再審 (Fable5 第2弾 案B).

結論ではなく「判断時点で何を知り得たか」の境界を保存し、結果ではなく過程を
教師にして学習する層。全体が opt-in で、未使用時は既存動作に一切触れない。
"""

from src.core.decision.package import (
    KNOWABLE_KINDS,
    InformationBoundary,
    build_package,
    classify_by_disclosure_time,
    load_package,
    save_package,
    list_packages,
    verify_package,
)
from src.core.decision.review import (
    OUTCOME_ORIGIN,
    PROCESS_ORIGIN,
    ReviewOrderViolation,
    attach_outcome,
    process_review,
)

__all__ = [
    "KNOWABLE_KINDS",
    "InformationBoundary",
    "build_package",
    "classify_by_disclosure_time",
    "load_package",
    "save_package",
    "list_packages",
    "verify_package",
    "OUTCOME_ORIGIN",
    "PROCESS_ORIGIN",
    "ReviewOrderViolation",
    "attach_outcome",
    "process_review",
]
