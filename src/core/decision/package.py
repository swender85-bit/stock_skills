"""Decision package: 判断パッケージの構築・封印・永続化 (案B P1/P2/P5).

判断パッケージ = 結論 + 可知集合 + 検討した代替案 + 宣言確信度。

可知集合(InformationBoundary)は3つに排他分割される:
  used            : 判断に実際に使用した情報
  available_unused: 判断時点で入手可能だったが使わなかった情報  ← 見落とし=過程誤り
  unknowable      : 判断時点では原理的に不可知だった情報        ← 運

この境界が保存されていないと、後からの検証は必ず後知恵で汚染される。
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Optional

from src.core.temporal import (
    apply_seal,
    compare_instants,
    market_of,
    stamp_for_symbol,
    verify_seal,
)


_PACKAGES_DIR = "data/decisions"

#: 可知集合の3区分。排他かつ網羅。
KNOWABLE_KINDS = ("used", "available_unused", "unknowable")


def _resolve_base_dir(base_dir: Optional[str] = None) -> str:
    """保存先を解決する。明示指定 > 環境変数 > 既定。

    ``DECISION_PACKAGES_DIR`` で差し替えられるのは、テストが実データの
    ``data/decisions/`` を汚さないようにするため（実際に統合テストの
    売買がマスターへ書き込んでいた）。
    """
    if base_dir:
        return base_dir
    return os.environ.get("DECISION_PACKAGES_DIR", "").strip() or _PACKAGES_DIR


def _packages_dir(base_dir: Optional[str] = None) -> Path:
    d = Path(_resolve_base_dir(base_dir))
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class InformationBoundary:
    """判断時点の可知集合。

    各要素は dict で、少なくとも ``label`` を持つ。開示時刻が分かる場合は
    ``disclosed_at`` (ISO8601, tz付き) を持たせると機械判定に載る。
    """

    used: list[dict] = field(default_factory=list)
    available_unused: list[dict] = field(default_factory=list)
    unknowable: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "used": list(self.used),
            "available_unused": list(self.available_unused),
            "unknowable": list(self.unknowable),
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "InformationBoundary":
        data = data or {}
        return cls(
            used=list(data.get("used") or []),
            available_unused=list(data.get("available_unused") or []),
            unknowable=list(data.get("unknowable") or []),
        )

    def overlaps(self) -> list[str]:
        """同一ラベルが複数区分に現れていないか検査する(排他性の検証)。"""
        seen: dict[str, str] = {}
        dupes: list[str] = []
        for kind in KNOWABLE_KINDS:
            for item in getattr(self, kind):
                label = str(item.get("label", "")).strip()
                if not label:
                    continue
                if label in seen and seen[label] != kind:
                    dupes.append(label)
                else:
                    seen[label] = kind
        return sorted(set(dupes))


def classify_by_disclosure_time(
    items: Iterable[dict],
    decided_at: Any,
    used_labels: Optional[Iterable[str]] = None,
) -> InformationBoundary:
    """開示タイムスタンプを使って可知集合を機械的に切り分ける (案B P5).

    判断時刻より前に開示されていた情報は「入手可能」であり、使っていなければ
    見落とし(過程誤り)。判断後の開示は「不可知」であり運。時刻はいずれも UTC に
    正規化してから比較するため、市場タイムゾーンをまたいでも前後が反転しない。

    Parameters
    ----------
    items : iterable of dict
        各要素は ``label`` と、可能なら ``disclosed_at`` を持つ。
    decided_at : str | datetime | dict
        判断時刻。``stamp()`` の返り値をそのまま渡してよい。
    used_labels : iterable of str, optional
        実際に判断へ使用した情報のラベル集合。

    Returns
    -------
    InformationBoundary
    """
    used_set = {str(x) for x in (used_labels or [])}
    boundary = InformationBoundary()

    for item in items:
        label = str(item.get("label", "")).strip()
        disclosed_at = item.get("disclosed_at")
        order = compare_instants(disclosed_at, decided_at)

        if order is None:
            # 開示時刻が不明なものは可知と断定できない。運側に倒して
            # 「見落とし」の誤検出(=誤ったlesson生成)を防ぐ。
            enriched = dict(item)
            enriched["unclassified_reason"] = "disclosure time unknown"
            boundary.unknowable.append(enriched)
            continue

        if order > 0:
            # 判断より後に開示 → 原理的に不可知(運)
            boundary.unknowable.append(dict(item))
        elif label in used_set:
            boundary.used.append(dict(item))
        else:
            boundary.available_unused.append(dict(item))

    return boundary


def build_package(
    symbol: str,
    decision: str,
    rationale: str = "",
    boundary: Optional[InformationBoundary | dict] = None,
    alternatives: Optional[list[dict]] = None,
    confidence: Optional[float] = None,
    source: str = "manual",
    decided_at: Any = None,
) -> dict:
    """判断パッケージを構築して封印する (案B P1).

    Parameters
    ----------
    symbol : str
        対象ティッカー。市場タイムゾーンの決定にも使う。
    decision : str
        結論(例: "buy", "sell", "hold", "policy-change")。
    rationale : str
        判断理由の要約。
    boundary : InformationBoundary | dict, optional
        可知集合。
    alternatives : list of dict, optional
        検討した代替案。各要素は ``label`` と ``rejected_because`` を推奨。
    confidence : float, optional
        宣言確信度 0.0-1.0。校正(過信/過小信の検出)に使う。
    source : str
        生成元(例: "buy", "sell", "plan-execute", "manual")。
    decided_at : optional
        判断時刻。省略時は現在時刻。

    Returns
    -------
    dict
        封印済みパッケージ。``seal`` キーにハッシュを持つ。
    """
    if not symbol:
        raise ValueError("symbol is required for a decision package")
    if not decision:
        raise ValueError("decision is required for a decision package")
    if confidence is not None and not (0.0 <= float(confidence) <= 1.0):
        raise ValueError("confidence must be between 0.0 and 1.0")

    bound = (
        boundary
        if isinstance(boundary, InformationBoundary)
        else InformationBoundary.from_dict(boundary)
    )
    overlaps = bound.overlaps()
    if overlaps:
        raise ValueError(
            "information boundary kinds must be exclusive; duplicated labels: "
            + ", ".join(overlaps)
        )

    ts = stamp_for_symbol(symbol, at=decided_at)
    today = ts["utc"][:10]
    package = {
        "id": f"dp_{today}_{symbol.replace('.', '_')}_{uuid.uuid4().hex[:8]}",
        "symbol": symbol,
        "market": market_of(symbol),
        "decision": decision,
        "rationale": rationale,
        "decided_at": ts,
        "information_boundary": bound.to_dict(),
        "alternatives": list(alternatives or []),
        "confidence": confidence,
        "source": source,
        # 過程再審と結果はここに後から入る。封印は再審完了時に張り直す。
        "process_review": None,
        "outcome": None,
        "sealed": False,
    }
    return package


def save_package(package: dict, base_dir: Optional[str] = None) -> Path:
    """パッケージをJSONで永続化する。JSONがmaster、Neo4jはview。"""
    path = _packages_dir(base_dir) / f"{package['id']}.json"
    path.write_text(
        json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def load_package(package_id: str, base_dir: Optional[str] = None) -> Optional[dict]:
    """IDでパッケージを読み込む。存在しなければ None。"""
    path = Path(_resolve_base_dir(base_dir)) / f"{package_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_packages(
    symbol: Optional[str] = None, base_dir: Optional[str] = None
) -> list[dict]:
    """保存済みパッケージを新しい順に返す。symbol 指定で絞り込む。"""
    d = Path(_resolve_base_dir(base_dir))
    if not d.exists():
        return []
    packages: list[dict] = []
    for path in d.glob("dp_*.json"):
        try:
            pkg = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if symbol and pkg.get("symbol") != symbol:
            continue
        packages.append(pkg)
    packages.sort(key=lambda p: p.get("decided_at", {}).get("utc", ""), reverse=True)
    return packages


def verify_package(package: dict) -> bool:
    """封印済みパッケージが改変されていないか検証する (案B 受け入れ基準P1)。

    未封印のパッケージは False を返す。
    """
    if not package.get("sealed"):
        return False
    return verify_seal(package)


def _seal_package(package: dict) -> dict:
    """内部用: パッケージを封印する。"""
    package["sealed"] = True
    return apply_seal(package)
