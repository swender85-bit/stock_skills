"""現金・流入・注意予算の一級市民化 (土曜設計書 提案9)。

## 名指しする問題

ポートフォリオ模型に**三つの決定的な要素が存在しない**。

**(a) 現金** — 「暴落時に買う」という計画は、現金がなければ計画ではない。
**(b) 入金フロー** — 蓄積期の投資家にとって最大の変数なのに、語彙が無い。
**(c) 注意予算** — 監視できない銘柄数は、インデックスを持つより悪い。

その帰結:

> 「Aを売ってBを買うべきか」に対する蓄積期の投資家の正解は、しばしば
> **「何もするな。8週後の入金でBを買え」** である。

売却を伴わないので税がかからず（提案3）、執行摩擦もなく（提案5）、
A のテーゼが生きているなら手放す理由もない。
ところが現行システムはこの結論を**出力する能力そのものを持たない**。

## 「待つ」には必ず期限を付ける

無期限の保留を作ると、「待つ」が先延ばしの口実に化ける。
`wait_option` は必ず `review_date` を持つ（政策台帳の失効期限と同じ原理）。

## 収入情報の扱い

Neo4j へは**絶対額を書かない**。`to_graph_safe()` が比率だけに落とす。
これはコードで強制し、テストで検証する（受け入れ基準5）。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

DEFAULT_CONFIG_PATH = "config/cashflow.yaml"

_FALLBACK = {
    "contributions": {"monthly_amount": 0, "irregular": []},
    "estimation": {"lookback_weeks": 26, "percentile": 25, "runway_weeks": 12},
    "cash": {"purposes": []},
    "attention": {"weekly_review_minutes": 45, "min_minutes_per_holding": 4},
    "privacy": {"disclose_absolute_amounts": True},
}

_cache: dict[str, Any] = {}


def load_cashflow_config(path: str = DEFAULT_CONFIG_PATH,
                         use_cache: bool = True) -> dict:
    if use_cache and path in _cache:
        return _cache[path]
    try:
        import yaml

        p = Path(path)
        cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {} if p.exists() else {}
    except Exception:
        cfg = {}
    merged = {**_FALLBACK, **cfg}
    for key, default in _FALLBACK.items():
        if isinstance(default, dict):
            merged[key] = {**default, **(cfg.get(key) or {})}
    if use_cache:
        _cache[path] = merged
    return merged


def reset_cache() -> None:
    _cache.clear()


# ---------------------------------------------------------------------------
# 入金額の推定
# ---------------------------------------------------------------------------


def _percentile(values: list[float], pct: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (pct / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def weekly_investable(
    deposit_history: Optional[list[dict]] = None,
    cfg: Optional[dict] = None,
) -> dict:
    """週あたりの投資可能額を推定する。

    実績があればそこから**保守側（既定: 下位25パーセンタイル）**で推定する。
    平均を使うと、たまたま賞与が入った週に引っ張られて
    「待てば買える」の見積もりが常に外れる。

    実績が無ければ設定の定期積立額から機械的に割り戻す。
    """
    cfg = cfg or load_cashflow_config()
    est = cfg.get("estimation") or {}
    lookback = int(est.get("lookback_weeks") or 26)
    pct = float(est.get("percentile") or 25)

    weekly = _weekly_amounts(deposit_history, lookback)
    if weekly:
        value = _percentile(weekly, pct)
        return {
            "available": True, "weekly_jpy": value, "basis": "history",
            "samples": len(weekly), "percentile": pct,
            "note": (f"過去{len(weekly)}週の入金実績の下位{pct:.0f}パーセンタイル。"
                     "保守側に倒しています。"),
        }

    monthly = float((cfg.get("contributions") or {}).get("monthly_amount") or 0.0)
    if monthly > 0:
        return {
            "available": True, "weekly_jpy": monthly * 12.0 / 52.0,
            "basis": "config", "samples": 0, "percentile": None,
            "note": "入出金実績が無いため、設定の定期積立額から換算した推定値です。",
        }

    return {"available": False, "weekly_jpy": None, "basis": None, "samples": 0,
            "note": ("入金実績も定期積立の設定もありません。"
                     "config/cashflow.yaml に月額を書くと「待つ」選択肢が使えます。")}


def _weekly_amounts(history: Optional[list[dict]], lookback_weeks: int) -> list[float]:
    """入出金履歴を週次バケットに畳む。出金は負として扱う。"""
    if not history:
        return []
    cutoff = date.today() - timedelta(weeks=lookback_weeks)
    buckets: dict[int, float] = {}
    for row in history:
        if not isinstance(row, dict):
            continue
        amount = row.get("amount_jpy", row.get("amount"))
        raw_date = row.get("date")
        if not isinstance(amount, (int, float)):
            continue
        try:
            d = date.fromisoformat(str(raw_date)[:10])
        except (TypeError, ValueError):
            continue
        if d < cutoff:
            continue
        week = (d - cutoff).days // 7
        buckets[week] = buckets.get(week, 0.0) + float(amount)
    # 入金の無い週も 0 として数える。抜くと「毎週入る」と誤認する。
    return [buckets.get(w, 0.0) for w in range(max(buckets) + 1)] if buckets else []


def runway(weekly_jpy: Optional[float], cash_jpy: Optional[float] = None,
           weeks: Optional[int] = None, cfg: Optional[dict] = None) -> dict:
    """今後N週の累積投資可能額。

    将来入金は「取得可能額」の計算にのみ使い、**リスク許容度の拡大には使わない**
    （設計書 提案9-⑧）。
    """
    cfg = cfg or load_cashflow_config()
    weeks = weeks or int((cfg.get("estimation") or {}).get("runway_weeks") or 12)
    if weekly_jpy is None:
        return {"available": False, "weeks": weeks,
                "reason": "週次投資可能額が推定できていません"}
    cumulative = [(w, (cash_jpy or 0.0) + weekly_jpy * w) for w in range(1, weeks + 1)]
    return {
        "available": True,
        "weeks": weeks,
        "weekly_jpy": weekly_jpy,
        "cash_jpy": cash_jpy,
        "cumulative_jpy": cumulative[-1][1],
        "schedule": [{"week": w, "available_jpy": v} for w, v in cumulative],
        "caveat": ("将来入金は取得可能額の計算にのみ使います。"
                   "これを根拠にリスクを増やしてはいけません。"),
    }


def weeks_until(target_jpy: float, weekly_jpy: Optional[float],
                cash_jpy: float = 0.0) -> Optional[int]:
    """目標額に届くまでの週数。届かない/推定不能なら None。"""
    if weekly_jpy is None or weekly_jpy <= 0:
        return None
    if cash_jpy >= target_jpy:
        return 0
    import math

    return int(math.ceil((target_jpy - cash_jpy) / weekly_jpy))


# ---------------------------------------------------------------------------
# 資金調達の選択肢（提案9の中核）
# ---------------------------------------------------------------------------


def funding_options(
    target_jpy: float,
    *,
    cash_jpy: float = 0.0,
    weekly_jpy: Optional[float] = None,
    sell_candidate: Optional[dict] = None,
    thesis_alive: Optional[bool] = None,
    today: Optional[date] = None,
    cfg: Optional[dict] = None,
) -> dict:
    """(a)売却 / (b)現金充当 / (c)入金待ち を比較して推奨を返す。

    Args:
        sell_candidate: {"symbol","name","shares","price","cost_price","account",
                         "currency","fx_rate"} — 売却で資金を作る場合の対象
        thesis_alive: 売却候補のテーゼが生きているか。生きているなら売る理由が無い。

    **売却を伴う提案には必ず入金代替案が併記される**（受け入れ基準2）。
    """
    cfg = cfg or load_cashflow_config()
    today = today or date.today()
    options: list[dict] = []

    # (a) 売却
    if sell_candidate:
        options.append(_sell_option(target_jpy, sell_candidate, thesis_alive))

    # (b) 現金充当
    options.append(_cash_option(target_jpy, cash_jpy))

    # (c) 入金待ち
    options.append(_wait_option(target_jpy, cash_jpy, weekly_jpy, today, cfg))

    # (d) 取得規模を落とす。
    # 「テーゼが生きている銘柄を売る」と「1年待つ」しか無いとき、
    # そのどちらかを最善手として提示するのは誤り。買う量を減らせばよい。
    options.append(_resize_option(target_jpy, cash_jpy, weekly_jpy, cfg))

    viable = [o for o in options if o.get("viable")]
    recommended = min(viable, key=lambda o: o.get("rank", 99)) if viable else None
    for o in options:
        o["recommended"] = bool(recommended and o is recommended)

    return {
        "target_jpy": target_jpy,
        "options": options,
        "recommended": recommended.get("kind") if recommended else None,
        "note": ("売却を伴う案には必ず入金代替案を併記しています。"
                 "蓄積期のリバランスは、売買ではなく入金で行うのが原則です。"),
    }


def _sell_option(target_jpy: float, cand: dict, thesis_alive: Optional[bool]) -> dict:
    from src.core.portfolio.tax import switching_hurdle

    shares = cand.get("shares")
    price = cand.get("price")
    cost = cand.get("cost_price")
    if not all(isinstance(x, (int, float)) for x in (shares, price, cost)):
        return {"kind": "sell", "viable": False, "rank": 99,
                "label": "保有の一部を売却して資金を作る",
                "detail": "売却候補の数量・価格・取得単価が揃わず試算できません。"}

    h = switching_hurdle(shares, price, cost, cand.get("account"),
                         fx_rate=cand.get("fx_rate", 1.0),
                         currency=cand.get("currency", "JPY"))
    friction = h.get("friction_jpy")
    reasons = []
    if friction:
        reasons.append(f"税・手数料・為替で {friction:,.0f}円 が消えます")
    if thesis_alive:
        reasons.append("この銘柄のテーゼは生きているため、売る理由がありません")

    # テーゼが生きている銘柄の売却は最下位。ここを中位に置くと、
    # 「売る理由がない」と書きながら売却を推奨する矛盾が起きる。
    rank = 6 if thesis_alive else (2 if thesis_alive is False else 4)

    return {
        "kind": "sell",
        # 売却は「できる」が、摩擦がある。テーゼが生きているなら選ばない。
        "viable": True,
        "rank": rank,
        "label": f"{cand.get('name') or cand.get('symbol')} を売却して資金を作る",
        "amount_jpy": h.get("net_jpy"),
        "friction_jpy": friction,
        "hurdle_pct": h.get("hurdle_pct"),
        "detail": "。".join(reasons) if reasons else h.get("message"),
        "tax_detail": h,
    }


def _cash_option(target_jpy: float, cash_jpy: float) -> dict:
    enough = cash_jpy >= target_jpy
    remaining = cash_jpy - target_jpy
    return {
        "kind": "cash",
        "viable": enough,
        "rank": 2,
        "label": "手元の現金を充当する",
        "amount_jpy": min(cash_jpy, target_jpy),
        "detail": (
            f"可能ですが、現金が {remaining:,.0f}円 まで減ります。"
            "暴落時の余力を失います。" if enough else
            f"現金 {cash_jpy:,.0f}円 では {target_jpy - cash_jpy:,.0f}円 足りません。"),
    }


def _wait_option(target_jpy: float, cash_jpy: float, weekly_jpy: Optional[float],
                 today: date, cfg: dict) -> dict:
    weeks = weeks_until(target_jpy, weekly_jpy, cash_jpy)
    if weeks is None:
        return {"kind": "wait", "viable": False, "rank": 99,
                "label": "入金を待って取得する",
                "detail": ("週次投資可能額が推定できないため試算できません。"
                           "config/cashflow.yaml に月額を書いてください。")}

    horizon = int((cfg.get("estimation") or {}).get("runway_weeks") or 12)
    disclose = bool((cfg.get("privacy") or {}).get("disclose_absolute_amounts", True))
    in_horizon_jpy = (cash_jpy or 0) + (weekly_jpy or 0) * horizon

    if weeks == 0:
        # 手元の現金で足りている。「0週待つ」は選択肢ではない。
        return {"kind": "wait", "viable": False, "rank": 99, "weeks": 0,
                "label": "入金を待って取得する",
                "detail": "手元の現金で足りるため、待つ必要はありません。"}

    # 「43週待て」は計画ではない。入金ペースに対して目標額が大きすぎる場合、
    # 待機を推奨1位に置くと、先延ばしを最善手として提示することになる。
    if weeks <= horizon:
        rank, realistic = 1, True
    elif weeks <= horizon * 2:
        rank, realistic = 3, True
    else:
        rank, realistic = 5, False

    review_weeks = min(max(weeks, 1), horizon)
    review = today + timedelta(weeks=review_weeks)
    amount_txt = (f"{weeks}週後の累積投資可能額 "
                  f"{(cash_jpy or 0) + (weekly_jpy or 0) * weeks:,.0f}円"
                  if disclose else f"入金 {weeks}週分")

    if realistic:
        detail = (f"税ゼロ・売却なし・執行摩擦なし。{amount_txt}。"
                  f"再評価日: {review.isoformat()}（この日に必ず見直します）。")
    else:
        size_txt = (f"{horizon}週で用意できるのは {in_horizon_jpy:,.0f}円 です"
                    if disclose else f"{horizon}週分の入金で足りる規模に落とせます")
        detail = (
            f"入金だけで届くのは {weeks}週後（約{weeks / 52:.1f}年）です。"
            f"これは事実上「買わない」と同義であり、待機を最善手として扱いません。"
            f"{size_txt}。取得規模を落とすか、売却案と正面から比較してください。"
            f" 再評価日: {review.isoformat()}。")

    return {
        "kind": "wait",
        "viable": True,
        # 税ゼロ・売却なし・執行摩擦なし。蓄積期では通常これが最善。
        # ただし現実的な期間で届く場合に限る。
        "rank": rank,
        "realistic": realistic,
        "label": f"{weeks}週待って入金で取得する",
        "weeks": weeks,
        "horizon_weeks": horizon,
        "affordable_in_horizon_jpy": in_horizon_jpy if disclose else None,
        "amount_jpy": (cash_jpy or 0) + (weekly_jpy or 0) * weeks if disclose else None,
        # 無期限の保留を作らない。「待つ」には必ず期限と再評価日を付ける。
        "review_date": review.isoformat(),
        "detail": detail,
    }


def _resize_option(target_jpy: float, cash_jpy: float,
                   weekly_jpy: Optional[float], cfg: dict) -> dict:
    """ランウェイ内で買える規模まで落とす案。

    「テーゼが生きている銘柄を売る」か「1年待つ」しか無いとき、
    そのどちらかを最善手として提示するのは誤りである。買う量を減らせばよい。
    """
    horizon = int((cfg.get("estimation") or {}).get("runway_weeks") or 12)
    if weekly_jpy is None:
        return {"kind": "resize", "viable": False, "rank": 99,
                "label": "取得規模を落とす",
                "detail": "週次投資可能額が推定できないため試算できません。"}

    affordable = (cash_jpy or 0.0) + weekly_jpy * horizon
    if affordable >= target_jpy:
        return {"kind": "resize", "viable": False, "rank": 99,
                "label": "取得規模を落とす",
                "detail": f"{horizon}週以内に全額用意できるため、縮小は不要です。"}

    ratio = affordable / target_jpy if target_jpy else 0.0
    disclose = bool((cfg.get("privacy") or {}).get("disclose_absolute_amounts", True))
    amount_txt = (f"{affordable:,.0f}円（目標の {ratio:.0%}）" if disclose
                  else f"目標の {ratio:.0%}")
    return {
        "kind": "resize",
        "viable": True,
        "rank": 4,
        "label": f"{horizon}週で用意できる規模に落として取得する",
        "amount_jpy": affordable if disclose else None,
        "ratio_of_target": round(ratio, 3),
        "detail": (f"税ゼロ・売却なし。{horizon}週で {amount_txt} まで取得できます。"
                   "全額でなくても、ポジションは持てます。"),
    }


# ---------------------------------------------------------------------------
# 現金の目的
# ---------------------------------------------------------------------------


def cash_purpose_check(cash_jpy: Optional[float], total_jpy: Optional[float] = None,
                       cfg: Optional[dict] = None) -> dict:
    """目的の割り当てられていない現金を警告する。

    目的のない現金は、下落時に使われない傾向がある。
    """
    cfg = cfg or load_cashflow_config()
    purposes = [p for p in ((cfg.get("cash") or {}).get("purposes") or [])
                if isinstance(p, dict)]
    allocated = sum(float(p.get("amount_jpy") or 0.0) for p in purposes)
    cash = float(cash_jpy or 0.0)
    unallocated = max(0.0, cash - allocated)
    ratio = (cash / total_jpy * 100.0) if total_jpy else None

    return {
        "cash_jpy": cash,
        "cash_pct": round(ratio, 1) if ratio is not None else None,
        "allocated_jpy": allocated,
        "unallocated_jpy": unallocated,
        "purposes": purposes,
        "warning": (
            f"現金 {unallocated:,.0f}円 に割り当てられた目的がありません。"
            "「暴落時の買い増し用」なのか「近く買う予定の銘柄用」なのかが未定義です。"
            "目的のない現金は、下落時に使われない傾向があります。"
            if unallocated > 0 else None),
    }


# ---------------------------------------------------------------------------
# 注意予算
# ---------------------------------------------------------------------------


def attention_budget(holdings_count: int, orphans_count: int = 0,
                     cfg: Optional[dict] = None) -> dict:
    """保有銘柄数が、実際に維持できる注意量に収まっているか。

    ポートフォリオの適正銘柄数は、リスク分散の理論値ではなく
    **保有者が実際に維持できる注意量**によって決まる。
    上限の強制ではなく目安として提示する（設計書 提案9-⑧）。
    """
    cfg = cfg or load_cashflow_config()
    att = cfg.get("attention") or {}
    minutes = float(att.get("weekly_review_minutes") or 45)
    min_each = float(att.get("min_minutes_per_holding") or 4)

    per_holding = minutes / holdings_count if holdings_count else None
    max_holdings = int(minutes // min_each) if min_each else None
    monitored = max(0, holdings_count - orphans_count)

    warning = None
    if per_holding is not None and per_holding < min_each:
        warning = (f"1銘柄あたり {per_holding:.1f}分 しか使えていません"
                   f"（目安 {min_each:.0f}分）。"
                   "注意予算を超えた銘柄は実質的に監視されていません。")

    orphan_note = None
    if orphans_count:
        orphan_note = (f"うち{orphans_count}銘柄は孤児ポジションで、レビューの基準が"
                       f"ありません。実質的に監視されている銘柄は{monitored}。")

    return {
        "holdings": holdings_count,
        "orphans": orphans_count,
        "effectively_monitored": monitored,
        "weekly_minutes": minutes,
        "minutes_per_holding": round(per_holding, 1) if per_holding else None,
        "suggested_max_holdings": max_holdings,
        "warning": warning,
        "orphan_note": orphan_note,
        "guidance": (f"あなたのレビュー時間では {max_holdings}銘柄程度が上限の目安です。"
                     if max_holdings else None),
    }


# ---------------------------------------------------------------------------
# グラフ書き込み用の安全化
# ---------------------------------------------------------------------------


def to_graph_safe(runway_result: dict, total_jpy: Optional[float] = None) -> dict:
    """Neo4j に書ける形へ落とす。**収入関連の絶対額を必ず落とす。**

    設計書 提案9-④注記: 金額の絶対値ではなく「週次投資可能額」として扱う。
    ここは比率だけにする。受け入れ基準5をこの関数で担保する。
    """
    weekly = runway_result.get("weekly_jpy")
    cumulative = runway_result.get("cumulative_jpy")
    return {
        "weeks": runway_result.get("weeks"),
        "available": runway_result.get("available"),
        # 絶対額は入れない。比率のみ。
        "weekly_pct_of_portfolio": (
            round(weekly / total_jpy * 100.0, 3)
            if isinstance(weekly, (int, float)) and total_jpy else None),
        "runway_pct_of_portfolio": (
            round(cumulative / total_jpy * 100.0, 2)
            if isinstance(cumulative, (int, float)) and total_jpy else None),
    }
