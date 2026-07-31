"""流動性制約 — 「動けること」の前提を外す (土曜設計書 提案6)。

## 名指しする問題

現行のストレステストには検証されていない巨大な仮定がある。

> **ストレステストは、ストレス時にユーザーが自由に売買できると仮定している。**

これは二重に誤っている。

**(a) 流動性が消える。** 小型株は平常時でも出来高が薄く、暴落時には板がさらに薄くなる。
解消所要日数が5日を超えるポジションは**暴落中に売れない**。
ストレステストが「-40%、売却推奨」と言っても、売れないなら推奨は無意味である。

**(b) 資金が枯れる。** 暴落時に「割安になった銘柄を買う」推奨は現金がなければ実行できない。
そして現金が最も少ないのは、直前まで強気で買い増していた局面 —— つまり**暴落直前**である。

**(c) 税・NISA制約。** NISA口座内の損失は損益通算できない。
NISA枠で買った銘柄が暴落した場合、税務上の救済が一切ない。
これは通常口座と全く異なる意思決定を要求するが、口座区分を持たないシステムは区別できない。

## 設計原理

出力を「損失額」から **「実行可能性を考慮した後の到達可能な状態」** に変える。
取り得ない行動は **閉じ込め資本（trapped capital）** として明示する。

## 却下条件ではなく警告とサイズ制約

流動性は小型株投資を**禁止しない**（設計書 提案6-⑧）。
長期的なリターン源を捨てさせないため、警告とサイズの目安として扱う。

## 板情報の用途制限

板情報は**流動性の実測という一点にのみ**用いる。
日中の売買シグナル生成に使わない（設計書 第2章の禁則）。
土曜には板が存在しないため、平常時に出来高を週次サンプリングして統計化する。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

#: 出来高のうち自分が占めてよい割合。保守的に設定する。
#: これを上げると「売れるはず」という楽観が入り込む。
DEFAULT_PARTICIPATION_RATE = 0.10

#: 解消所要日数の区分
IMMEDIATE_DAYS = 1.0
SEVERAL_DAYS = 5.0

#: ストレス時の出来高減少係数。単一の値に決めない（設計書 提案6-⑧）。
#: 実際のストレス時出来高は事前に分からないので、幅で示す。
STRESS_VOLUME_FACTORS: dict[str, float] = {
    "optimistic": 0.80,   # 出来高が2割減
    "moderate": 0.50,     # 半減
    "conservative": 0.30,  # 7割減
}

#: 現金比率がこれ未満なら「暴落時に買う計画」は計画ではない
LOW_CASH_PCT = 5.0


def _num(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


# ---------------------------------------------------------------------------
# 出来高
# ---------------------------------------------------------------------------


def average_volume(history: Any, days: int = 60) -> Optional[float]:
    """直近N営業日の平均出来高。取れなければ None。

    0 を返さない。**出来高ゼロと取得不能は違う。**
    """
    if history is None:
        return None
    try:
        series = history["Volume"].dropna()
    except Exception:
        return None
    values = [float(v) for v in list(series)[-days:] if float(v) > 0]
    return sum(values) / len(values) if values else None


def fetch_average_volume(symbol: str, days: int = 60) -> dict:
    """1銘柄の平均出来高。yahoo_client 経由。"""
    try:
        from src.data import yahoo_client as yc

        hist = yc.get_price_history(symbol, period="6mo")
    except Exception as e:
        return {"symbol": symbol, "available": False,
                "error": f"{type(e).__name__}"}
    adv = average_volume(hist, days=days)
    if adv is None:
        return {"symbol": symbol, "available": False,
                "error": "出来高を取得できませんでした（材料なしではなく取得不可）"}
    return {"symbol": symbol, "available": True, "adv": adv, "days": days,
            "fetched_at": datetime.now(timezone.utc).isoformat()}


# ---------------------------------------------------------------------------
# 解消所要日数
# ---------------------------------------------------------------------------


def days_to_liquidate(shares: Optional[float], adv: Optional[float],
                      participation: float = DEFAULT_PARTICIPATION_RATE
                      ) -> Optional[float]:
    """保有を売り切るのに要する日数。

        days = 保有株数 / (平均出来高 × 参加率)
    """
    s, v = _num(shares), _num(adv)
    if s is None or v is None or v <= 0 or participation <= 0:
        return None
    return abs(s) / (v * participation)


def liquidity_profile(holding: dict, adv: Optional[float],
                      participation: float = DEFAULT_PARTICIPATION_RATE) -> dict:
    """1銘柄の流動性プロファイル（平常時＋ストレス時の幅）。"""
    symbol = holding.get("symbol")
    shares = holding.get("shares")
    normal = days_to_liquidate(shares, adv, participation)

    if normal is None:
        return {
            "symbol": symbol, "name": holding.get("name"),
            "available": False, "tier": "unknown",
            "weight_pct": holding.get("weight_pct"),
            "reason": ("出来高または株数が取得できず、解消所要日数を計算できません。"
                       "**流動性が高いという意味ではありません。**"),
        }

    stress = {k: normal / f for k, f in STRESS_VOLUME_FACTORS.items()}
    tier = ("immediate" if normal <= IMMEDIATE_DAYS else
            "several_days" if normal <= SEVERAL_DAYS else "trapped")

    return {
        "symbol": symbol, "name": holding.get("name"),
        "available": True,
        "shares": shares,
        "adv": adv,
        "participation_rate": participation,
        "days_normal": round(normal, 1),
        "days_stress": {k: round(v, 1) for k, v in sorted(stress.items())},
        "tier": tier,
        "weight_pct": holding.get("weight_pct"),
        "value_jpy": holding.get("value_jpy"),
        "note": ("ストレス時の出来高は事前に分かりません。"
                 "楽観/中庸/保守の3通りを併記しています。"),
    }


TIER_LABELS = {
    "immediate": "即時解消可能（1日以内）",
    "several_days": "数日を要する（2〜5日）",
    "trapped": "閉じ込め（5日超）",
    "unknown": "判定不能（出来高が取れない）",
}


def portfolio_liquidity(holdings: list[dict],
                        volumes: Optional[dict] = None,
                        participation: float = DEFAULT_PARTICIPATION_RATE) -> dict:
    """PF を「即時解消 / 数日 / 閉じ込め / 判定不能」の4層に分ける。

    `unknown` を `immediate` に混ぜない。**測れていないものを流動的と扱わない。**
    """
    volumes = volumes or {}
    profiles: list[dict] = []
    # ティッカーを持たない保有（投信など）は出来高で測れない。
    # 黙って除外すると合計が100%にならず、「残りは流動的」と誤読される。
    unmeasurable_pct = 0.0
    unmeasurable: list[dict] = []

    for h in holdings or []:
        sym = h.get("symbol")
        if not sym:
            w = _num(h.get("weight_pct")) or 0.0
            unmeasurable_pct += w
            unmeasurable.append({"name": h.get("name"), "weight_pct": w})
            continue
        v = volumes.get(sym) or {}
        adv = v.get("adv") if v.get("available") else None
        profiles.append(liquidity_profile(h, adv, participation))

    tiers: dict[str, float] = {k: 0.0 for k in TIER_LABELS}
    for p in profiles:
        w = _num(p.get("weight_pct"))
        if w is not None:
            tiers[p["tier"]] = tiers.get(p["tier"], 0.0) + w

    trapped = [p for p in profiles if p["tier"] == "trapped"]
    unknown = [p for p in profiles if p["tier"] == "unknown"]

    return {
        "profiles": profiles,
        "tiers_pct": {k: round(v, 1) for k, v in tiers.items()},
        "trapped": sorted(trapped, key=lambda p: -(p.get("days_normal") or 0)),
        "unknown": unknown,
        "unmeasurable": unmeasurable,
        "unmeasurable_pct": round(unmeasurable_pct, 1),
        "participation_rate": participation,
        "message": _liquidity_message(tiers, trapped, unknown, unmeasurable_pct),
    }


def format_days(days: Optional[float]) -> str:
    """解消所要日数の表示。0.0日と書くと『一瞬で売れる』と読めてしまう。"""
    if days is None:
        return "判定不能"
    if days < 0.05:
        return "即日"
    if days < 1.0:
        return f"{days:.2f}日"
    return f"{days:.1f}日"


def _liquidity_message(tiers: dict, trapped: list, unknown: list,
                       unmeasurable_pct: float = 0.0) -> Optional[str]:
    parts: list[str] = []
    if trapped:
        parts.append(
            f"閉じ込め資本が評価額の {tiers.get('trapped', 0):.1f}%（{len(trapped)}銘柄）。"
            "暴落中にこの分は売り切れません。")
    if unknown:
        parts.append(
            f"{len(unknown)}銘柄は出来高が取れず**判定不能**です"
            "（流動性が高いという意味ではありません）。")
    if unmeasurable_pct > 0:
        parts.append(
            f"ティッカーを持たない保有が評価額の {unmeasurable_pct:.1f}% あり、"
            "出来高では測れません（投信は解約規約が別途あります）。")
    return " ".join(parts) if parts else None


# ---------------------------------------------------------------------------
# ストレステスト推奨の実行可能性
# ---------------------------------------------------------------------------


def check_recommendation_feasibility(
    recommendations: list[dict],
    liquidity: dict,
    *,
    stress_case: str = "moderate",
) -> dict:
    """ストレステストの推奨アクションに実行可能性判定を付ける。

    「この銘柄を売却」という推奨が、そのシナリオ下で何日かかるかを突きつける。
    **売れない推奨は推奨ではなく雑音である。**
    """
    by_symbol = {p["symbol"]: p for p in liquidity.get("profiles") or []}
    checked: list[dict] = []

    for rec in recommendations or []:
        sym = rec.get("symbol")
        action = str(rec.get("action") or rec.get("type") or "").upper()
        row = {**rec, "feasible": None, "reason": None}

        if "SELL" not in action and "売却" not in str(rec.get("action") or ""):
            row["feasible"] = True
            row["reason"] = "売却を伴わないため流動性の制約を受けません。"
            checked.append(row)
            continue

        p = by_symbol.get(sym)
        if not p or not p.get("available"):
            row["feasible"] = None
            row["reason"] = ("出来高が取れず実行可能性を判定できません"
                             "（実行できるという意味ではありません）。")
            checked.append(row)
            continue

        days = (p.get("days_stress") or {}).get(stress_case)
        row["days_stress"] = days
        row["days_normal"] = p.get("days_normal")
        if days is None:
            row["feasible"] = None
            row["reason"] = "ストレス時の所要日数を計算できません。"
        elif days > SEVERAL_DAYS:
            row["feasible"] = False
            row["reason"] = (
                f"そのシナリオ下で {sym} を売り切るには約{format_days(days)}かかります。"
                "**この推奨は実行不能です。**")
            row["alternatives"] = [
                "平常時のいまのうちに、段階的に規模を落とす",
                "売れない前提で、ポジションサイズを縮小する",
                "売却でなくヘッジで対応する政策を事前に用意する",
            ]
        else:
            row["feasible"] = True
            row["reason"] = f"ストレス時でも {format_days(days)} で解消可能です。"
        checked.append(row)

    infeasible = [r for r in checked if r.get("feasible") is False]
    unknown = [r for r in checked if r.get("feasible") is None]
    return {
        "checked": checked,
        "infeasible": infeasible,
        "unknown": unknown,
        "stress_case": stress_case,
        "message": (f"⚠️ 実行不能な推奨が {len(infeasible)}件あります。"
                    if infeasible else None),
    }


# ---------------------------------------------------------------------------
# ストレス時の資金余力
# ---------------------------------------------------------------------------


def crash_buying_power(cash_jpy: Optional[float], total_jpy: Optional[float],
                       buy_candidates: Optional[list] = None,
                       typical_ticket_jpy: Optional[float] = None) -> dict:
    """暴落時に「買う」推奨を、実際の現金と突合する。

    「暴落時に買う」という計画は、**暴落前に現金を用意して初めて計画になる**。
    """
    cash = _num(cash_jpy) or 0.0
    total = _num(total_jpy)
    pct = (cash / total * 100.0) if total else None
    n_candidates = len(buy_candidates or [])

    ticket = _num(typical_ticket_jpy)
    if ticket is None and total:
        # 1銘柄あたりの標準的な投資額をPF総額の5%と仮定する
        ticket = total * 0.05
    affordable = int(cash // ticket) if ticket and ticket > 0 else None

    message = None
    if n_candidates and affordable is not None and affordable < n_candidates:
        message = (
            f"暴落シナリオ下で「買い」推奨が {n_candidates}銘柄 出ていますが、"
            f"現在の現金（{cash:,.0f}円 / 評価額比 "
            f"{pct:.1f}%）で実際に買えるのは {affordable}銘柄分です。"
            if pct is not None else
            f"買い推奨 {n_candidates}銘柄に対し、実際に買えるのは {affordable}銘柄分です。")
    elif pct is not None and pct < LOW_CASH_PCT:
        message = (f"現金比率が {pct:.1f}% しかありません。"
                   "「暴落時に買う」計画は、暴落前に現金を用意して初めて計画になります。")

    return {
        "cash_jpy": cash,
        "cash_pct": round(pct, 1) if pct is not None else None,
        "buy_candidates": n_candidates,
        "affordable_count": affordable,
        "assumed_ticket_jpy": round(ticket) if ticket else None,
        "message": message,
        "note": ("現金が最も少ないのは、直前まで強気で買い増していた局面 —— "
                 "つまり暴落直前です。"),
    }


# ---------------------------------------------------------------------------
# 口座区分による下方非対称（提案3との合流点）
# ---------------------------------------------------------------------------


def account_asymmetry(holdings: list[dict]) -> dict:
    """NISA口座の損失は損益通算できない —— 暴落時の下方非対称。

    日本の個人投資家にとって極めて重要だが、ほぼ誰も指摘していない。
    """
    try:
        from src.core.portfolio.tax import account_kind, load_tax_config

        cfg = load_tax_config()
    except Exception:
        return {"available": False,
                "reason": "税制設定が読めず口座区分を判定できません"}

    tax_free = 0.0
    taxable = 0.0
    unknown = 0.0
    for h in holdings or []:
        w = _num(h.get("weight_pct"))
        if w is None:
            continue
        acct = h.get("account")
        if acct is None:
            unknown += w
            continue
        if account_kind(acct, cfg)["tax_free"]:
            tax_free += w
        else:
            taxable += w

    message = None
    if tax_free > 0:
        message = (
            f"NISA口座保有分が評価額比 {tax_free:.1f}%。"
            "**NISA口座内の損失は損益通算できません。** "
            "暴落時、特定口座の損失には約20%の税務救済がありますが、"
            "NISA分にはそれがありません。実質的な下方リスクは口座区分によって非対称です。")

    return {
        "available": True,
        "tax_free_pct": round(tax_free, 1),
        "taxable_pct": round(taxable, 1),
        "unknown_pct": round(unknown, 1),
        "message": message,
    }


# ---------------------------------------------------------------------------
# まとめ
# ---------------------------------------------------------------------------


def build_liquidity_section(
    holdings: list[dict],
    *,
    cash_jpy: Optional[float] = None,
    total_jpy: Optional[float] = None,
    recommendations: Optional[list[dict]] = None,
    buy_candidates: Optional[list] = None,
    participation: float = DEFAULT_PARTICIPATION_RATE,
    volumes: Optional[dict] = None,
) -> dict:
    """流動性セクションの材料を一括で作る。各要素は独立に失敗し得る。"""
    out: dict = {"errors": []}

    if volumes is None:
        volumes = {}
        for h in holdings or []:
            sym = h.get("symbol")
            if not sym or sym in volumes:
                continue
            try:
                volumes[sym] = fetch_average_volume(sym)
            except Exception as e:
                out["errors"].append(f"{sym} の出来高: {type(e).__name__}")

    try:
        liq = portfolio_liquidity(holdings, volumes, participation)
        out["liquidity"] = liq
    except Exception as e:
        liq = {"profiles": [], "tiers_pct": {}, "trapped": [], "unknown": []}
        out["liquidity"] = liq
        out["errors"].append(f"流動性プロファイル: {type(e).__name__}: {e}")

    for name, fn in (
        ("feasibility", lambda: check_recommendation_feasibility(
            recommendations or [], liq)),
        ("buying_power", lambda: crash_buying_power(
            cash_jpy, total_jpy, buy_candidates)),
        ("account_asymmetry", lambda: account_asymmetry(holdings)),
    ):
        try:
            out[name] = fn()
        except Exception as e:
            out[name] = None
            out["errors"].append(f"{name}: {type(e).__name__}: {e}")

    return out
