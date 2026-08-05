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
import re
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


# ---------------------------------------------------------------------------
# X の発言を主張に落とす (改善5 の入力経路)
# ---------------------------------------------------------------------------

#: 発言の分野を推定するキーワード。**先入観を持たせないため、
#: 「この人は需給の人」ではなく「この発言は需給の話」として分類する。**
#: 一人の中でも分野によって的中率が違うので、人ではなく発言に付ける。
DOMAIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "supply_demand": ("需給", "資金流入", "資金流出", "空売り", "信用買い", "信用残",
                      "出来高", "板", "先物", "オプション", "ガンマ", "機関投資家",
                      "海外投資家", "自社株買い", "パッシブ", "リバランス"),
    "price_level": ("円まで", "ドルまで", "到達", "目標株価", "ターゲット", "$", "円台",
                    "まで上がる", "まで下がる", "指値", "節目"),
    "timing": ("いつ", "タイミング", "来週", "来月", "年内", "上期", "下期",
               "月末", "週明け", "寄り", "引け", "決算後", "決算前"),
    "fundamentals": ("決算", "業績", "売上", "利益", "営業利益", "eps", "per", "pbr",
                     "roe", "増収", "増益", "減収", "減益", "ガイダンス", "上方修正",
                     "下方修正", "受注", "在庫"),
    "macro": ("金利", "fomc", "利上げ", "利下げ", "インフレ", "cpi", "雇用統計",
              "日銀", "為替", "円安", "円高", "景気", "リセッション", "gdp", "国債"),
    "regulation": ("規制", "当局", "政府", "法案", "関税", "制裁", "独禁", "公取",
                   "sec", "訴訟", "承認"),
    "technology": ("ai", "半導体", "gpu", "hbm", "euv", "クラウド", "量子", "ev",
                   "バッテリー", "創薬", "5g", "6g", "データセンター"),
    "sentiment": ("織り込み", "過熱", "悲観", "楽観", "恐怖", "強気", "弱気",
                  "センチメント", "地合い", "空気", "熱狂", "投げ売り"),
}

#: 価格ターゲットの言明。自動採点できる唯一の形。
_PRICE_TARGET_RE = re.compile(
    r"(?:[\$＄]\s*([\d,]+(?:\.\d+)?)|([\d,]+(?:\.\d+)?)\s*(?:円|ドル))")

#: 方向の言明。価格が無くても上下は検証できる。
_UP_WORDS = ("上がる", "上昇", "買い", "強気", "反発", "上抜け", "続伸", "高値更新")
_DOWN_WORDS = ("下がる", "下落", "売り", "弱気", "急落", "下抜け", "続落", "安値更新")

#: 期間の言明。無ければ既定期間で検証する。
_HORIZON_PATTERNS: tuple[tuple[str, int], ...] = (
    ("年内", 180), ("来年", 365), ("半年", 180), ("3ヶ月", 90), ("3カ月", 90),
    ("1ヶ月", 30), ("1カ月", 30), ("今月", 30), ("来月", 45),
    ("来週", 7), ("週明け", 5), ("明日", 1),
)


def classify_domain(text: str) -> str:
    """発言の分野を推定する。

    複数該当したら**最も多くのキーワードが当たった分野**を採る。
    どれにも当たらなければ `sentiment`（相場観）に寄せる —— これは
    「分類できなかった」の逃げ場であり、精度を主張するものではない。
    """
    lowered = str(text or "").lower()
    scores = {
        domain: sum(1 for kw in keywords if kw in lowered)
        for domain, keywords in DOMAIN_KEYWORDS.items()
    }
    best = max(scores.items(), key=lambda kv: kv[1])
    return best[0] if best[1] > 0 else "sentiment"


def extract_verifiable(text: str, symbols: Optional[list[str]] = None,
                       default_horizon_days: int = 30) -> Optional[dict]:
    """自動採点できる形の言明を取り出す。

    採点できるのは **銘柄が特定できて、価格ターゲットか方向がある**ものだけ。
    それ以外（「地合いが悪い」等）は定性的で、機械では採点できない。
    **採点できないものを無理に採点しないのがここの役目。**

    Returns
    -------
    dict | None
        {"symbol", "kind": "price_target"|"direction", "target", "direction",
         "horizon_days"}
    """
    symbols = [s for s in (symbols or []) if s]
    if not symbols:
        return None
    text = str(text or "")

    horizon = default_horizon_days
    for word, days in _HORIZON_PATTERNS:
        if word in text:
            horizon = days
            break

    m = _PRICE_TARGET_RE.search(text)
    if m:
        raw = (m.group(1) or m.group(2) or "").replace(",", "")
        try:
            target = float(raw)
        except ValueError:
            target = None
        if target and target > 0:
            return {"symbol": symbols[0], "kind": "price_target",
                    "target": target, "direction": None, "horizon_days": horizon}

    up = any(w in text for w in _UP_WORDS)
    down = any(w in text for w in _DOWN_WORDS)
    if up != down:      # 両方あるときは判定しない（「売りが出たが上がる」等）
        return {"symbol": symbols[0], "kind": "direction", "target": None,
                "direction": "up" if up else "down", "horizon_days": horizon}
    return None


