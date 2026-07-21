"""過程再審と結果ログ -- 運と技能の分離 (案B P3/P4).

運用順序は厳格である:

    build_package  →  process_review(結果を見ない)  →  封印  →  attach_outcome

結果を先に見てから再審すると後知恵が混入する。本モジュールはその順序違反を
実行時に検出して拒否する(``ReviewOrderViolation``)。

lesson は二層に分離される:
  process origin : 可知情報の見落とし・論理誤り → plan-check の制約になる資格を持つ
  outcome origin : 運の記録 → 制約にならず統計としてのみ蓄積
"""

from __future__ import annotations

from typing import Any, Optional

from src.core.decision.package import _seal_package, verify_package
from src.core.temporal import stamp_for_symbol


#: lesson の出自。plan-check の制約になれるのは PROCESS_ORIGIN のみ。
PROCESS_ORIGIN = "process"
OUTCOME_ORIGIN = "outcome"
#: 案B導入以前の既存lesson。無変換保持し、出自不明として扱う。
LEGACY_ORIGIN = "legacy"

VALID_ORIGINS = (PROCESS_ORIGIN, OUTCOME_ORIGIN, LEGACY_ORIGIN)


class ReviewOrderViolation(RuntimeError):
    """運用順序違反(結果を見てからの再審、二重再審など)。"""


def process_review(
    package: dict,
    findings: Optional[list[dict]] = None,
    reviewer: str = "auto",
    at: Any = None,
) -> dict:
    """結果を見ない状態で過程だけを審査し、パッケージを封印する (案B P3).

    可知集合の ``available_unused``(入手可能だったのに使わなかった情報)が
    過程誤りの候補になる。``unknowable`` は運であり、ここでは lesson を生成しない。

    Parameters
    ----------
    package : dict
        ``build_package`` が返した未封印パッケージ。
    findings : list of dict, optional
        人手/エージェントによる追加の過程指摘。各要素は ``label`` と ``detail``。
    reviewer : str
        審査者(例: "auto", "judge", "human")。

    Returns
    -------
    dict
        再審結果を格納して封印済みになったパッケージ(同一オブジェクト)。

    Raises
    ------
    ReviewOrderViolation
        結果が既に添付されている / 既に再審済みの場合。
    """
    if package.get("outcome") is not None:
        raise ReviewOrderViolation(
            "結果が既に記録されています。結果を見た後の過程再審は後知恵で汚染されるため実行できません。"
        )
    if package.get("process_review") is not None:
        raise ReviewOrderViolation(
            "このパッケージは既に再審・封印済みです。再審は結果確定前の一度に限られます。"
        )

    boundary = package.get("information_boundary") or {}
    missed = list(boundary.get("available_unused") or [])
    luck = list(boundary.get("unknowable") or [])

    lessons: list[dict] = []
    for item in missed:
        label = str(item.get("label", "")).strip() or "(無題の情報)"
        lessons.append(
            {
                "origin": PROCESS_ORIGIN,
                "trigger": f"{package.get('symbol', '')} の判断時に「{label}」が開示済みだった",
                "expected_action": f"次回は判断前に「{label}」を確認する",
                "detail": item.get("detail", ""),
            }
        )

    for finding in findings or []:
        label = str(finding.get("label", "")).strip() or "(無題の指摘)"
        lessons.append(
            {
                "origin": PROCESS_ORIGIN,
                "trigger": finding.get("trigger") or label,
                "expected_action": finding.get("expected_action")
                or f"次回は「{label}」を検討に含める",
                "detail": finding.get("detail", ""),
            }
        )

    package["process_review"] = {
        "reviewed_at": stamp_for_symbol(package.get("symbol", ""), at=at),
        "reviewer": reviewer,
        "missed_count": len(missed),
        "unknowable_count": len(luck),
        # 可知集合内に誤りがなければ「過程は正しい」と根拠を持って言える
        "process_sound": len(lessons) == 0,
        "lessons": lessons,
    }

    return _seal_package(package)


def attach_outcome(
    package: dict,
    return_pct: Optional[float] = None,
    note: str = "",
    at: Any = None,
) -> dict:
    """結果を「結果ログ」として記録する。lesson の教師にはしない (案B P4).

    封印済みパッケージに対してのみ実行できる。結果は統計としてのみ蓄積され、
    plan-check の制約には昇格しない。

    Raises
    ------
    ReviewOrderViolation
        未再審 / 未封印 / 既に結果記録済みの場合。
    ValueError
        封印が破られている(改変された)場合。
    """
    if package.get("process_review") is None:
        raise ReviewOrderViolation(
            "過程再審の前に結果を記録することはできません。先に process_review を実行してください。"
        )
    if package.get("outcome") is not None:
        raise ReviewOrderViolation("結果は既に記録済みです。")
    if not verify_package(package):
        raise ValueError(
            "封印が検証できません。パッケージが改変されたか、未封印の可能性があります。"
        )

    # 封印値を保ったまま結果だけを外付けする。封印は判断時点の内容に対する
    # 証明なので、結果は封印対象から外した領域に置く。
    package["outcome"] = {
        "recorded_at": stamp_for_symbol(package.get("symbol", ""), at=at),
        "return_pct": return_pct,
        "note": note,
        "origin": OUTCOME_ORIGIN,
        # 過程健全 × 結果不良 は「運」。統計偏在の監視対象になる。
        "process_sound": bool(package["process_review"].get("process_sound")),
    }
    return package


def sealed_body_intact(package: dict) -> bool:
    """結果添付後も、封印対象である判断時点の内容が無傷かを確認する。"""
    if not package.get("sealed"):
        return False
    # 封印は「結果を知らない時点の内容」に対する証明。結果欄を封印時の値(None)に
    # 戻して検証する。キーごと落とすと封印時の本体と形が変わり不一致になる。
    probe = dict(package)
    probe["outcome"] = None
    from src.core.temporal import verify_seal

    return verify_seal(probe)


def luck_skill_stats(packages: list[dict]) -> dict:
    """過程健全性 × 結果 のクロス集計 (案B 抑制設計).

    「過程正当 × 結果不良」が偏在するなら、可知集合の外に構造要因がある可能性。
    運と呼んだものを再分類する経路を残すための統計。
    """
    cells = {
        "sound_good": 0,
        "sound_bad": 0,
        "flawed_good": 0,
        "flawed_bad": 0,
    }
    scored = 0
    for pkg in packages:
        outcome = pkg.get("outcome")
        review = pkg.get("process_review")
        if not outcome or not review or outcome.get("return_pct") is None:
            continue
        scored += 1
        sound = bool(review.get("process_sound"))
        good = float(outcome["return_pct"]) >= 0
        key = f"{'sound' if sound else 'flawed'}_{'good' if good else 'bad'}"
        cells[key] += 1

    total_sound = cells["sound_good"] + cells["sound_bad"]
    warn = bool(total_sound >= 5 and cells["sound_bad"] / total_sound > 0.7)

    return {
        "scored": scored,
        **cells,
        # 過程健全なのに結果不良が7割超 → 「運」の再分類を促す警報
        "structural_factor_warning": warn,
    }
