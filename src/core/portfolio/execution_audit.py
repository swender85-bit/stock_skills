"""執行監査と決定生存率 — 分析と現実の間の断層 (土曜設計書 提案5)。

## 名指しする問題

システムは自らの分析精度を測ろうとしている。しかしそこに致命的な断層がある。

> **システムが測っている「精度」は、実際には執行されなかった判断の精度を含んでいる。**

システムが「A社買い」と判断し、ユーザーが実際には買わなかった場合、その後 A社が
上がっても**ユーザーの資産は1円も増えていない**。にもかかわらず現行の追跡は
「判定は当たった」と記録する。逆に、システムが推奨しなかった銘柄をユーザーが
独断で買った場合、その損益はシステムの成績に一切現れない。

つまり**システムの成績表と、ユーザーの実際の資産の間には体系的な乖離がある。**

## 測定する二つの量

- **決定生存率** … 生成した判断のうち実際に執行された割合。
  これが50%なら、システムの「精度」の半分は架空である。
- **執行ショートフォール** … 判断時点の価格と実際の約定価格の差。
  分析のアルファがどれだけ執行で溶けたかを示す。

## トーンの設計（重要）

設計書 提案5-⑧:

> 執行率の低さは「ユーザーの怠慢」ではなく「システムの提案形式が実行可能でない」
> 証拠として提示する（**責任の所在をシステム側に置く**）。

未執行理由は推定であることを常に明示し、確定的に書かない。

## マッチングは無理に紐付けない

同一銘柄への複数判断・部分約定・分割執行があるため、曖昧なものは
`unmatched` のまま保留する。**無理に紐付けると成績表が嘘になる。**
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

#: 判断と約定を同一とみなす最大日数。これを超えたら別物として扱う。
DEFAULT_MATCH_WINDOW_DAYS = 30

#: 数量がこの比率以内なら同一判断の執行とみなす（部分約定を許容）
QTY_TOLERANCE_RATIO = 0.5


def _parse_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00").replace("/", "-")
    for candidate in (text, text[:19], text[:10]):
        try:
            dt = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def _norm(symbol: Any) -> str:
    from src.core.portfolio.reconciliation import normalize_symbol

    return normalize_symbol(symbol)


def _side(value: Any) -> Optional[str]:
    s = str(value or "").strip().upper()
    if s in ("BUY", "B", "購入", "買い", "ADD"):
        return "BUY"
    if s in ("SELL", "S", "売却", "売り", "SWAP"):
        return "SELL"
    return None


# ---------------------------------------------------------------------------
# マッチング
# ---------------------------------------------------------------------------


def match_decisions(
    decisions: list[dict],
    executions: list[dict],
    *,
    window_days: int = DEFAULT_MATCH_WINDOW_DAYS,
) -> dict:
    """判断と約定を突き合わせる。

    Args:
        decisions: [{"id","symbol","side","shares","price","decided_at", ...}]
        executions: [{"symbol","side","shares","price","executed_at", ...}]

    曖昧なものは無理に紐付けず `unmatched_decisions` / `unmatched_executions`
    に残す。紐付けを強行すると成績表が嘘になる。
    """
    remaining = [dict(e) for e in executions or [] if _norm(e.get("symbol"))]
    matched: list[dict] = []
    unmatched_decisions: list[dict] = []

    for d in decisions or []:
        sym = _norm(d.get("symbol"))
        side = _side(d.get("side"))
        decided = _parse_dt(d.get("decided_at") or d.get("date"))
        if not sym or decided is None:
            unmatched_decisions.append({**d, "reason": "銘柄または判断時刻が不明"})
            continue

        best = None
        best_gap = None
        for e in remaining:
            if _norm(e.get("symbol")) != sym:
                continue
            if side and _side(e.get("side")) and _side(e.get("side")) != side:
                continue
            executed = _parse_dt(e.get("executed_at"))
            if executed is None or executed < decided:
                continue
            gap = (executed - decided).days
            if gap > window_days:
                continue
            if best_gap is None or gap < best_gap:
                best, best_gap = e, gap

        if best is None:
            unmatched_decisions.append({**d, "reason": "対応する約定が見つかりません"})
            continue

        remaining.remove(best)
        matched.append(_build_match(d, best, decided))

    return {
        "matched": matched,
        "unmatched_decisions": unmatched_decisions,
        # システム外の独断売買。成績に混ぜてはいけないが、存在は示す。
        "unmatched_executions": remaining,
        "window_days": window_days,
    }


def _build_match(decision: dict, execution: dict, decided: datetime) -> dict:
    executed = _parse_dt(execution.get("executed_at"))
    delay_days = (executed - decided).days if executed else None

    d_price = decision.get("price")
    e_price = execution.get("price")
    shortfall = None
    if isinstance(d_price, (int, float)) and isinstance(e_price, (int, float)) and d_price:
        raw = (e_price - d_price) / d_price * 100.0
        # 買いは高く買うほど不利、売りは安く売るほど不利。
        # 符号を「不利ならマイナス」に揃えないと、買い売り混在で相殺されて消える。
        shortfall = -raw if _side(decision.get("side")) == "BUY" else raw

    return {
        "decision_id": decision.get("id"),
        "symbol": decision.get("symbol"),
        "side": _side(decision.get("side")),
        "decided_at": decided.isoformat(),
        "executed_at": executed.isoformat() if executed else None,
        "delay_days": delay_days,
        "decision_price": d_price,
        "execution_price": e_price,
        "shortfall_pct": round(shortfall, 2) if shortfall is not None else None,
        "decision_context": decision.get("context"),
    }


# ---------------------------------------------------------------------------
# 決定生存率
# ---------------------------------------------------------------------------


def survival_rate(match_result: dict, executions_available: bool = True) -> dict:
    """生成した判断のうち実際に執行された割合。

    これが低いとき、責任の所在は**システム側**にある。
    「提案形式が実行可能でなかった」という証拠として提示する。

    Args:
        executions_available: 約定履歴そのものが取得できたか。
            **偽なら生存率を計算してはならない。** 約定履歴が取れないだけなのに
            「執行率0%」と断定するのは、取得失敗を結果と混同する典型的な誤り。
    """
    matched = match_result.get("matched") or []
    unmatched = match_result.get("unmatched_decisions") or []
    total = len(matched) + len(unmatched)

    if not executions_available:
        return {
            "available": False, "total": total, "executed": None,
            "unexecuted": None,
            "reason": ("約定履歴が取得できていないため、決定生存率を計算できません。"
                       "これは「執行率0%」ではなく**「測定できていない」**です。"
                       "moomoo(OpenD) を有効にするか、売買記録を残してください。"),
        }

    if total == 0:
        return {"available": False, "total": 0,
                "reason": "対象期間に記録された判断がありません（判断の記録から始めてください）"}

    rate = len(matched) / total * 100.0
    return {
        "available": True,
        "total": total,
        "executed": len(matched),
        "unexecuted": len(unmatched),
        "rate_pct": round(rate, 1),
        "message": (
            f"生成された判断 {total}件のうち、実際に執行されたのは {len(matched)}件"
            f"（決定生存率 {rate:.1f}%）。"
            + (f"システムの提案の約{100 - rate:.0f}%が執行されていません。"
               "これはあなたの怠慢ではなく、**提案の形式が実行可能でなかった**"
               "証拠として扱います。" if rate < 70 else "")),
        "caveat": ("未執行判断の損益は、あなたの資産には反映されていません。"
                   "成績表は執行済みと未執行を分けて読んでください。"),
    }


def infer_unexecuted_reasons(match_result: dict,
                             cash_by_date: Optional[dict] = None) -> dict:
    """未執行の理由を**推定する**。確定的に書かない。

    資金制約かどうかは、判断時点の現金残高が必要額に足りていたかで見る。
    現金履歴が無ければ推定しない（憶測を数字にしない）。
    """
    unmatched = match_result.get("unmatched_decisions") or []
    funding: list[dict] = []
    other: list[dict] = []
    unknown: list[dict] = []

    for d in unmatched:
        need = _required_cash(d)
        when = str(d.get("decided_at") or d.get("date") or "")[:10]
        cash = (cash_by_date or {}).get(when)
        if need is None or cash is None:
            unknown.append(d)
        elif cash < need:
            funding.append({**d, "required_jpy": need, "cash_jpy": cash})
        else:
            other.append({**d, "required_jpy": need, "cash_jpy": cash})

    messages: list[str] = []
    if funding:
        messages.append(
            f"資金制約と推定 {len(funding)}件 — 提案時の現金残高が不足していました。"
            "**提案時に現金残高の制約を課すべきです**（資金ランウェイと接続）。")
    if other:
        messages.append(
            f"納得されなかったと推定 {len(other)}件 — 資金は足りていたが未執行です。"
            "提案の根拠の示し方に問題がある可能性があります。")
    if unknown:
        messages.append(
            f"理由を推定できない {len(unknown)}件 — 現金履歴または必要額が不明です。")

    return {
        "funding_constrained": funding,
        "not_convinced": other,
        "unknown": unknown,
        "messages": messages,
        "caveat": "理由はすべて推定です。確定的に扱わないでください。",
    }


def _required_cash(decision: dict) -> Optional[float]:
    shares, price = decision.get("shares"), decision.get("price")
    if _side(decision.get("side")) != "BUY":
        return 0.0
    if not isinstance(shares, (int, float)) or not isinstance(price, (int, float)):
        return None
    return abs(shares * price)


# ---------------------------------------------------------------------------
# 執行ショートフォール
# ---------------------------------------------------------------------------


def _median(values: list[float]) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def execution_shortfall(match_result: dict) -> dict:
    """判断から執行までの遅延と、その間に失った価格差。

    分析精度をいくら上げても回収できない損失源。**政策化（事前の条件付き注文）
    でしか解決できない**ため、政策台帳を採用すべき実証的根拠になる。
    """
    matched = match_result.get("matched") or []
    delays = [m["delay_days"] for m in matched
              if isinstance(m.get("delay_days"), (int, float))]
    shortfalls = [m["shortfall_pct"] for m in matched
                  if isinstance(m.get("shortfall_pct"), (int, float))]

    if not matched:
        return {"available": False,
                "reason": "判断と紐付いた約定がありません"}

    avg = sum(shortfalls) / len(shortfalls) if shortfalls else None
    result = {
        "available": bool(shortfalls or delays),
        "samples": len(matched),
        "median_delay_days": _median([float(d) for d in delays]),
        "avg_shortfall_pct": round(avg, 2) if avg is not None else None,
        "worst": min(matched, key=lambda m: m.get("shortfall_pct") or 0)
        if shortfalls else None,
    }

    if avg is not None:
        direction = "不利" if avg < 0 else "有利"
        result["message"] = (
            f"判断から執行までの遅延は中央値 {result['median_delay_days']}日。"
            f"その間の価格変化は平均 {avg:+.2f}%（{direction}方向）。")
        if avg < 0:
            result["message"] += (
                "この摩擦は分析の改良では回収できません。"
                "**事前の条件付き注文（政策台帳）でしか解決できない種類の損失です。**")

    result["buy_dip_note"] = _buy_dip_note(matched)
    return result


def _buy_dip_note(matched: list[dict]) -> Optional[str]:
    """下落局面の買い判断が遅れていないか。

    「押し目で買う」判断が押し目が終わってから執行されている、という発見は
    分析の改良では絶対に解決できない（設計書 提案5-⑦）。
    """
    buys = [m for m in matched if m.get("side") == "BUY"
            and isinstance(m.get("delay_days"), (int, float))]
    if len(buys) < 3:
        return None
    others = [m for m in matched if m.get("side") != "BUY"
              and isinstance(m.get("delay_days"), (int, float))]
    buy_median = _median([float(m["delay_days"]) for m in buys])
    if not others:
        return None
    other_median = _median([float(m["delay_days"]) for m in others])
    if buy_median is None or other_median is None or buy_median <= other_median:
        return None
    return (f"買い判断の執行遅延（中央値 {buy_median}日）が売り判断"
            f"（{other_median}日）より大きくなっています。"
            "『押し目で買う』判断が、押し目が終わってから執行されている可能性があります。")


# ---------------------------------------------------------------------------
# 成績の分離表示
# ---------------------------------------------------------------------------


def split_performance(match_result: dict,
                      outcomes_by_decision: Optional[dict] = None) -> dict:
    """成績を「執行済み」と「未執行の仮想」に分離する。

    **混ぜてはいけない。** 未執行分の損益はユーザーの資産に反映されていない。
    """
    outcomes = outcomes_by_decision or {}

    def tally(rows: list[dict], key: str) -> dict:
        pnls = []
        for r in rows:
            o = outcomes.get(r.get(key) or r.get("id"))
            if isinstance(o, (int, float)):
                pnls.append(float(o))
        if not pnls:
            return {"available": False, "count": len(rows),
                    "reason": "損益データがありません"}
        wins = sum(1 for p in pnls if p > 0)
        return {"available": True, "count": len(rows), "scored": len(pnls),
                "wins": wins, "win_rate_pct": round(wins / len(pnls) * 100, 1),
                "total_pnl": round(sum(pnls), 0)}

    return {
        "executed": tally(match_result.get("matched") or [], "decision_id"),
        "unexecuted_virtual": tally(
            match_result.get("unmatched_decisions") or [], "id"),
        "note": ("未執行分の損益は、あなたの資産には反映されていません。"
                 "この2つを合算した『精度』は架空です。"),
    }


# ---------------------------------------------------------------------------
# まとめ
# ---------------------------------------------------------------------------


def build_execution_audit(
    decisions: Optional[list[dict]] = None,
    executions: Optional[list[dict]] = None,
    *,
    days: int = 90,
    cash_by_date: Optional[dict] = None,
    outcomes_by_decision: Optional[dict] = None,
) -> dict:
    """執行監査を一括で組み立てる（第7セクション「監査」の材料）。

    判断・約定のどちらかが取れなくても、取れた側の事実は返す。
    """
    errors: list[str] = []
    # 約定履歴を「取れなかった」のか「0件だった」のかを区別する。
    # 混同すると「執行率0%」という嘘の成績が出る。
    executions_available = executions is not None

    if decisions is None:
        decisions, err = _load_decisions(days)
        if err:
            errors.append(err)
    if executions is None:
        executions, err = _load_executions(days)
        if err:
            errors.append(err)
        else:
            executions_available = True

    match = match_decisions(decisions or [], executions or [])
    return {
        "window_days": days,
        "match": match,
        "executions_available": executions_available,
        "survival": survival_rate(match, executions_available),
        "reasons": infer_unexecuted_reasons(match, cash_by_date),
        "shortfall": execution_shortfall(match),
        "performance": split_performance(match, outcomes_by_decision),
        "errors": errors,
        "tone": ("執行率の低さはユーザーの怠慢ではなく、"
                 "提案の形式が実行可能でなかった証拠として扱います。"),
    }


def _load_decisions(days: int):
    """記録済みの判断を読む。無ければ空（0件と『取得できず』は区別する）。"""
    try:
        from src.data.history.load import load_history
    except Exception as e:
        return [], f"判断履歴を読めません: {type(e).__name__}: {e}"

    out: list[dict] = []
    for kind in ("decision", "trade", "report"):
        try:
            rows = load_history(kind, days_back=days) or []
        except Exception:
            continue
        for r in rows:
            if isinstance(r, dict) and r.get("symbol"):
                out.append({**r, "_history_kind": kind})
    return out, None


def _load_executions(days: int):
    """ブローカーの約定履歴。取れなければ理由を返す。"""
    try:
        from src.data.brokers import moomoo_broker

        r = moomoo_broker.fetch_executions(days=days)
    except Exception as e:
        return [], f"約定履歴を取得できません: {type(e).__name__}: {e}"
    if not r.get("available"):
        return [], f"約定履歴を取得できません: {r.get('error')}"
    return r.get("executions") or [], None