def post_to_thesis(post: dict, default_horizon_days: int = 30) -> dict:
    """X の発言1件を、採点待ちの主張に変換する。

    **要約しない。** 原文をそのまま `claim` に入れる。要約した時点で
    自己推論が混ざり、後から「本人が何と言ったか」を検証できなくなる。
    """
    text = str(post.get("text") or "").strip()
    thesis = build_thesis(
        claim=text,
        domain=classify_domain(text),
        at=post.get("posted_at") or date.today().isoformat(),
    )
    thesis["url"] = post.get("url") or ""
    thesis["symbols"] = list(post.get("symbols") or [])
    thesis["topic"] = post.get("topic") or ""
    thesis["source_kind"] = "x_post"
    verifiable = extract_verifiable(text, thesis["symbols"], default_horizon_days)
    if verifiable:
        thesis["verifiable"] = verifiable
        thesis["verify_after"] = _add_days(thesis["date"], verifiable["horizon_days"])
    return thesis


def _add_days(day: str, days: int) -> str:
    from datetime import timedelta

    try:
        return (date.fromisoformat(str(day)[:10]) + timedelta(days=days)).isoformat()
    except ValueError:
        return date.today().isoformat()


def _fingerprint(thesis: dict) -> str:
    """重複判定の鍵。URL があれば URL、無ければ 日付＋本文の先頭。"""
    url = str(thesis.get("url") or "").strip()
    if url:
        return f"url:{url}"
    return f"txt:{thesis.get('date')}:{str(thesis.get('claim') or '')[:80]}"


def ingest_posts(
    source_id: str,
    posts: list[dict],
    base_dir: str = _CRITICS_DIR,
    default_horizon_days: int = 30,
    name: str = "",
    apply: bool = True,
) -> dict:
    """発言を台帳に追記する。既にある発言は飛ばす。

    Returns
    -------
    dict
        {"added", "skipped", "verifiable", "theses"}
    """
    critic = load_critic(source_id, base_dir)
    if name and not critic.get("name"):
        critic["name"] = name
    existing = {_fingerprint(t) for t in critic["theses"]}

    added: list[dict] = []
    skipped = 0
    for post in posts or []:
        thesis = post_to_thesis(post, default_horizon_days)
        if _fingerprint(thesis) in existing:
            skipped += 1
            continue
        existing.add(_fingerprint(thesis))
        added.append(thesis)

    if apply and added:
        critic["theses"].extend(added)
        save_critic(critic, base_dir)

    return {
        "source_id": source_id,
        "added": len(added),
        "skipped": skipped,
        "verifiable": sum(1 for t in added if t.get("verifiable")),
        "theses": added,
    }


# ---------------------------------------------------------------------------
# 採点（自動採点できるものだけ）
# ---------------------------------------------------------------------------


def score_verifiable(
    thesis: dict, price_now: Optional[float], price_then: Optional[float]
) -> Optional[dict]:
    """検証期限が来た言明を実測で採点する。

    採点できないときは **None を返す**（`refuted` にしない）。
    価格が取れなかったことを「外れた」と記録すると、台帳が汚染される。

    Returns
    -------
    dict | None
        {"score", "verified_on", "evidence"}
    """
    v = thesis.get("verifiable") or {}
    if not v:
        return None
    if price_now is None or price_then is None or price_then == 0:
        return None

    change = (price_now - price_then) / price_then
    today = date.today().isoformat()

    if v["kind"] == "price_target":
        target = float(v["target"])
        # 目標が現値より上なら上昇予想、下なら下落予想とみなす
        wanted_up = target > price_then
        reached = price_now >= target if wanted_up else price_now <= target
        moved_right_way = (change > 0) if wanted_up else (change < 0)
        if reached:
            score = "hit_exact"
        elif moved_right_way and abs(change) >= 0.03:
            score = "hit_direction"
        elif moved_right_way:
            score = "partial"
        else:
            score = "refuted"
        evidence = (f"{v['symbol']}: 発言時 {price_then:.2f} → 検証時 {price_now:.2f} "
                    f"({change * 100:+.1f}%) / 目標 {target:.2f}")
    else:
        wanted_up = v.get("direction") == "up"
        moved_right_way = (change > 0) if wanted_up else (change < 0)
        if moved_right_way and abs(change) >= 0.05:
            score = "hit_direction"
        elif moved_right_way:
            score = "partial"
        else:
            score = "refuted"
        evidence = (f"{v['symbol']}: 発言時 {price_then:.2f} → 検証時 {price_now:.2f} "
                    f"({change * 100:+.1f}%) / 予想 {'上昇' if wanted_up else '下落'}")

    return {"score": score, "verified_on": today, "evidence": evidence}


