"""反証条件の点検 — 「信念の変化」を週次で見る (土曜設計書 提案8)。

## 名指しする問題

> **報告すべきは価格の変化ではなく、信念の変化である。**

価格が10%動いても、それが自分のテーゼを一切揺るがさないなら報告価値は小さい。
価格が動かなくても、保有企業の開示でテーゼの前提が崩れたなら**それが最重要**である。

現行のレポートは状態（現在のPER、現在の損益）を報告する。スタンバイ運用の
ユーザーが必要としているのは差分であり、最も重要なのは信念の差分である。

## 反証条件を必須にする理由

各 thesis に「何が起きたらこのテーゼは間違いだったと認めるか」を書かせる。
書いていないテーゼは**週次で点検する対象を持たない**ため、レポートは
価格の報告に退化するしかない。

反証条件が満たされていなければ、その銘柄については何も書かない。
これが「静穏週」を成立させる（提案8）。

## 政策台帳との関係

構造はトリガーと同じ（測定可能な指標 × 演算子 × 閾値）なので、評価器は
`src.core.policy.evaluator` を再利用する。違いは**用途**である:

- 政策トリガー … 成立したら**行動**する（売る/買う）
- 反証条件     … 成立したら**テーゼを書き直す**（行動は別途決める）

したがって反証成立は自動で売り推奨を作らない。議題を作る。
"""

from __future__ import annotations

import re
from typing import Any, Optional

from src.core.policy.ledger import MEASURABLE_METRICS, OPERATORS

#: 反証条件で使える指標。政策トリガーの集合に、テーゼ検証向けの
#: ファンダメンタル指標を足す（政策側の集合は変えない＝非破壊）。
FALSIFICATION_METRICS: dict[str, str] = {
    **MEASURABLE_METRICS,
    "revenue_growth": "売上成長率(%)",
    "earnings_growth": "利益成長率(%)",
    "roe": "ROE(%)",
    "profit_margin": "純利益率(%)",
    "forward_per": "予想PER(倍)",
    "market_cap": "時価総額",
    "week_change_pct": "週間騰落率(%)",
}

_COND_RE = re.compile(
    r"^\s*([a-z_]+)\s*(<=|>=|==|<|>)\s*(-?\d+(?:\.\d+)?)\s*$", re.IGNORECASE)


class InvalidFalsification(ValueError):
    """測定不能・曖昧な反証条件。"""


# ---------------------------------------------------------------------------
# 条件のパース
# ---------------------------------------------------------------------------


def parse_condition(raw: Any) -> dict:
    """反証条件を {metric, op, value} に正規化する。

    受け付ける形:
        "operating_margin < 8"        （文字列）
        {"metric": ..., "op": ..., "value": ...}

    曖昧な自然文（「業績が悪化したら」）は**受け付けない**。
    測定できない条件は週次で点検できず、書いた気になるだけで害があるため。
    """
    if isinstance(raw, dict):
        metric = str(raw.get("metric", "")).strip()
        op = str(raw.get("op", "")).strip()
        value = raw.get("value")
    elif isinstance(raw, str):
        m = _COND_RE.match(raw)
        if not m:
            raise InvalidFalsification(
                f"反証条件を解釈できません: {raw!r}。"
                f"『指標 演算子 数値』の形で書いてください（例: operating_margin < 8）。"
                f"使える指標: {', '.join(sorted(FALSIFICATION_METRICS))}")
        metric, op, value = m.group(1).lower(), m.group(2), m.group(3)
    else:
        raise InvalidFalsification("反証条件は文字列か dict で指定してください。")

    if metric not in FALSIFICATION_METRICS:
        raise InvalidFalsification(
            f"測定できない指標です: {metric!r}。"
            f"使える指標: {', '.join(sorted(FALSIFICATION_METRICS))}")
    if op not in OPERATORS:
        raise InvalidFalsification(f"演算子が不正です: {op!r}（{', '.join(OPERATORS)}）")
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise InvalidFalsification(f"閾値が数値ではありません: {value!r}") from None

    return {"metric": metric, "op": op, "value": value,
            "label": FALSIFICATION_METRICS[metric]}


def parse_conditions(raw: Any) -> list[dict]:
    """1つでも複数でも受ける。1つでも壊れていたら全体を拒否する。"""
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    return [parse_condition(i) for i in items]


# ---------------------------------------------------------------------------
# 市場状態の抽出
# ---------------------------------------------------------------------------


def market_state_from_holding(holding: dict) -> dict:
    """パックの保有行から、反証条件の評価に使える状態を組み立てる。

    取れない指標は**含めない**（None を入れると「0」と誤評価される危険がある）。
    """
    f = holding.get("fundamentals") or {}
    t = holding.get("technicals") or {}
    state: dict[str, float] = {}

    def put(key: str, value: Any, scale: float = 1.0) -> None:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            state[key] = float(value) * scale

    put("price", holding.get("price"))
    put("price_change_pct", holding.get("pl_pct"))
    put("week_change_pct", holding.get("week_change_pct"))
    put("position_weight_pct", holding.get("weight_pct"))
    put("rsi", t.get("rsi"))
    put("per", f.get("per"))
    put("forward_per", f.get("forward_per"))
    put("pbr", f.get("pbr"))
    put("market_cap", f.get("market_cap"))

    # 比率系は 0.15 のような小数で来る。反証条件は「%」で書かれるので揃える。
    for key, src in (("roe", "roe"), ("profit_margin", "profit_margin"),
                     ("operating_margin", "operating_margin"),
                     ("revenue_growth", "revenue_growth"),
                     ("earnings_growth", "earnings_growth"),
                     ("dividend_yield", "dividend_yield")):
        v = f.get(src)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            state[key] = float(v) * 100.0 if abs(v) <= 1.5 else float(v)

    dd = t.get("drawdown_pct")
    if isinstance(dd, (int, float)):
        state["drawdown_pct"] = float(dd)
    return state


