"""外部言説の較正重み -- 情報源の過去的中率をドメイン別に持つ (改善5).

## 名指しする問題

系譜会計（`provenance.py`）は主張を四系譜に分ける:

    一次観測 / 外部言説 / ユーザー言明 / 自己推論

だが **「外部言説」に、その情報源が過去どれだけ当たったかという重みがない。**
同じ「外部言説」でも、需給読みで9回当てた情報源と、初出の情報源が同じ扱いになる。
結果として、レポートは「誰が言ったか」を区別せずに引用する。

## 設計

**採点は主張ごと、重みはドメインごと。** 一人の批評家が需給には強く価格水準には
弱い、という実態は珍しくない。情報源そのものを「信頼できる／できない」で
二値化すると、この構造が潰れて使い物にならない。

## 現時点の運用（重要）

**判定への使用は保留する。** 採点済みのテーゼは十数件しかなく、ドメイン別の
重みを出すには足りない。`domain_weight()` は `available=False` を返し、
**「重みが低い」ではなく「まだ測れていない」**として扱われる。

台帳の形式を先に作るのは、蓄積が始まらないと永遠に使えるようにならないため。
効果が出るのは蓄積後であり、それを承知で入れている。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

_CRITICS_DIR = "data/critics"

#: 採点。**pending を「外れ」と数えない**（未検証と誤りは別物）。
SCORES: dict[str, float] = {
    "hit_exact": 1.0,      # 方向も水準も当たった
    "hit_direction": 0.7,  # 方向は当たった
    "partial": 0.4,        # 部分的に当たった
    "refuted": 0.0,        # 反証された
}
PENDING = "pending"
VALID_SCORES = tuple(SCORES) + (PENDING,)

#: ドメイン。批評家の得手不得手はここで分かれる。
DOMAINS: dict[str, str] = {
    "supply_demand": "需給・資金フロー",
    "price_level": "価格水準の断言",
    "timing": "タイミング",
    "fundamentals": "業績・ファンダメンタル",
    "macro": "マクロ・金融政策",
    "regulation": "規制・政策",
    "technology": "技術動向",
    "sentiment": "センチメント",
}

#: この件数に満たないドメインでは重みを出さない。
#: 少数の的中で「この人は当たる」と決めると、**偶然を実力と誤認する**。
MIN_SAMPLES = 5

#: 本文の根拠に使ってよい重みの下限（`.claude/rules/provenance.md` の規約）。
USABLE_WEIGHT = 0.6


class InvalidThesis(ValueError):
    """採点不能・不正な批評家テーゼ。"""


# ---------------------------------------------------------------------------
# 台帳
# ---------------------------------------------------------------------------


def _critics_dir(base_dir: str = _CRITICS_DIR) -> Path:
    d = Path(base_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def build_thesis(
    claim: str,
    domain: str,
    at: Any = None,
    score: str = PENDING,
    verified_on: Any = None,
    note: str = "",
) -> dict:
    """批評家の主張を1件つくる。

    Raises
    ------
    InvalidThesis
        未知のドメイン・未知の採点・採点済みなのに検証日が無い。
    """
    if domain not in DOMAINS:
        raise InvalidThesis(
            f"未知のドメインです: 「{domain}」。使えるドメイン: {', '.join(sorted(DOMAINS))}")
    if score not in VALID_SCORES:
        raise InvalidThesis(
            f"未知の採点です: 「{score}」。使える採点: {', '.join(VALID_SCORES)}")
    if score != PENDING and not verified_on:
        # 検証日の無い採点は、後から結果を見て付けた後知恵と区別できない
        raise InvalidThesis("採点するには検証日(verified_on)が必要です。")
    if not str(claim or "").strip():
        raise InvalidThesis("主張の本文が空です。")

    stamp = at or date.today().isoformat()
    return {
        "date": str(stamp)[:10],
        "claim": str(claim).strip(),
        "domain": domain,
        "score": score,
        "verified_on": str(verified_on)[:10] if verified_on else None,
        "note": note,
    }


def load_critic(source_id: str, base_dir: str = _CRITICS_DIR) -> dict:
    """情報源の台帳。無ければ空の台帳を返す（存在しないことをエラーにしない）。"""
    path = Path(base_dir) / f"{source_id}.json"
    if not path.exists():
        return {"source_id": source_id, "theses": [], "exists": False}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"source_id": source_id, "theses": [], "exists": False,
                "error": "台帳を読めませんでした"}
    data.setdefault("source_id", source_id)
    data.setdefault("theses", [])
    data["exists"] = True
    return data


def save_critic(critic: dict, base_dir: str = _CRITICS_DIR) -> Path:
    path = _critics_dir(base_dir) / f"{critic['source_id']}.json"
    payload = {k: v for k, v in critic.items() if k not in ("exists", "error")}
    payload["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def add_thesis(source_id: str, thesis: dict, base_dir: str = _CRITICS_DIR) -> dict:
    critic = load_critic(source_id, base_dir)
    critic["theses"].append(thesis)
    save_critic(critic, base_dir)
    return critic


def list_critics(base_dir: str = _CRITICS_DIR) -> list[str]:
    """台帳のある情報源。`_` 始まりは雛形なので実データとして数えない。"""
    d = Path(base_dir)
    if not d.exists():
        return []
    return sorted(p.stem for p in d.glob("*.json") if not p.stem.startswith("_"))


# ---------------------------------------------------------------------------
# 重み
# ---------------------------------------------------------------------------


def _scored(theses: Iterable[dict], domain: Optional[str] = None) -> list[dict]:
    out = []
    for t in theses or []:
        if t.get("score") in (None, PENDING):
            continue          # 未検証を「外れ」と数えない
        if t.get("score") not in SCORES:
            continue
        if domain and t.get("domain") != domain:
            continue
        out.append(t)
    return out


def domain_weight(
    source_id: str,
    domain: str,
    base_dir: str = _CRITICS_DIR,
    min_samples: int = MIN_SAMPLES,
) -> dict:
    """ドメイン別の的中率から重みを返す。

    Returns
    -------
    dict
        {"available", "weight", "samples", "pending", "usable", "reason"}

    **`available=False` は「重みが低い」ではなく「まだ測れていない」。**
    ここを 0.0 として扱うと、蓄積が無い情報源が「外れる情報源」に化ける。
    現時点では採点済みが十数件しかないので、ほとんどのドメインが False になる。
    """
    critic = load_critic(source_id, base_dir)
    all_in_domain = [t for t in critic["theses"] if t.get("domain") == domain]
    scored = _scored(all_in_domain)
    pending = len(all_in_domain) - len(scored)

    if len(scored) < min_samples:
        return {
            "available": False,
            "weight": None,
            "samples": len(scored),
            "pending": pending,
            "usable": False,
            "reason": (
                f"{DOMAINS.get(domain, domain)} の採点済みが {len(scored)}件で、"
                f"重みを出すには {min_samples}件必要です。"
                "**これは『当たらない』ではなく『まだ測れていない』です。**"
                "少数の的中で実力を判定すると、偶然を実力と誤認します。"
            ),
        }

    weight = sum(SCORES[t["score"]] for t in scored) / len(scored)
    return {
        "available": True,
        "weight": round(weight, 3),
        "samples": len(scored),
        "pending": pending,
        "usable": weight >= USABLE_WEIGHT,
        "reason": (
            f"{DOMAINS.get(domain, domain)}: 採点済み {len(scored)}件の平均 {weight:.2f}。"
            + ("本文の根拠に使えます。" if weight >= USABLE_WEIGHT else
               f"重みが {USABLE_WEIGHT} 未満のため、本文の根拠ではなく引用形式で書きます。")
        ),
    }


def profile(source_id: str, base_dir: str = _CRITICS_DIR) -> dict:
    """情報源のドメイン別プロフィール。得手不得手の構造を出す。"""
    critic = load_critic(source_id, base_dir)
    domains = {}
    for domain in sorted({t.get("domain") for t in critic["theses"] if t.get("domain")}):
        domains[domain] = domain_weight(source_id, domain, base_dir)
    scored = _scored(critic["theses"])
    return {
        "source_id": source_id,
        "exists": critic.get("exists", False),
        "name": critic.get("name"),
        "total": len(critic["theses"]),
        "scored": len(scored),
        "pending": len(critic["theses"]) - len(scored),
        "domains": domains,
        "any_usable": any(d.get("usable") for d in domains.values()),
    }


def citation_style(source_id: str, domain: str, base_dir: str = _CRITICS_DIR) -> dict:
    """この主張をレポートでどう書くべきか (`.claude/rules/provenance.md` の規約).

    > 外部言説をレポート本文の根拠に使えるのは、その情報源のそのドメインの重みが
    > 0.6 以上のときのみ。それ未満は「◯◯氏の見解（当該ドメインでの過去的中率: n/m）」
    > として引用形式で書く。
    """
    w = domain_weight(source_id, domain, base_dir)
    critic = load_critic(source_id, base_dir)
    label = critic.get("name") or source_id

    if not w["available"]:
        return {
            "style": "unverified",
            "usable_as_evidence": False,
            "prefix": f"{label}の見解（当該ドメインでの的中率は未測定・採点済み {w['samples']}件）",
            "note": "未検証の主張として明示する。的中率が低いという意味ではない。",
            "weight": None,
        }
    if w["usable"]:
        return {
            "style": "evidence",
            "usable_as_evidence": True,
            "prefix": f"{label}（{DOMAINS.get(domain, domain)}の的中率 {w['weight']:.2f}）",
            "note": "本文の根拠に使ってよい。",
            "weight": w["weight"],
        }
    return {
        "style": "quotation",
        "usable_as_evidence": False,
        "prefix": (f"{label}の見解（{DOMAINS.get(domain, domain)}での過去的中率 "
                   f"{w['weight']:.2f} / {w['samples']}件）"),
        "note": "本文の根拠にはせず、引用形式で書く。",
        "weight": w["weight"],
    }


# ---------------------------------------------------------------------------
# provenance との接続
# ---------------------------------------------------------------------------


def annotate_claim(
    claim: dict, source_id: str, domain: str, base_dir: str = _CRITICS_DIR
) -> dict:
    """外部言説の Claim に `source_id` と較正重みを付ける (改善5 → 案C の接続).

    **判定には使わない。** 重みは表示と引用形式の決定にのみ使い、
    Claim の系譜や深度は変えない（蓄積が足りないうちに判定へ流すと、
    測れていないものが「信頼できない」に化ける）。
    """
    from src.core.provenance import EXTERNAL

    if claim.get("provenance") != EXTERNAL:
        return claim

    w = domain_weight(source_id, domain, base_dir)
    claim["source_id"] = source_id
    claim["domain"] = domain
    claim["domain_weight"] = w["weight"]
    claim["weight_available"] = w["available"]
    claim["citation"] = citation_style(source_id, domain, base_dir)
    return claim