def due_for_scoring(critic: dict, today: Optional[date] = None) -> list[dict]:
    """検証期限が来ていて、まだ採点していない言明。"""
    ref = (today or date.today()).isoformat()
    out = []
    for thesis in critic.get("theses") or []:
        if thesis.get("score") != PENDING:
            continue
        if not thesis.get("verifiable") or not thesis.get("verify_after"):
            continue
        if str(thesis["verify_after"]) <= ref:
            out.append(thesis)
    return out


def unscorable_count(critic: dict) -> int:
    """自動採点できない pending の件数。

    **これを0と混同しない。** 定性的な主張は機械では採点できないだけで、
    「採点対象が無い」わけではない。手で採点する対象が残っている。
    """
    return sum(1 for t in critic.get("theses") or []
               if t.get("score") == PENDING and not t.get("verifiable"))


# ---------------------------------------------------------------------------
# レポートへの供給
# ---------------------------------------------------------------------------


def build_external_views(
    days: int = 7,
    symbols: Optional[list[str]] = None,
    base_dir: str = _CRITICS_DIR,
    limit_per_source: int = 5,
    today: Optional[date] = None,
) -> dict:
    """レポートに載せる外部言説を組み立てる。

    **API は叩かない。** 台帳（`fetch_critics.py` が積んだもの）を読むだけ。
    パック生成のたびに X を叩くと、遅くて高くて、しかも毎回内容が変わる。

    各見解には `citation`（本文の根拠に使えるか／引用形式か／未測定か）が付く。
    重みが 0.6 未満、または未測定のものは **本文の根拠にしてはならない**。

    Returns
    -------
    dict
        {"available", "sources", "views", "by_symbol", "macro_views",
         "usable_count", "note"}
    """
    ref = today or date.today()
    cutoff = (ref - _timedelta(days=days)).isoformat()
    held = {s for s in (symbols or []) if s}

    sources = list_critics(base_dir)
    if not sources:
        return {
            "available": False,
            "sources": [],
            "views": [],
            "by_symbol": {},
            "macro_views": [],
            "usable_count": 0,
            "note": ("批評家の台帳がありません。**これは『誰も何も言っていない』では"
                     "ありません** — 取得していないだけです。"
                     "`python scripts/fetch_critics.py --apply` で取り込めます。"),
        }

    views: list[dict] = []
    for source_id in sources:
        critic = load_critic(source_id, base_dir)
        recent = [t for t in critic.get("theses") or []
                  if str(t.get("date") or "") >= cutoff]
        recent.sort(key=lambda t: str(t.get("date") or ""), reverse=True)
        for thesis in recent[:limit_per_source]:
            citation = citation_style(source_id, thesis.get("domain", ""), base_dir)
            views.append({
                "source_id": source_id,
                "name": critic.get("name") or source_id,
                "date": thesis.get("date"),
                "domain": thesis.get("domain"),
                "domain_label": DOMAINS.get(thesis.get("domain", ""), thesis.get("domain")),
                "claim": thesis.get("claim"),
                "url": thesis.get("url", ""),
                "symbols": thesis.get("symbols") or [],
                "provenance": "external_discourse",
                "citation": citation,
                "usable_as_evidence": citation["usable_as_evidence"],
                "verifiable": bool(thesis.get("verifiable")),
                "score": thesis.get("score"),
            })

    by_symbol: dict[str, list[dict]] = {}
    for view in views:
        for symbol in view["symbols"]:
            if held and symbol not in held:
                continue
            by_symbol.setdefault(symbol, []).append(view)

    macro_views = [v for v in views
                   if v["domain"] in ("macro", "regulation", "sentiment", "supply_demand")
                   and not v["symbols"]]

    usable = sum(1 for v in views if v["usable_as_evidence"])
    return {
        "available": True,
        "sources": sources,
        "views": views,
        "by_symbol": by_symbol,
        "macro_views": macro_views,
        "usable_count": usable,
        "days": days,
        "note": (
            f"直近{days}日で {len(sources)}情報源から {len(views)}件。"
            + (f"うち {usable}件は本文の根拠に使えます。" if usable else
               "**本文の根拠に使えるものはありません（全て未測定または重み不足）。**"
               "引用形式で書き、これを根拠に判断を組み立てないこと。")
        ),
    }


def _timedelta(days: int):
    from datetime import timedelta

    return timedelta(days=days)


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
