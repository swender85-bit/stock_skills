"""三点照合 — 模型・実在・意図の週次突合 (土曜設計書 提案1)。

## 三つの真実

| 層 | 実体 | 真実の所在 |
|:---|:---|:---|
| **模型** | `config/weekly_holdings.yaml` / portfolio.csv / Neo4j | 従属変数（上書きされる） |
| **実在** | 証券口座（moomoo / 楽天CSV） | **残高の唯一真実** |
| **意図** | investment-note の thesis / 政策台帳 | 模型側が真実（上書きしない） |

真実の所在をデータ種別ごとに分ける。残高は口座が正、意図は記録が正。

## 検出する4つの状態

- **幽霊ポジション** — 模型にあるが口座に無い。売却済みなのに記録されていない。
  幽霊があると、ストレステストもHHIも**存在しない資産のリスクを計算している**。
- **未記録ポジション** — 口座にあるが模型に無い。取込漏れ。
- **数量差分** — 両方にあるが株数が違う。分割/端株/記録漏れに分類する。
- **孤児ポジション** — 口座にも模型にもあるが **thesis も政策も無い**。
  勢いで買って以後一度も再評価されていない銘柄。どのシステムも検出しないが、
  最も長く保有され最も大きな毀損を生みやすい。

## 黙って進まない

`available=False` のソースがある場合、その市場の保有は「照合できなかった」であって
「一致した」でも「幽霊」でもない。`unverified` に落ちる。
照合が通らない限り `blocking=True` を立て、下流の分析セクションは
未照合フラグ付きで実行される（設計書 第3章: ここが通らなければ以降を実行しない）。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional

#: 分割として認める比率（模型:実在）。1:2 なら 2.0。
_SPLIT_RATIOS = (2.0, 3.0, 4.0, 5.0, 10.0, 1.5, 2.5)
#: 比率一致とみなす許容誤差
_RATIO_TOLERANCE = 0.02
#: この株数差以下は無視（端株・単元未満株のノイズ）
DEFAULT_SHARE_TOLERANCE = 0.51
#: この評価額（円）以下の差分は無視
DEFAULT_VALUE_TOLERANCE_JPY = 20000.0


# ---------------------------------------------------------------------------
# 同定
# ---------------------------------------------------------------------------


def normalize_symbol(symbol: Any) -> str:
    """照合キー用のシンボル正規化。

    `7203.T` と `7203` を同じものとして扱う。moomoo は `US.AAPL` を返すので
    プレフィックスも落とす。
    """
    s = str(symbol or "").strip().upper()
    if not s:
        return ""
    for prefix in ("US.", "JP.", "HK.", "SH.", "SZ.", "SG."):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    for suffix in (".T", ".JP", ".TO"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    return s


def normalize_name(name: Any) -> str:
    """投信など、シンボルが無い銘柄の同定キー。

    楽天の表記は「iFreeNEXT FANG+」→「iFreeNEXT FANG+インデックス」のように揺れる。
    記号と空白を落として先頭12文字で束ねる（`import_rakuten_csv` と同じ思想）。
    """
    s = str(name or "").strip().upper()
    s = "".join(ch for ch in s if ch.isalnum())
    return s[:12]


def position_key(pos: dict) -> str:
    """ポジションの同定キー。口座区分は**含めない**。

    口座区分を含めると、同じ味の素を特定とNISAで持っているだけで
    「幽霊2件」に化ける。口座差は数量差分の内訳として別に見る。
    """
    sym = normalize_symbol(pos.get("symbol") or pos.get("quote_symbol"))
    if sym:
        return f"sym:{sym}"
    return f"name:{normalize_name(pos.get('name'))}"


def market_of(pos: dict) -> str:
    """このポジションがどの市場か（scope 判定用）。"""
    raw = str(pos.get("symbol") or pos.get("quote_symbol") or "").strip()
    if not raw:
        return "FUND"
    up = raw.upper()
    if up.endswith(".T") or up.endswith(".JP") or up.startswith("JP."):
        return "JP"
    if up.endswith(".HK") or up.startswith("HK."):
        return "HK"
    if up.isdigit():
        return "JP"
    return "US"


# ---------------------------------------------------------------------------
# 集約
# ---------------------------------------------------------------------------


def _num(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def aggregate(positions: list[dict]) -> dict[str, dict]:
    """同一銘柄（複数口座）を1つに束ねる。"""
    out: dict[str, dict] = {}
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        key = position_key(p)
        if not key or key in ("sym:", "name:"):
            continue
        entry = out.setdefault(key, {
            "key": key,
            "symbol": p.get("symbol") or p.get("quote_symbol"),
            "name": p.get("name"),
            "market": market_of(p),
            "shares": 0.0,
            "accounts": [],
            "rows": [],
            "currency": p.get("currency"),
        })
        shares = _num(p.get("shares"))
        if shares is not None:
            entry["shares"] += shares
        if p.get("account"):
            entry["accounts"].append(p.get("account"))
        entry["rows"].append(p)
        if not entry.get("name"):
            entry["name"] = p.get("name")
    return out


# ---------------------------------------------------------------------------
# 差分の分類
# ---------------------------------------------------------------------------


def classify_quantity_diff(
    model_shares: Optional[float],
    broker_shares: Optional[float],
    value_per_share_jpy: Optional[float] = None,
    share_tolerance: float = DEFAULT_SHARE_TOLERANCE,
    value_tolerance_jpy: float = DEFAULT_VALUE_TOLERANCE_JPY,
) -> dict:
    """数量差分を分類する。

    Returns:
        {"classification": ..., "message": ..., "ratio": ..., "auto_fixable": bool}

    分類:
        `match`              一致（許容誤差内）
        `below_threshold`    差はあるが閾値未満（端株等）→ 無視してよい
        `corporate_action`   株式分割/併合の比率に一致 → 模型を自動補正できる
        `unrecorded_trade`   売買が記録されていない可能性
        `unknown`            原因不明 → **レポート最上部に赤字で出す**
    """
    if model_shares is None or broker_shares is None:
        return {"classification": "unknown", "ratio": None, "auto_fixable": False,
                "message": "株数が取得できず比較できません"}

    diff = broker_shares - model_shares
    if abs(diff) <= share_tolerance:
        return {"classification": "match", "ratio": 1.0, "auto_fixable": False,
                "message": "一致"}

    value_diff = abs(diff) * value_per_share_jpy if value_per_share_jpy else None
    if value_diff is not None and value_diff <= value_tolerance_jpy:
        return {"classification": "below_threshold", "ratio": None, "auto_fixable": False,
                "message": f"差 {diff:+,.0f}株（評価額 約¥{value_diff:,.0f}）— 閾値未満"}

    ratio = (broker_shares / model_shares) if model_shares else None
    if ratio:
        for r in _SPLIT_RATIOS:
            if abs(ratio - r) <= _RATIO_TOLERANCE * r:
                return {"classification": "corporate_action", "ratio": r,
                        "auto_fixable": True,
                        "message": f"1:{r:g} 分割の比率に一致（模型を自動補正できます）"}
            if abs(ratio - 1.0 / r) <= _RATIO_TOLERANCE / r:
                return {"classification": "corporate_action", "ratio": 1.0 / r,
                        "auto_fixable": True,
                        "message": f"{r:g}:1 併合の比率に一致（模型を自動補正できます）"}

    # 端数のない整数株の増減は売買の記録漏れが最も自然な説明
    if abs(diff - round(diff)) < 1e-6:
        side = "買い増し" if diff > 0 else "売却"
        return {"classification": "unrecorded_trade", "ratio": ratio, "auto_fixable": False,
                "message": f"{abs(diff):,.0f}株の{side}が模型に記録されていない可能性"}

    return {"classification": "unknown", "ratio": ratio, "auto_fixable": False,
            "message": f"株数差 {diff:+,.4f} の原因が特定できません"}


# ---------------------------------------------------------------------------
# 意図（thesis / 政策）の照会
# ---------------------------------------------------------------------------


def _load_intent(symbol: Optional[str], name: Optional[str]) -> dict:
    """この銘柄に thesis / 政策 があるか。無ければ孤児候補。

    ティッカーの無い投信（FANG+ 等）は `symbol` が None で来る。名前も候補に
    入れないと、**投信は thesis を書いても政策を登録しても永久に孤児のまま**になり、
    孤児率が構造的に下がらない。実際 FANG+ は評価額の 7% を占めている。
    """
    theses: list[dict] = []
    policies: list[dict] = []

    candidates = [c for c in (symbol, normalize_symbol(symbol)) if c]
    if not symbol and name:
        candidates.append(str(name).strip())
    # `7203` 形式でメモが書かれている場合も拾う（表記揺れで孤児を誤検出しない）
    try:
        from src.data.note_manager import load_notes

        for c in candidates:
            for t in ("thesis", "target", "exit-rule"):
                theses.extend(load_notes(symbol=c, note_type=t) or [])
    except Exception:
        theses = []

    try:
        from src.core.policy.ledger import list_policies

        for c in candidates:
            policies.extend(list_policies(symbol=c, active_only=True) or [])
    except Exception:
        policies = []

    # 同じメモを2回拾わない
    seen: set[str] = set()
    uniq_theses = []
    for t in theses:
        tid = str(t.get("id") or t.get("created_at") or t.get("content", "")[:40])
        if tid in seen:
            continue
        seen.add(tid)
        uniq_theses.append(t)

    seen_p: set[str] = set()
    uniq_pol = []
    for p in policies:
        pid = str(p.get("id") or p.get("policy_id") or "")
        if pid and pid in seen_p:
            continue
        seen_p.add(pid)
        uniq_pol.append(p)

    return {"theses": uniq_theses, "policies": uniq_pol,
            "has_thesis": bool(uniq_theses), "has_policy": bool(uniq_pol)}


def _oldest_thesis_date(theses: list[dict]) -> Optional[str]:
    dates = []
    for t in theses:
        d = t.get("date") or t.get("created_at")
        if isinstance(d, dict):
            d = d.get("utc") or d.get("local")
        if isinstance(d, str) and d:
            dates.append(d[:10])
    return min(dates) if dates else None


# ---------------------------------------------------------------------------
# 本体
# ---------------------------------------------------------------------------


def reconcile(
    model_positions: list[dict],
    broker_snapshots: list[dict],
    *,
    values_jpy: Optional[dict[str, float]] = None,
    total_jpy: Optional[float] = None,
    share_tolerance: float = DEFAULT_SHARE_TOLERANCE,
    value_tolerance_jpy: float = DEFAULT_VALUE_TOLERANCE_JPY,
    check_intent: bool = True,
) -> dict:
    """三点照合を実行する。

    Args:
        model_positions: 模型側の保有（`config/weekly_holdings.yaml` の holdings 等）
        broker_snapshots: `src.data.brokers.collect_snapshots()` の戻り
        values_jpy: 照合キー → 評価額（円）。閾値判定と孤児の重要度に使う
        total_jpy: PF総額（孤児の比率算出用）

    Returns:
        照合結果 dict。`blocking=True` なら原因不明差分あり。
    """
    from src.data.brokers import merged_scope

    snaps = list(broker_snapshots or [])
    available = [s for s in snaps if s.get("available")]
    scope = merged_scope(snaps)
    reconcilable = bool(available)

    broker_positions: list[dict] = []
    for s in available:
        broker_positions.extend(s.get("positions") or [])

    model = aggregate(model_positions)
    broker = aggregate(broker_positions)
    values = values_jpy or {}

    diffs: list[dict] = []
    ghosts: list[dict] = []
    unrecorded: list[dict] = []
    unverified: list[dict] = []
    corporate_actions: list[dict] = []
    matched = 0

    for key, m in model.items():
        b = broker.get(key)
        market = m.get("market") or "US"
        if b is None:
            if not reconcilable or market not in scope:
                unverified.append({
                    **_brief(m, values, total_jpy),
                    "reason": ("照合ソースが取得できませんでした" if not reconcilable
                               else f"{market} をカバーするソースが無い（scope: "
                                    f"{sorted(scope) or 'なし'}）"),
                })
                continue
            ghosts.append({
                **_brief(m, values, total_jpy),
                "message": "模型に残存、口座に不在。売却済みで記録漏れの可能性。",
                "impact": "この銘柄はストレステスト・HHI計算から除外すべきです。",
            })
            continue

        vps = _value_per_share(key, m, values)
        cls = classify_quantity_diff(
            m.get("shares"), b.get("shares"), vps,
            share_tolerance=share_tolerance, value_tolerance_jpy=value_tolerance_jpy)

        if cls["classification"] == "match":
            matched += 1
            continue

        row = {
            **_brief(m, values, total_jpy),
            "model_shares": m.get("shares"),
            "broker_shares": b.get("shares"),
            "share_diff": (b.get("shares") or 0) - (m.get("shares") or 0),
            "accounts_model": m.get("accounts"),
            "accounts_broker": b.get("accounts"),
            **cls,
        }
        if cls["classification"] == "below_threshold":
            matched += 1
            continue
        if cls["classification"] == "corporate_action":
            corporate_actions.append(row)
        diffs.append(row)

    for key, b in broker.items():
        if key in model:
            continue
        unrecorded.append({
            **_brief(b, values, total_jpy),
            "broker_shares": b.get("shares"),
            "classification": "unrecorded_position",
            "message": "口座にあるが模型に無い。取り込み漏れ。",
        })

    orphans = _find_orphans(model, values, total_jpy) if check_intent else []
    # 記述状態の4分類（V0）。孤児だけを数えると「政策はあるが理由がない」
    # 保有を見落とす。表示すべきは「孤児0件」ではなく「健全0件」。
    description = (describe_positions(model, values, total_jpy)
                   if check_intent else None)

    unknown = [d for d in diffs if d.get("classification") == "unknown"]
    blocking = bool(unknown or ghosts or unrecorded) or not reconcilable

    # 独立に検証できたか。模型を作った元CSVとの突合は「一致」ではなく循環であり、
    # 取り込み後の売買を原理的に検出できない。ここを ok と呼んではならない。
    independent = [s for s in available
                   if not (s.get("detail") or {}).get("circular")]
    circular_only = bool(available) and not independent

    if not reconcilable:
        status = "unreconciled"
    elif blocking:
        status = "differences"
    elif circular_only:
        status = "circular"
    elif diffs:
        status = "differences_explained"
    else:
        status = "ok"

    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "blocking": blocking,
        "reconcilable": reconcilable,
        "independently_verified": bool(independent),
        "scope": sorted(scope),
        "sources": [_source_view(s) for s in snaps],
        "description": description,
        "counts": {
            "model": len(model),
            "broker": len(broker),
            "matched": matched,
            "diffs": len(diffs),
            "ghosts": len(ghosts),
            "unrecorded": len(unrecorded),
            "unverified": len(unverified),
            "orphans": len(orphans),
        },
        "diffs": diffs,
        "unknown_diffs": unknown,
        "ghosts": ghosts,
        "unrecorded": unrecorded,
        "unverified": unverified,
        "corporate_actions": corporate_actions,
        "orphans": orphans,
        "orphan_burden_pct": orphan_burden(orphans),
        "messages": _messages(status, reconcilable, unknown, ghosts,
                              unrecorded, orphans, snaps),
    }


def _value_per_share(key: str, entry: dict, values: dict) -> Optional[float]:
    total = values.get(key)
    shares = entry.get("shares")
    if isinstance(total, (int, float)) and isinstance(shares, (int, float)) and shares:
        return abs(total / shares)
    return None


def _brief(entry: dict, values: dict, total_jpy: Optional[float]) -> dict:
    v = values.get(entry.get("key"))
    weight = (v / total_jpy * 100.0) if (isinstance(v, (int, float))
                                         and total_jpy) else None
    return {
        "key": entry.get("key"),
        "symbol": entry.get("symbol"),
        "name": entry.get("name"),
        "market": entry.get("market"),
        "shares": entry.get("shares"),
        "value_jpy": v,
        "weight_pct": round(weight, 2) if weight is not None else None,
    }


def describe_positions(model: dict, values: dict,
                       total_jpy: Optional[float]) -> dict:
    """保有の記述状態を4分類する（読書台帳仕様 v2 の V0）。

    🔴 `_find_orphans` は「thesis も政策も無い」というAND条件だったため、
    政策だけがある保有（3xスリーブ・FANG+）を**非孤児として通過させていた**。
    その結果「孤児0件」と表示しつつ、同じレポートの別の節に
    「thesis は2件しか存在しない」と書くという自己矛盾が起きていた。

    正しい表示は「孤児0件」ではなく**「健全0件」**である。
    詳細は `src/core/portfolio/description_state.py`。
    """
    from src.core.portfolio import description_state

    rows = []
    for key, m in model.items():
        intent = _load_intent(m.get("symbol"), m.get("name"))
        brief = _brief(m, values, total_jpy)
        rows.append({
            "symbol": m.get("symbol"),
            "name": m.get("name"),
            "account": m.get("account"),
            "weight_pct": brief.get("weight_pct"),
            "intent": intent,
        })
    return description_state.describe(rows)


def _find_orphans(model: dict, values: dict, total_jpy: Optional[float]) -> list[dict]:
    """thesis も政策も無い保有 = 完全孤児。

    ⚠️ **この関数だけを「なぜ持つかの記述がある」の判定に使ってはならない。**
    政策だけがある保有（根拠なき執行）をここは拾わない。
    全体像は `describe_positions()` を使うこと。後方互換のため残している。
    """
    out: list[dict] = []
    for key, m in model.items():
        intent = _load_intent(m.get("symbol"), m.get("name"))
        if intent["has_thesis"] or intent["has_policy"]:
            continue
        row = _brief(m, values, total_jpy)
        row.update({
            "has_thesis": False,
            "has_policy": False,
            "message": "なぜ持っているかが記述されていません（thesis・政策の双方なし）",
        })
        out.append(row)
    out.sort(key=lambda r: (r.get("weight_pct") or 0.0), reverse=True)
    return out


def orphan_burden(orphans: list[dict]) -> Optional[float]:
    """孤児ポジションが評価額に占める比率（%）。"""
    vals = [o.get("weight_pct") for o in orphans
            if isinstance(o.get("weight_pct"), (int, float))]
    return round(sum(vals), 1) if vals else None


def _source_view(s: dict) -> dict:
    from src.data.brokers.base import snapshot_summary

    return {
        "source": s.get("source"),
        "available": s.get("available"),
        "as_of": s.get("as_of"),
        "age_hours": s.get("age_hours"),
        "stale": s.get("stale"),
        "scope": s.get("scope"),
        "error": s.get("error"),
        "degraded": (s.get("detail") or {}).get("degraded"),
        "degraded_reason": (s.get("detail") or {}).get("degraded_reason"),
        "circular": (s.get("detail") or {}).get("circular"),
        "circular_reason": (s.get("detail") or {}).get("circular_reason"),
        "summary": snapshot_summary(s),
    }


def _messages(status, reconcilable, unknown, ghosts, unrecorded, orphans,
              snaps) -> list[str]:
    msgs: list[str] = []
    if not reconcilable:
        msgs.append(
            "⛔ 照合できませんでした。全ての残高ソースが取得不能です。"
            "以降の数値はすべて**未照合**として扱ってください。")
    for s in snaps:
        if s.get("available") and s.get("stale"):
            age = s.get("age_hours") or 0
            msgs.append(
                f"⚠️ {s.get('source')} のデータが {age / 24:.0f}日前のものです。"
                "この間に売買していれば反映されていません。")
        if s.get("available") and (s.get("detail") or {}).get("degraded"):
            msgs.append(f"⚠️ {s.get('source')}: "
                        f"{(s.get('detail') or {}).get('degraded_reason')}")
        if s.get("available") and (s.get("detail") or {}).get("circular"):
            msgs.append(f"⚠️ {s.get('source')}: "
                        f"{(s.get('detail') or {}).get('circular_reason')}")
        if not s.get("available") and s.get("error"):
            msgs.append(f"ℹ️ {s.get('source')} は使えませんでした: {s.get('error')}")
    if unknown:
        msgs.append(f"🔴 原因不明の差分 {len(unknown)}件。まずこれを解消してください。")
    if ghosts:
        msgs.append(f"⚠️ 幽霊ポジション {len(ghosts)}件 — "
                    "存在しない資産のリスクを計算している状態です。")
    if unrecorded:
        msgs.append(f"⚠️ 未記録ポジション {len(unrecorded)}件 — CSVの取り込みを実行してください。")
    if orphans:
        burden = orphan_burden(orphans)
        tail = f"（評価額の {burden}%）" if burden is not None else ""
        msgs.append(f"⚠️ 孤児ポジション {len(orphans)}件{tail} — "
                    "なぜ持っているかが未記述です。今週の議題候補。")
    if status == "circular":
        msgs.append("🟡 独立した照合は成立していません（模型の生成元と同じデータを見ています）。"
                    "以降の数値は『取り込み時点では正しい』という条件付きで読んでください。")
    if status == "ok":
        msgs.append("✅ 口座と模型は一致しています。")
    return msgs


# ---------------------------------------------------------------------------
# 模型の自動補正（コーポレートアクション由来のみ）
# ---------------------------------------------------------------------------


def apply_corporate_actions(config: dict, result: dict) -> tuple[dict, list[str]]:
    """分割/併合と判定された差分だけ模型の株数を補正する。

    **売買の記録漏れ・原因不明差分は自動補正しない。** 自動で辻褄を合わせると
    「記録習慣の弱点」が可視化されなくなり、照合の目的が失われるため。
    """
    applied: list[str] = []
    if not result.get("corporate_actions"):
        return config, applied

    by_key = {}
    for row in result["corporate_actions"]:
        if row.get("auto_fixable") and row.get("broker_shares") is not None:
            by_key[row["key"]] = row

    for h in config.get("holdings") or []:
        key = position_key({"symbol": h.get("quote_symbol"), "name": h.get("name")})
        row = by_key.get(key)
        if not row:
            continue
        ratio = row.get("ratio")
        if not ratio:
            continue
        old_shares = h.get("shares")
        old_cost = h.get("cost_price")
        if isinstance(old_shares, (int, float)):
            h["shares"] = round(old_shares * ratio, 6)
        if isinstance(old_cost, (int, float)) and ratio:
            h["cost_price"] = round(old_cost / ratio, 6)
        applied.append(
            f"{h.get('name')}（{h.get('quote_symbol')}）: {old_shares}株 → "
            f"{h.get('shares')}株, 取得単価 {old_cost} → {h.get('cost_price')}"
            f"（{row.get('message')}）")
    return config, applied


# ---------------------------------------------------------------------------
# オーケストレーション — CLI と週次パックの共通入口
# ---------------------------------------------------------------------------


def run_reconciliation(
    holdings_config: Optional[dict] = None,
    *,
    report_data: Optional[dict] = None,
    sources: Optional[list[str]] = None,
    csv_path: Optional[str] = None,
    autostart_opend: bool = True,
    check_intent: bool = True,
) -> dict:
    """設定の読み込みからブローカー取得・照合までを1本で行う。

    Args:
        holdings_config: 模型。None なら `config/weekly_holdings.yaml` を読む。
        report_data: `weekly.build_report_data()` の戻り。あれば評価額を使い、
            孤児の比率や差分の金額換算が出る。無くても照合自体は成立する。
    """
    from src.data.brokers import collect_snapshots

    if holdings_config is None:
        from src.core.portfolio.weekly import load_holdings_config

        holdings_config = load_holdings_config()

    values, total = _values_from_report(report_data)
    snaps = collect_snapshots(sources=sources, csv_path=csv_path,
                              autostart_opend=autostart_opend)
    result = reconcile(holdings_config.get("holdings") or [], snaps,
                       values_jpy=values, total_jpy=total,
                       check_intent=check_intent)
    result["model_source"] = (holdings_config.get("source") or {})
    return result


def _values_from_report(report_data: Optional[dict]):
    """`build_report_data` の analyses から 照合キー→評価額 を作る。"""
    if not report_data:
        return {}, None
    values: dict[str, float] = {}
    for a in report_data.get("analyses") or []:
        if not isinstance(a, dict):
            continue
        key = position_key({"symbol": a.get("symbol"), "name": a.get("name")})
        v = _num(a.get("value_jpy"))
        if v is None:
            continue
        values[key] = values.get(key, 0.0) + v
    return values, _num(report_data.get("total_jpy"))


def days_since(iso_date: Optional[str]) -> Optional[int]:
    if not iso_date:
        return None
    try:
        d = datetime.strptime(str(iso_date)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return (date.today() - d).days