# ---------------------------------------------------------------------------
# 点検
# ---------------------------------------------------------------------------


def check_thesis(thesis: dict, state: dict) -> dict:
    """thesis 1件の反証条件を評価する。

    Returns:
        {"symbol", "has_falsification", "conditions": [...], "falsified": bool,
         "state": "met"/"near"/"far"/"unknown"}
    """
    from src.core.policy.evaluator import evaluate_trigger

    symbol = thesis.get("symbol") or ""
    raw = thesis.get("falsification")
    if not raw:
        return {
            "symbol": symbol, "note_id": thesis.get("id"),
            "content": thesis.get("content"), "date": thesis.get("date"),
            "has_falsification": False, "conditions": [], "falsified": False,
            "state": "undefined",
            "message": "反証条件が未定義です。何が起きたら間違いと認めるかが書かれていません。",
        }

    try:
        conditions = parse_conditions(raw)
    except InvalidFalsification as e:
        return {
            "symbol": symbol, "note_id": thesis.get("id"),
            "content": thesis.get("content"), "date": thesis.get("date"),
            "has_falsification": True, "conditions": [], "falsified": False,
            "state": "invalid", "message": str(e),
        }

    evaluated = [evaluate_trigger(c, state) for c in conditions]
    for c, e in zip(conditions, evaluated):
        e["label"] = c.get("label")

    states = [e.get("state") for e in evaluated]
    # 1つでも成立すればテーゼは反証されたとみなす（AND ではなく OR）。
    # 「全部壊れないと認めない」は、間違いを認めない構造そのもの。
    if "met" in states:
        overall = "met"
    elif "near" in states:
        overall = "near"
    elif all(s == "unknown" for s in states):
        overall = "unknown"
    else:
        overall = "far"

    return {
        "symbol": symbol, "note_id": thesis.get("id"),
        "content": thesis.get("content"), "date": thesis.get("date"),
        "has_falsification": True, "conditions": evaluated,
        "falsified": overall == "met", "state": overall,
        "message": _message(overall, evaluated, thesis),
    }


def _fmt(v: Any) -> str:
    """浮動小数の桁ノイズをそのまま人に見せない（7.199999999999999 等）。"""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return f"{v:.4g}"
    return str(v)


def _message(overall: str, evaluated: list[dict], thesis: dict) -> str:
    hit = [e for e in evaluated if e.get("state") == "met"]
    if overall == "met":
        parts = ", ".join(
            f"{e.get('label') or e.get('metric')} = {_fmt(e.get('actual'))}"
            f"（条件 {e.get('metric')} {e.get('op')} {_fmt(e.get('value'))}）" for e in hit)
        return (f"反証条件が成立しました: {parts}。"
                "これはあなたが事前に『これが起きたらこのテーゼは間違い』と書いた条件です。"
                "保有を続けるなら、別の理由でテーゼを書き直してください。")
    if overall == "near":
        return "反証条件に接近しています。今週の議題候補です。"
    if overall == "unknown":
        return "反証条件の指標が取得できず、点検できませんでした（未点検であって『問題なし』ではありません）。"
    return "反証条件に抵触していません。"


def check_all(holdings: list[dict], theses_by_symbol: Optional[dict] = None) -> dict:
    """保有全体の反証条件を点検する。

    Returns:
        {"falsified": [...], "near": [...], "unchecked": [...],
         "missing": [...], "checked": n}

    `missing`（反証条件を持たない thesis）は、書かせるためのキューになる。
    """
    theses_by_symbol = (theses_by_symbol if theses_by_symbol is not None
                        else _load_theses(holdings))

    falsified: list[dict] = []
    near: list[dict] = []
    unchecked: list[dict] = []
    missing: list[dict] = []
    ok = 0

    for h in holdings or []:
        symbol = h.get("symbol")
        state = market_state_from_holding(h)
        for t in theses_by_symbol.get(symbol or "", []):
            r = check_thesis(t, state)
            r["name"] = h.get("name")
            r["weight_pct"] = h.get("weight_pct")
            if r["state"] == "undefined" or r["state"] == "invalid":
                missing.append(r)
            elif r["state"] == "met":
                falsified.append(r)
            elif r["state"] == "near":
                near.append(r)
            elif r["state"] == "unknown":
                unchecked.append(r)
            else:
                ok += 1

    return {
        "falsified": falsified,
        "near": near,
        "unchecked": unchecked,
        "missing": missing,
        "intact": ok,
        # checked = 実際に評価できた件数。`missing`（条件が無い/壊れている）は
        # 点検の対象になれないので含めない。total と分けておかないと
        # 「N件点検した」が嘘になる。
        "checked": len(falsified) + len(near) + len(unchecked) + ok,
        "total": len(falsified) + len(near) + len(unchecked) + ok + len(missing),
        "note": ("反証成立は売り推奨ではありません。テーゼを書き直すか退出するかの議題です。"
                 if falsified else None),
    }


def _load_theses(holdings: list[dict]) -> dict[str, list[dict]]:
    try:
        from src.data.note_manager import load_notes
    except Exception:
        return {}
    out: dict[str, list[dict]] = {}
    for h in holdings or []:
        sym = h.get("symbol")
        if not sym:
            continue
        try:
            out[sym] = load_notes(symbol=sym, note_type="thesis") or []
        except Exception:
            out[sym] = []
    return out
