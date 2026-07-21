"""質問ストリームの第一級データ化 -- 観測者リスクの内部化 (Fable5 第1弾 案1).

## なぜ必要か

自然言語ファースト設計は、intent-routing が意図を判定した瞬間に発話そのものを
捨てている。だが Stock Skills で唯一モデル化されていない最大のリスク要因は
**ユーザー自身**である。

「PF大丈夫かな」「そろそろ売るべき？」という問いの種類・頻度・タイミングは
投資家の認知状態の時系列データなのに、どこにも残っていない。
システムは市場を記憶していて、観測者を記憶していない。

## 何をするか

intent-routing が既に無料で生成している判定結果（意図分類・対象銘柄・感情極性）を、
その瞬間の市場状態とジョインして Question として刻む。

蓄積されると「防御的質問は過去3回とも指数-4%以降に集中。あなたの不安は遅行指標で、
過去2回はその直後が底だった」と返せるようになる。**自分自身が逆張り指標になる。**

## 非破壊

記録は追記のみ。既存のルーティング動作には一切影響しない。
記録に失敗しても質問処理は続行する（graceful degradation）。
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src.core.temporal import now_utc, stamp


_QUESTIONS_DIR = "data/questions"

#: 質問の感情極性(投資家の認知状態)
DEFENSIVE = "defensive"      # 不安・防御的（売るべき？大丈夫？損切り？）
ACQUISITIVE = "acquisitive"  # 攻撃的・取得志向（買うべき？何かいい株？）
NEUTRAL = "neutral"          # 情報照会（いくら？見せて）

_DEFENSIVE_MARKERS = (
    "大丈夫", "売るべき", "損切", "やばい", "危ない", "怖い", "不安", "暴落",
    "下がって", "含み損", "撤退", "逃げ", "耐え", "持ちこたえ", "どうしよう",
)
_ACQUISITIVE_MARKERS = (
    "買うべき", "買い増し", "いい株", "おすすめ", "狙い目", "仕込み", "エントリー",
    "チャンス", "有望", "探して", "上がる",
)


def classify_sentiment(text: str) -> str:
    """質問の感情極性を分類する。

    intent-routing の副産物として無料で得られる情報。厳密な感情分析ではなく、
    「防御的な問いか、取得志向の問いか」という投資行動上の区別だけを見る。
    """
    if not text:
        return NEUTRAL
    lowered = text.lower()
    defensive = sum(1 for m in _DEFENSIVE_MARKERS if m in lowered)
    acquisitive = sum(1 for m in _ACQUISITIVE_MARKERS if m in lowered)

    # 「いい日本株ある？」のように修飾語が挟まる形は単純な部分一致で拾えない。
    # 「いい/良い」と「株/銘柄」の共起を取得志向とみなす。
    if any(a in lowered for a in ("いい", "良い", "よい")) and any(
        b in lowered for b in ("株", "銘柄")
    ):
        acquisitive += 1

    if defensive > acquisitive:
        return DEFENSIVE
    if acquisitive > defensive:
        return ACQUISITIVE
    return NEUTRAL


def _questions_dir(base_dir: str = _QUESTIONS_DIR) -> Path:
    d = Path(base_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def record_question(
    text: str,
    intent: str = "",
    symbols: Optional[list[str]] = None,
    market_state: Optional[dict] = None,
    at: Any = None,
    base_dir: str = _QUESTIONS_DIR,
) -> Optional[dict]:
    """質問を市場状態とジョインして記録する。

    Parameters
    ----------
    text : str
        ユーザーの発話そのもの。
    intent : str
        intent-routing が判定したドメイン/スキル。
    symbols : list of str
        検出されたティッカー。
    market_state : dict
        その瞬間の市場状態（指数変動率・PF損益など）。無ければ空で記録する
        （後から市況を遡って埋めることはできないが、質問の時刻と種類は残る）。

    Returns
    -------
    dict | None
        記録したレコード。失敗時は None（呼び出し側の処理は止めない）。
    """
    try:
        ts = stamp("JP", at=at)
        today = ts["utc"][:10]
        record = {
            "id": f"q_{today}_{uuid.uuid4().hex[:8]}",
            "text": text,
            "intent": intent,
            "symbols": list(symbols or []),
            "sentiment": classify_sentiment(text),
            "asked_at": ts,
            "market_state": dict(market_state or {}),
        }

        path = _questions_dir(base_dir) / f"{today}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record
    except Exception:
        # 観測の記録が失敗しても、ユーザーの質問処理は絶対に止めない。
        return None


def load_questions(
    since: Optional[date] = None,
    sentiment: Optional[str] = None,
    base_dir: str = _QUESTIONS_DIR,
) -> list[dict]:
    """記録済みの質問を新しい順に読む。"""
    d = Path(base_dir)
    if not d.exists():
        return []

    out: list[dict] = []
    for path in sorted(d.glob("*.jsonl")):
        if since is not None:
            try:
                if date.fromisoformat(path.stem) < since:
                    continue
            except ValueError:
                pass
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if sentiment and record.get("sentiment") != sentiment:
                    continue
                out.append(record)
        except OSError:
            continue

    out.sort(key=lambda r: r.get("asked_at", {}).get("utc", ""), reverse=True)
    return out


def investor_diagnosis(
    questions: Optional[list[dict]] = None,
    base_dir: str = _QUESTIONS_DIR,
) -> dict:
    """質問ストリームから投資家自身を診断する (health の「投資家診断」節).

    Returns
    -------
    dict
        {"total", "defensive", "acquisitive", "neutral", "defensive_ratio",
         "defensive_market_levels": [...], "insight": str}
    """
    if questions is None:
        questions = load_questions(base_dir=base_dir)

    total = len(questions)
    counts = {DEFENSIVE: 0, ACQUISITIVE: 0, NEUTRAL: 0}
    defensive_levels: list[float] = []

    for q in questions:
        sentiment = q.get("sentiment", NEUTRAL)
        if sentiment in counts:
            counts[sentiment] += 1
        if sentiment == DEFENSIVE:
            level = (q.get("market_state") or {}).get("index_change_pct")
            if level is not None:
                try:
                    defensive_levels.append(float(level))
                except (TypeError, ValueError):
                    pass

    ratio = counts[DEFENSIVE] / total if total else 0.0
    insight = _build_insight(total, counts, ratio, defensive_levels)

    return {
        "total": total,
        "defensive": counts[DEFENSIVE],
        "acquisitive": counts[ACQUISITIVE],
        "neutral": counts[NEUTRAL],
        "defensive_ratio": round(ratio, 3),
        "defensive_market_levels": defensive_levels,
        "insight": insight,
    }


def _build_insight(
    total: int, counts: dict, ratio: float, defensive_levels: list[float]
) -> str:
    """診断文を組み立てる。サンプルが少ないうちは断定しない。"""
    if total < 5:
        return f"記録された質問は{total}件。傾向を語るにはまだ足りない（5件以上で診断開始）。"

    parts = [
        f"質問{total}件のうち防御的{counts[DEFENSIVE]}件 / "
        f"取得志向{counts[ACQUISITIVE]}件 / 情報照会{counts[NEUTRAL]}件。"
    ]

    if defensive_levels and len(defensive_levels) >= 3:
        avg = sum(defensive_levels) / len(defensive_levels)
        if avg < -2.0:
            parts.append(
                f"防御的な質問は指数が平均{avg:.1f}%のときに集中している。"
                f"**あなたの不安は遅行指標である可能性が高い** — "
                f"下げた後に不安になっており、下げる前に察知できてはいない。"
            )

    if ratio >= 0.6:
        parts.append(
            "防御的な質問が6割を超えている。下落局面での狼狽売りリスクが高い状態。"
            "平時に政策（撤退条件）を確定しておくと、この偏りが実際の売買に伝播しにくくなる。"
        )
    elif ratio <= 0.15 and counts[ACQUISITIVE] > counts[DEFENSIVE]:
        parts.append(
            "取得志向の質問が優勢。高値掴みリスクの方が相対的に高い状態。"
        )

    return " ".join(parts)
