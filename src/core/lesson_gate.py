"""Lesson gate -- 記憶を条件付き債務として扱う検証ゲート (Fable5 第1弾 案3 + 第2弾 案B P4).

lesson が plan-check の制約として「黙って効く」系から、「適用のたびに自らの
有効性を証明する」系へ転換するためのゲート。2つの独立した関門を通す:

関門1: 出自 (案B)
    process 由来のみが制約になる資格を持つ。outcome 由来(運の記録)は
    統計にはなるが制約にはならない。legacy(導入前の既存lesson)は
    互換のため通すが、その旨を明示する。

関門2: 有効期限 (案3)
    validity envelope(生成時レジーム・想定有効期間)と現在市況を照合し、
    適用 / 警告付き適用 / 棚上げ に仕分ける。

いずれも graceful degradation する。envelope 未設定の既存 lesson は
従来どおり「適用」になるため、非破壊。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from src.core.decision.review import (
    LEGACY_ORIGIN,
    OUTCOME_ORIGIN,
    PROCESS_ORIGIN,
)


#: ゲート判定
VERDICT_APPLY = "apply"
VERDICT_WARN = "apply_with_warning"
VERDICT_SHELVE = "shelve"

_VERDICT_LABEL = {
    VERDICT_APPLY: "適用",
    VERDICT_WARN: "警告付き適用",
    VERDICT_SHELVE: "棚上げ",
}


def verdict_label(verdict: str) -> str:
    """判定の日本語ラベル。"""
    return _VERDICT_LABEL.get(verdict, verdict)


def lesson_origin(lesson: dict) -> str:
    """lesson の出自を返す。未設定は legacy(導入前の既存lesson)とみなす。"""
    origin = (lesson.get("origin") or "").strip().lower()
    if origin in (PROCESS_ORIGIN, OUTCOME_ORIGIN, LEGACY_ORIGIN):
        return origin
    return LEGACY_ORIGIN


def is_constraint_eligible(lesson: dict) -> bool:
    """plan-check の制約になる資格があるか (案B 受け入れ基準P4).

    outcome 由来(結果から学んだ=運を教師にした)lesson は資格を持たない。
    """
    return lesson_origin(lesson) != OUTCOME_ORIGIN


# ---------------------------------------------------------------------------
# validity envelope
# ---------------------------------------------------------------------------


def _parse_day(value: Any) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


def build_envelope(
    regime: Optional[list[str]] = None,
    valid_until: Any = None,
    conditions: Optional[dict] = None,
) -> dict:
    """validity envelope を作る。

    Parameters
    ----------
    regime : list of str, optional
        生成時のレジームタグ(例: ["low-rate", "yen-weak"])。
    valid_until : optional
        想定有効期限(ISO日付)。
    conditions : dict, optional
        生成時の市場条件スナップショット(VIX・金利など)。
    """
    return {
        "regime": list(regime or []),
        "valid_until": str(valid_until)[:10] if valid_until else None,
        "conditions": dict(conditions or {}),
    }


def evaluate_lesson(
    lesson: dict,
    current_regime: Optional[list[str]] = None,
    today: Optional[date] = None,
) -> dict:
    """lesson 1件をゲートに通し、判定と理由を返す。

    Returns
    -------
    dict
        {"verdict": ..., "label": ..., "origin": ..., "reasons": [...]}
    """
    reasons: list[str] = []
    origin = lesson_origin(lesson)

    # --- 関門1: 出自 ---
    if origin == OUTCOME_ORIGIN:
        return {
            "verdict": VERDICT_SHELVE,
            "label": verdict_label(VERDICT_SHELVE),
            "origin": origin,
            "reasons": ["結果由来(運の記録)のため制約にならない"],
        }
    if origin == LEGACY_ORIGIN:
        reasons.append("出自不明(案B導入前のlesson)")

    # --- 関門2: 有効期限 ---
    envelope = lesson.get("validity") or {}
    ref_day = today or date.today()

    valid_until = _parse_day(envelope.get("valid_until"))
    if valid_until is not None and ref_day > valid_until:
        reasons.append(f"想定有効期限 {valid_until.isoformat()} を経過")
        return {
            "verdict": VERDICT_SHELVE,
            "label": verdict_label(VERDICT_SHELVE),
            "origin": origin,
            "reasons": reasons,
        }

    lesson_regime = [str(r).strip().lower() for r in (envelope.get("regime") or []) if str(r).strip()]
    now_regime = [str(r).strip().lower() for r in (current_regime or []) if str(r).strip()]

    if lesson_regime and now_regime:
        overlap = set(lesson_regime) & set(now_regime)
        if not overlap:
            reasons.append(
                f"生成時レジーム({', '.join(lesson_regime)})が現在({', '.join(now_regime)})と一致しない"
            )
            return {
                "verdict": VERDICT_WARN,
                "label": verdict_label(VERDICT_WARN),
                "origin": origin,
                "reasons": reasons,
            }
        reasons.append(f"レジーム一致: {', '.join(sorted(overlap))}")
    elif lesson_regime and not now_regime:
        reasons.append("現在のレジーム不明のため照合できず")

    # 出自不明(legacy)は理由として明示するが、適用自体は止めない。
    # ここで警告に落とすと envelope 未設定の既存lesson が全件警告になり、
    # 非破壊条件(未使用時は従来動作と一致)を破る。
    return {
        "verdict": VERDICT_APPLY,
        "label": verdict_label(VERDICT_APPLY),
        "origin": origin,
        "reasons": reasons or ["有効"],
    }


def gate_lessons(
    lessons: list[dict],
    current_regime: Optional[list[str]] = None,
    today: Optional[date] = None,
    drop_shelved: bool = True,
) -> tuple[list[dict], list[dict]]:
    """lesson 群をゲートに通す。

    Returns
    -------
    (passed, shelved)
        passed は各 lesson に ``_gate`` キーで判定を添えたもの。
        drop_shelved=False なら棚上げも passed に含める(表示用途)。
    """
    passed: list[dict] = []
    shelved: list[dict] = []
    for lesson in lessons:
        result = evaluate_lesson(lesson, current_regime=current_regime, today=today)
        enriched = dict(lesson)
        enriched["_gate"] = result
        if result["verdict"] == VERDICT_SHELVE and drop_shelved:
            shelved.append(enriched)
        else:
            passed.append(enriched)
    return passed, shelved


def detect_current_regime() -> list[str]:
    """現在の市場レジームタグを推定する。取得できなければ空リスト。

    market_dashboard の定量指標(VIX / Fear&Greed)から粗いタグを起こす。
    別機構を増設せず、既存の KIK-427 鮮度判定と同じデータ源に相乗りする。
    """
    try:
        from src.core import market_dashboard as md
    except Exception:
        return []

    tags: list[str] = []
    try:
        getter = getattr(md, "get_dashboard", None) or getattr(md, "build_dashboard", None)
        if getter is None:
            return []
        data = getter() or {}
    except Exception:
        return []

    vix = _num(data.get("vix") or (data.get("indicators") or {}).get("vix"))
    if vix is not None:
        if vix >= 28:
            tags.append("high-vol")
        elif vix <= 15:
            tags.append("low-vol")

    fng = _num(data.get("fear_greed") or (data.get("indicators") or {}).get("fear_greed"))
    if fng is not None:
        if fng >= 70:
            tags.append("greed")
        elif fng <= 30:
            tags.append("fear")

    return tags


def _num(value: Any) -> Optional[float]:
    if isinstance(value, dict):
        value = value.get("value")
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
