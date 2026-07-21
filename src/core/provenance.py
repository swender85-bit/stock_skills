"""認識の系譜会計 -- 自己参照汚染の遮断 (Fable5 第2弾 案C).

## 隠れた問題

KIK-466（過去情報と現在情報の統合解釈）は Stock Skills の看板機能だが、
同時に**汚染経路**でもある。

レポートが保存され、次のレポートが過去レポートを解釈に使い、その解釈がまた保存される。
時間とともにシステムの「知識」に占める自己生成物の比率が単調増加し、
一次観測由来の主張と、自分の過去の推論由来の主張が同格で混入する。

plan-execute の複数エージェントも同じ記憶を共有するため、その合意は
独立検証ではなく**同一汚染源の反響**になり得る。

記憶が優秀であるほど、運用が長いほど悪化する構造病であり、短期テストでは顕在化しない。

## 設計原理

すべての保存主張に系譜（provenance）を型として付与し、**自己参照深度**
——この結論は何世代の自己推論の上に立つか——を会計する。
深度が閾値を超えた主張は、一次情報からの再導出（再接地）なしに新しい解釈へ使えない。

記憶の強化ではなく、**記憶の使用権の設計**である。

## 非破壊

既存ノードは provenance=legacy で無変換保持する（遡及型付けは捏造リスクがある）。
型付け義務は新規書込のみ。汚染度は警告であり、レポート出力自体は止めない。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

from src.core.temporal import stamp_for_symbol


_CLAIMS_DIR = "data/claims"

# ---------------------------------------------------------------------------
# 四系譜（排他）
# ---------------------------------------------------------------------------

#: 一次観測 — 価格・開示原文・IR原文。深度0の錨。
PRIMARY = "primary_observation"
#: 外部言説 — ニュース・センチメント・アナリスト見解。
EXTERNAL = "external_discourse"
#: 自己推論 — このシステム自身の解釈。汚染源。
SELF = "self_inference"
#: ユーザー言明 — テーゼ・投資メモ。
USER = "user_statement"
#: 案C導入以前の既存ノード。遡及型付けはしない。
LEGACY = "legacy"

PROVENANCE_TYPES = (PRIMARY, EXTERNAL, SELF, USER)
ALL_PROVENANCE = PROVENANCE_TYPES + (LEGACY,)

PROVENANCE_LABELS = {
    PRIMARY: "一次観測",
    EXTERNAL: "外部言説",
    SELF: "自己推論",
    USER: "ユーザー言明",
    LEGACY: "型付け前(legacy)",
}

#: 深度0の錨とみなす情報源ドメイン。
#: 型付けは取得元で機械判定し、自己申告を認めない（系譜偽装の防止）。
PRIMARY_DOMAINS = (
    "edinet-fsa.go.jp", "disclosure.edinet-fsa.go.jp",
    "tdnet.info", "release.tdnet.info",
    "sec.gov", "www.sec.gov",
    "boj.or.jp", "federalreserve.gov",
    "jpx.co.jp",
)

#: 他者集計（加工済み指標）。一次ではなく外部言説の深度1として扱う。
AGGREGATOR_DOMAINS = ("finance.yahoo.com", "finnhub.io", "query1.finance.yahoo.com")

#: 深度閾値。保有銘柄は厳格、ウォッチは緩和の二段。
DEPTH_LIMIT_HOLDING = 3
DEPTH_LIMIT_WATCH = 4


class UntypedClaimError(ValueError):
    """系譜の型が無い / 不正な主張の書込 (案C P2)。"""


class CircularDerivationError(ValueError):
    """循環導出 (A→B→A)。深度が無限になるため作成時に拒否する。"""


def classify_source(url_or_domain: str) -> str:
    """取得元ドメインから系譜を機械判定する。

    自己申告を認めないのは、又聞きニュースを一次情報と偽装されると
    深度会計そのものが無意味になるため。
    """
    if not url_or_domain:
        return EXTERNAL
    lowered = str(url_or_domain).lower()
    if any(d in lowered for d in PRIMARY_DOMAINS):
        return PRIMARY
    if any(d in lowered for d in AGGREGATOR_DOMAINS):
        return EXTERNAL
    return EXTERNAL


def build_claim(
    text: str,
    provenance: str,
    symbol: str = "",
    derived_from: Optional[list[dict]] = None,
    source: str = "",
    at: Any = None,
) -> dict:
    """主張を系譜つきで構築する (案C P1/P3)。

    深度の規則:
      - 一次観測は常に深度0（錨）
      - 外部言説・ユーザー言明は深度1（他者/本人の言明であって自己推論ではない）
      - 自己推論は 祖先の最大深度 + 1

    Raises
    ------
    UntypedClaimError
        provenance が無い / 四系譜のいずれでもない。
    CircularDerivationError
        祖先に自分自身が含まれる。
    """
    if not provenance:
        raise UntypedClaimError(
            "系譜(provenance)の無い主張は保存できません。"
            f"次のいずれかを指定してください: {', '.join(PROVENANCE_TYPES)}"
        )
    if provenance not in ALL_PROVENANCE:
        raise UntypedClaimError(
            f"未知の系譜です: 「{provenance}」。"
            f"使える系譜: {', '.join(PROVENANCE_TYPES)}"
        )

    ancestors = list(derived_from or [])
    ts = stamp_for_symbol(symbol, at=at)
    claim_id = f"claim_{ts['utc'][:10]}_{uuid.uuid4().hex[:8]}"

    ancestor_ids = [a.get("id", "") for a in ancestors]
    if claim_id in ancestor_ids:
        raise CircularDerivationError("主張が自分自身から導出されています。")

    depth = _compute_depth(provenance, ancestors)

    return {
        "id": claim_id,
        "text": text,
        "symbol": symbol,
        "provenance": provenance,
        "depth": depth,
        "derived_from": ancestor_ids,
        "source": source,
        "created_at": ts,
        "regrounded_at": None,
    }


def _compute_depth(provenance: str, ancestors: list[dict]) -> int:
    """自己参照深度を計算する。"""
    if provenance == PRIMARY:
        return 0
    if provenance in (EXTERNAL, USER):
        return 1
    if provenance == LEGACY:
        # 型付け前のノードは深度が分からない。0(=一次)と誤認させないため
        # 便宜的に1を置き、構成比では legacy として別掲する。
        return 1
    # self_inference: 祖先の最大深度 + 1
    if not ancestors:
        # 根拠を持たない自己推論。一次情報に接地していないので深度1から始める。
        return 1
    return max(int(a.get("depth", 0)) for a in ancestors) + 1


def link_derivation(claim: dict, ancestor: dict, all_claims: Iterable[dict]) -> dict:
    """導出エッジを追加する。循環はエッジ作成時に検出して拒否する (案C P3)。"""
    index = {c["id"]: c for c in all_claims}
    if _reaches(ancestor.get("id", ""), claim.get("id", ""), index):
        raise CircularDerivationError(
            f"循環導出です: {ancestor.get('id')} は既に {claim.get('id')} に依存しています。"
        )

    claim.setdefault("derived_from", []).append(ancestor["id"])
    index[claim["id"]] = claim
    claim["depth"] = _compute_depth(
        claim.get("provenance", SELF),
        [index[a] for a in claim["derived_from"] if a in index],
    )
    return claim


def _reaches(start_id: str, target_id: str, index: dict[str, dict]) -> bool:
    """start から derived_from を辿って target に到達するか（循環検出用）。"""
    seen: set[str] = set()
    stack = [start_id]
    while stack:
        current = stack.pop()
        if current == target_id:
            return True
        if current in seen:
            continue
        seen.add(current)
        node = index.get(current)
        if node:
            stack.extend(node.get("derived_from") or [])
    return False


def trace_to_primary(claim: dict, all_claims: Iterable[dict]) -> list[dict]:
    """主張の根拠を一次情報まで遡る (graph-query「この結論の根拠を遡る」照会)。

    Returns
    -------
    list of dict
        到達した一次観測の主張。空なら独立した一次根拠が無い。
    """
    index = {c["id"]: c for c in all_claims}
    found: list[dict] = []
    seen: set[str] = set()
    stack = list(claim.get("derived_from") or [])

    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        node = index.get(current)
        if node is None:
            continue
        if node.get("provenance") == PRIMARY:
            found.append(node)
        stack.extend(node.get("derived_from") or [])

    return found


def provenance_summary(claims: Iterable[dict]) -> dict:
    """解釈の系譜構成比と最大深度を出す (案C P4).

    レポート末尾に常設される「本解釈の根拠: 一次観測62%／外部言説21%／自己推論17%
    （最大深度2）」の材料。
    """
    claims = list(claims)
    total = len(claims)
    if total == 0:
        return {
            "total": 0,
            "composition": {},
            "max_depth": 0,
            "self_ratio": 0.0,
            "contaminated": False,
            "summary": "根拠となる主張が記録されていません。",
        }

    counts: dict[str, int] = {}
    for c in claims:
        p = c.get("provenance", LEGACY)
        counts[p] = counts.get(p, 0) + 1

    composition = {k: v / total for k, v in counts.items()}
    max_depth = max(int(c.get("depth", 0)) for c in claims)
    self_ratio = composition.get(SELF, 0.0)

    # 自己推論が過半、または深度が閾値超え → 独立根拠が薄い
    contaminated = self_ratio > 0.5 or max_depth >= DEPTH_LIMIT_HOLDING

    parts = [
        f"{PROVENANCE_LABELS.get(k, k)}{v * 100:.0f}%"
        for k, v in sorted(composition.items(), key=lambda kv: kv[1], reverse=True)
    ]
    summary = f"本解釈の根拠: {'／'.join(parts)}（最大深度{max_depth}）"
    if contaminated:
        summary += (
            "\n⚠️ 本見解は当システムの過去解釈への依存が強く、独立根拠が薄い。"
            "一次情報からの再導出を推奨する。"
        )

    return {
        "total": total,
        "composition": {k: round(v, 4) for k, v in composition.items()},
        "max_depth": max_depth,
        "self_ratio": round(self_ratio, 4),
        "contaminated": contaminated,
        "summary": summary,
    }


def needs_regrounding(claim: dict, is_holding: bool = True) -> bool:
    """この主張は再接地なしに新しい解釈へ使えるか (案C P5)。

    深度閾値は保有銘柄=厳格、ウォッチ=緩和の二段。
    再接地済み（regrounded_at あり）なら深度に関わらず使用可。
    """
    if claim.get("regrounded_at"):
        return False
    limit = DEPTH_LIMIT_HOLDING if is_holding else DEPTH_LIMIT_WATCH
    return int(claim.get("depth", 0)) >= limit


def filter_usable(
    claims: Iterable[dict], is_holding: bool = True
) -> tuple[list[dict], list[dict]]:
    """新しい解釈に使える主張と、再接地が必要な主張に分ける (案C P5)。

    Returns
    -------
    (usable, needs_reground)
    """
    usable: list[dict] = []
    blocked: list[dict] = []
    for c in claims:
        (blocked if needs_regrounding(c, is_holding) else usable).append(c)
    return usable, blocked


def reground(claim: dict, primary_claim: dict, at: Any = None) -> dict:
    """主張を一次情報から再導出して深度をリセットする。

    錨が一次観測でなければ拒否する（又聞きへの接地は再接地ではない）。
    """
    if primary_claim.get("provenance") != PRIMARY:
        raise UntypedClaimError(
            "再接地の錨は一次観測(primary_observation)でなければなりません。"
            f"渡された系譜: {primary_claim.get('provenance')}"
        )

    ts = stamp_for_symbol(claim.get("symbol", ""), at=at)
    claim["derived_from"] = [primary_claim["id"]]
    claim["depth"] = 1
    claim["regrounded_at"] = ts
    return claim


def regrounding_queue(
    claims: Iterable[dict], is_holding: bool = True
) -> list[dict]:
    """再接地の優先度付きキュー（怠惰評価: 深い順）。

    現在アクティブな解釈に使われる主張のみを対象にすることで、
    全 legacy 資産の再導出という非現実的なコストを避ける。
    """
    _usable, blocked = filter_usable(claims, is_holding)
    return sorted(blocked, key=lambda c: int(c.get("depth", 0)), reverse=True)


# ---------------------------------------------------------------------------
# 永続化
# ---------------------------------------------------------------------------


def save_claim(claim: dict, base_dir: str = _CLAIMS_DIR) -> Path:
    d = Path(base_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{claim['id']}.json"
    path.write_text(json.dumps(claim, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_claims(
    symbol: Optional[str] = None, base_dir: str = _CLAIMS_DIR
) -> list[dict]:
    d = Path(base_dir)
    if not d.exists():
        return []
    out: list[dict] = []
    for path in d.glob("claim_*.json"):
        try:
            claim = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if symbol and claim.get("symbol") != symbol:
            continue
        out.append(claim)
    out.sort(key=lambda c: c.get("created_at", {}).get("utc", ""), reverse=True)
    return out


def claims_from_decision_package(package: dict) -> list[dict]:
    """判断パッケージを Claim に変換する (案B → 案C の供給経路).

    可知集合の used が根拠、結論が自己推論。DecisionPackage を Claim の
    最初の供給源にすることで、系譜台帳を二重実装せずに立ち上げられる。
    """
    symbol = package.get("symbol", "")
    boundary = package.get("information_boundary") or {}

    evidence: list[dict] = []
    for item in boundary.get("used") or []:
        label = str(item.get("label", "")).strip()
        if not label:
            continue
        provenance = (
            PRIMARY if item.get("disclosed_at") else classify_source(item.get("source", ""))
        )
        evidence.append(
            build_claim(
                text=label,
                provenance=provenance,
                symbol=symbol,
                source=item.get("source", ""),
            )
        )

    conclusion = build_claim(
        text=package.get("rationale") or package.get("decision", ""),
        provenance=SELF,
        symbol=symbol,
        derived_from=evidence,
        source=f"decision_package:{package.get('id', '')}",
    )

    return evidence + [conclusion]
