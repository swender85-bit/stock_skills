"""政策カバレッジと逸脱監査 (案A P5, health 統合用).

「決めた通りに動けたか」を定量化する。逸脱は lesson 候補の主要な供給源になる。
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from src.core.policy.evaluator import evaluate_policy
from src.core.policy.ledger import is_expired, list_policies
from src.core.temporal import stamp_for_symbol


def coverage_rate(
    holdings: list[dict],
    policies: Optional[list[dict]] = None,
    today: Optional[date] = None,
    base_dir: Optional[str] = None,
) -> dict:
    """政策カバレッジ率 = 撤退条件が定義済みの保有比率。

    Parameters
    ----------
    holdings : list of dict
        各要素は ``symbol`` と、あれば ``value``(評価額)。
    """
    if policies is None:
        kwargs = {"active_only": True, "today": today}
        if base_dir is not None:
            kwargs["base_dir"] = base_dir
        policies = list_policies(**kwargs)

    covered_symbols = {
        p.get("symbol") for p in policies if not is_expired(p, today)
    }

    total_value = 0.0
    covered_value = 0.0
    uncovered: list[str] = []

    for h in holdings:
        symbol = h.get("symbol", "")
        try:
            value = float(h.get("value") or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        total_value += value
        if symbol in covered_symbols:
            covered_value += value
        else:
            uncovered.append(symbol)

    count_rate = (
        (len(holdings) - len(uncovered)) / len(holdings) if holdings else 0.0
    )
    value_rate = covered_value / total_value if total_value > 0 else 0.0

    return {
        "holdings_count": len(holdings),
        "covered_count": len(holdings) - len(uncovered),
        "count_rate": round(count_rate, 4),
        "value_rate": round(value_rate, 4),
        "uncovered_symbols": uncovered,
    }


def detect_deviations(
    trades: list[dict],
    policies: Optional[list[dict]] = None,
    market_states: Optional[dict[str, dict]] = None,
    today: Optional[date] = None,
    base_dir: Optional[str] = None,
) -> list[dict]:
    """政策と実際の売買記録の乖離を検出する。

    2種類の逸脱を見る:
      acted_without_trigger : トリガー未成立なのに政策と反する売買をした
      ignored_trigger       : トリガー成立済みなのに政策の応答を実行していない

    Parameters
    ----------
    trades : list of dict
        各要素は ``symbol`` / ``action``("buy"|"sell") / ``date``。
    market_states : dict
        symbol -> 市況dict。無い銘柄はトリガー判定不能としてスキップ。
    """
    if policies is None:
        kwargs = {"active_only": True, "today": today}
        if base_dir is not None:
            kwargs["base_dir"] = base_dir
        policies = list_policies(**kwargs)

    states = market_states or {}
    deviations: list[dict] = []

    by_symbol: dict[str, list[dict]] = {}
    for p in policies:
        by_symbol.setdefault(p.get("symbol", ""), []).append(p)

    traded_symbols: dict[str, list[dict]] = {}
    for t in trades:
        traded_symbols.setdefault(t.get("symbol", ""), []).append(t)

    for symbol, sym_policies in by_symbol.items():
        market = states.get(symbol)
        if market is None:
            continue

        for policy in sym_policies:
            assessment = evaluate_policy(policy, market, today)
            sym_trades = traded_symbols.get(symbol, [])
            sells = [t for t in sym_trades if t.get("action") == "sell"]

            if assessment["state"] == "met":
                # 政策の応答が売却系なのに売っていない
                if _is_exit_response(policy.get("response", "")) and not sells:
                    deviations.append(
                        _deviation(
                            symbol,
                            policy,
                            "ignored_trigger",
                            f"トリガー成立済みだが政策の応答「{policy.get('response')}」が未実行",
                        )
                    )
            else:
                # トリガー未成立なのに売却している
                if sells and _is_exit_response(policy.get("response", "")):
                    deviations.append(
                        _deviation(
                            symbol,
                            policy,
                            "acted_without_trigger",
                            f"トリガー未成立({assessment['label']})だが売却を実行",
                        )
                    )

    return deviations


def _is_exit_response(response: str) -> bool:
    """応答が撤退(売却)系かの粗い判定。"""
    text = (response or "").lower()
    return any(k in text for k in ("売却", "売る", "撤退", "全株", "利確", "損切", "sell", "exit"))


def _deviation(symbol: str, policy: dict, kind: str, detail: str) -> dict:
    return {
        "symbol": symbol,
        "policy_id": policy.get("id", ""),
        "kind": kind,
        "detail": detail,
        "policy_response": policy.get("response", ""),
        "detected_at": stamp_for_symbol(symbol),
    }


def record_deviation(deviation: dict) -> dict:
    """逸脱を lesson 候補として記録可能な形にする (案A → 案B の接続点).

    逸脱は「過程」の誤りなので、案Bの二層化では process 由来になる資格を持つ。
    """
    return {
        "origin": "process",
        "symbol": deviation.get("symbol", ""),
        "trigger": f"{deviation.get('symbol')} で政策逸脱({deviation.get('kind')})",
        "expected_action": (
            f"政策の応答「{deviation.get('policy_response')}」に従うか、"
            f"平時に政策そのものを改訂する"
        ),
        "detail": deviation.get("detail", ""),
    }
