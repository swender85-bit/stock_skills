"""情報量比例レポート — 差分エンジン (土曜設計書 提案8)。

## 名指しする問題

> **毎週同じ長さで届くレポートは、やがて読まれなくなる。**

これは意志の問題ではなく構造の問題である。スケジュールが分量を決める設計では、
情報量ゼロの週にも大量の文章が生成される。読者は「今週も特に何もない」を学習し、
やがて**重要な週の警告も見落とす**。週次レポートの最大の失敗モードは
精度不足ではなく形骸化である。

## 設計原理

レポートを差分生成に変える。前週の**構造化スナップショット**を保持し、
今週の全項目を前週と比較。閾値未満の項目は折り畳む。全項目が閾値未満なら
数行で終える —— そしてそれは失敗ではなく**正しい出力**である。

## 緩慢な変化の罠

前週比だけを見ると「毎週1%ずつ悪化」が永久に閾値に引っかからない。
したがって **4週前比・13週前比の累積差分を必ず併設する**（設計書 提案8-⑧）。

## 折り畳みは削除ではない

閾値未満の項目は `folded` に入るが、消えない。常に展開できる。
「静穏週」でも「システムは正常に動作し、N項目を点検した」と明示する
（動いているか不安にさせないため）。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

#: スナップショットの保存先（gitignore 対象）
DEFAULT_SNAPSHOT_DIR = "data/weekly_snapshots"

SNAPSHOT_VERSION = 1

#: 「変化した」とみなす閾値。設定ファイル(config/thresholds.yaml の weekly_diff)で上書き可。
DEFAULT_THRESHOLDS: dict[str, float] = {
    "price_pct": 5.0,            # 株価の週次変化率
    "weight_pct": 2.0,           # 構成比の変化（ポイント）
    "pl_pct": 5.0,               # 含み損益率の変化（ポイント）
    "per_pct": 15.0,             # PER の相対変化率
    "pbr_pct": 15.0,
    "rsi": 10.0,                 # RSI の変化（ポイント）
    "total_jpy_pct": 3.0,        # PF総額の変化率
    "hhi": 0.03,                 # HHI の変化
}

#: 累積差分を見る週数。緩慢な悪化を捕まえるための第二の網。
CUMULATIVE_WINDOWS = (4, 13)

#: 累積側の閾値は「週次閾値 × この倍率」。単純加算にすると厳しすぎる。
CUMULATIVE_MULTIPLIER = {4: 1.6, 13: 2.6}


def _threshold(name: str) -> float:
    try:
        from src.core._thresholds import th

        return float(th("weekly_diff", name, DEFAULT_THRESHOLDS[name]))
    except Exception:
        return DEFAULT_THRESHOLDS[name]


# ---------------------------------------------------------------------------
# スナップショット
# ---------------------------------------------------------------------------


def build_snapshot(pack: dict) -> dict:
    """ブリーフィングパックから、差分比較用の構造化スナップショットを作る。

    Markdown ではなく**数値の辞書**を保存する。文章を diff しても
    「何が変わったか」は取り出せないため。
    """
    pf = pack.get("portfolio") or {}
    rec = pack.get("reconciliation") or {}

    holdings: dict[str, dict] = {}
    for h in pack.get("holdings") or []:
        key = _key(h)
        if not key:
            continue
        f = h.get("fundamentals") or {}
        t = h.get("technicals") or {}
        row = holdings.setdefault(key, {
            "name": h.get("name"), "symbol": h.get("symbol"),
            "price": h.get("price"), "weight_pct": 0.0,
            "pl_pct": h.get("pl_pct"),
            "per": f.get("per"), "pbr": f.get("pbr"),
            "rsi": t.get("rsi"),
        })
        # 複数口座は構成比だけ合算し、価格や指標は同じなので上書きしない
        w = h.get("weight_pct")
        if isinstance(w, (int, float)):
            row["weight_pct"] = (row.get("weight_pct") or 0.0) + w

    return {
        "version": SNAPSHOT_VERSION,
        "date": (pack.get("meta") or {}).get("as_of") or date.today().isoformat(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "portfolio": {
            "total_jpy": pf.get("total_jpy"),
            "cash_jpy": pf.get("cash_jpy"),
            "pl_pct": pf.get("pl_pct"),
        },
        "holdings": holdings,
        "reconciliation": {
            "status": rec.get("status"),
            "orphans": (rec.get("counts") or {}).get("orphans"),
            "ghosts": (rec.get("counts") or {}).get("ghosts"),
            "unrecorded": (rec.get("counts") or {}).get("unrecorded"),
        },
        "policies": _policy_snapshot(),
        # 翌週の日程変更（決算日の前倒し/後ろ倒し）は前週と比べないと検出できない。
        "forward_calendar": pack.get("forward_calendar"),
    }


def _key(h: dict) -> str:
    from src.core.portfolio.reconciliation import position_key

    return position_key({"symbol": h.get("symbol"), "name": h.get("name")})


def _policy_snapshot() -> dict:
    try:
        from src.core.policy.ledger import list_policies

        pols = list_policies(active_only=True) or []
    except Exception:
        return {"active": None, "ids": []}
    return {"active": len(pols),
            "ids": sorted(str(p.get("id") or p.get("policy_id") or "") for p in pols)}


def save_snapshot(snapshot: dict, base_dir: str = DEFAULT_SNAPSHOT_DIR) -> Optional[Path]:
    try:
        d = Path(base_dir)
        d.mkdir(parents=True, exist_ok=True)
        day = str(snapshot.get("date") or date.today().isoformat()).replace("-", "")
        path = d / f"{day}.json"
        path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2,
                                   default=str), encoding="utf-8")
        return path
    except Exception:
        return None


def load_snapshots(base_dir: str = DEFAULT_SNAPSHOT_DIR) -> list[dict]:
    """古い順に全スナップショットを返す。壊れたファイルは飛ばす。"""
    d = Path(base_dir)
    if not d.exists():
        return []
    out: list[dict] = []
    for p in sorted(d.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict):
            out.append(data)
    out.sort(key=lambda s: str(s.get("date") or ""))
    return out


def prior_snapshot(snapshots: list[dict], weeks_back: int = 1,
                   today: Optional[str] = None) -> Optional[dict]:
    """N週前のスナップショット。無ければ None（**代わりに直近を使わない**）。

    「4週前比」と称して2週前のデータを使うと、緩慢な変化の検出が壊れる。
    """
    today = today or date.today().isoformat()
    past = [s for s in snapshots if str(s.get("date") or "") < today]
    if len(past) < weeks_back:
        return None
    return past[-weeks_back]


# ---------------------------------------------------------------------------
# 差分
# ---------------------------------------------------------------------------


def _num(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _delta(cur: Any, prev: Any) -> Optional[float]:
    c, p = _num(cur), _num(prev)
    return None if (c is None or p is None) else c - p


def _pct_delta(cur: Any, prev: Any) -> Optional[float]:
    c, p = _num(cur), _num(prev)
    if c is None or p is None or p == 0:
        return None
    return (c - p) / abs(p) * 100.0


def _change(label: str, kind: str, cur, prev, threshold: float,
            *, relative: bool, unit: str, key: Optional[str] = None) -> Optional[dict]:
    d = _pct_delta(cur, prev) if relative else _delta(cur, prev)
    if d is None:
        return None
    return {
        "key": key or kind,
        "label": label,
        "kind": kind,
        "current": cur,
        "previous": prev,
        "delta": round(d, 2),
        "threshold": threshold,
        "unit": unit,
        "significant": abs(d) >= threshold,
    }


def diff_snapshots(current: dict, previous: Optional[dict]) -> dict:
    """今週と前週を比較し、有意な変化と折り畳む変化に分ける。"""
    if not previous:
        return {"available": False, "reason": "前週のスナップショットがありません（初回実行）",
                "changes": [], "folded": [], "checked": 0}

    changes: list[dict] = []

    cp, pp = current.get("portfolio") or {}, previous.get("portfolio") or {}
    for label, field, thr_name, rel, unit in (
        ("PF総額", "total_jpy", "total_jpy_pct", True, "%"),
        ("含み損益率", "pl_pct", "pl_pct", False, "pt"),
    ):
        c = _change(label, "portfolio", cp.get(field), pp.get(field),
                    _threshold(thr_name), relative=rel, unit=unit,
                    key=f"portfolio.{field}")
        if c:
            changes.append(c)

    ch, ph = current.get("holdings") or {}, previous.get("holdings") or {}
    for key, cur in ch.items():
        prev = ph.get(key)
        name = cur.get("name") or key
        if prev is None:
            changes.append({"key": key, "label": name, "kind": "holding_new",
                            "current": cur.get("weight_pct"), "previous": None,
                            "delta": None, "unit": "%", "significant": True,
                            "note": "前週には無かった保有です"})
            continue
        for label, field, thr_name, rel, unit in (
            ("株価", "price", "price_pct", True, "%"),
            ("構成比", "weight_pct", "weight_pct", False, "pt"),
            ("含み損益率", "pl_pct", "pl_pct", False, "pt"),
            ("PER", "per", "per_pct", True, "%"),
            ("PBR", "pbr", "pbr_pct", True, "%"),
            ("RSI", "rsi", "rsi", False, "pt"),
        ):
            c = _change(f"{name} {label}", "holding", cur.get(field), prev.get(field),
                        _threshold(thr_name), relative=rel, unit=unit,
                        key=f"{key}.{field}")
            if c:
                changes.append(c)

    for key in ph.keys() - ch.keys():
        changes.append({"key": key, "label": ph[key].get("name") or key,
                        "kind": "holding_gone", "current": None,
                        "previous": ph[key].get("weight_pct"), "delta": None,
                        "unit": "%", "significant": True,
                        "note": "前週にあった保有が今週ありません"})

    changes.extend(_reconciliation_changes(current, previous))

    significant = [c for c in changes if c.get("significant")]
    folded = [c for c in changes if not c.get("significant")]
    return {
        "available": True,
        "previous_date": previous.get("date"),
        "changes": significant,
        "folded": folded,
        "checked": len(changes),
    }


def _reconciliation_changes(current: dict, previous: dict) -> list[dict]:
    cr, pr = current.get("reconciliation") or {}, previous.get("reconciliation") or {}
    out: list[dict] = []
    for label, field in (("孤児ポジション", "orphans"), ("幽霊ポジション", "ghosts"),
                         ("未記録ポジション", "unrecorded")):
        c, p = _num(cr.get(field)), _num(pr.get(field))
        if c is None or p is None or c == p:
            continue
        out.append({"key": f"reconciliation.{field}", "label": label,
                    "kind": "reconciliation", "current": c, "previous": p,
                    "delta": c - p, "unit": "件",
                    # 件数の増減は1件でも意味がある。閾値で潰さない。
                    "significant": True})
    if cr.get("status") != pr.get("status"):
        out.append({"key": "reconciliation.status", "label": "照合ステータス",
                    "kind": "reconciliation", "current": cr.get("status"),
                    "previous": pr.get("status"), "delta": None, "unit": "",
                    "significant": True})
    return out


def cumulative_diff(current: dict, snapshots: list[dict]) -> dict:
    """4週前・13週前との累積差分。**緩慢な悪化**を捕まえるための網。

    前週比だけを見ていると「毎週1%ずつ悪化」が永久に閾値を超えず見逃される。
    """
    out: dict[str, Any] = {"windows": {}, "slow_drift": []}
    for weeks in CUMULATIVE_WINDOWS:
        prev = prior_snapshot(snapshots, weeks_back=weeks,
                              today=str(current.get("date") or ""))
        if not prev:
            out["windows"][f"{weeks}w"] = {
                "available": False,
                "reason": f"{weeks}週前のスナップショットがまだありません（蓄積中）"}
            continue
        d = diff_snapshots(current, prev)
        mult = CUMULATIVE_MULTIPLIER.get(weeks, 2.0)
        drifts = [c for c in (d["changes"] + d["folded"])
                  if isinstance(c.get("delta"), (int, float))
                  and isinstance(c.get("threshold"), (int, float))
                  and abs(c["delta"]) >= c["threshold"] * mult]
        out["windows"][f"{weeks}w"] = {
            "available": True, "previous_date": prev.get("date"),
            "significant": len(d["changes"]), "drifts": drifts,
        }
        for c in drifts:
            out["slow_drift"].append({**c, "window_weeks": weeks})
    return out


# ---------------------------------------------------------------------------
# 情報量の判定
# ---------------------------------------------------------------------------


def assess_information(
    diff: dict,
    cumulative: Optional[dict] = None,
    *,
    falsified: Optional[list] = None,
    falsification: Optional[dict] = None,
    reconciliation: Optional[dict] = None,
    forward: Optional[dict] = None,
) -> dict:
    """今週の情報量を判定し、レポートの分量を決める。

    「静穏週」は失敗ではなく正しい出力である。
    **何もしないことが正しい**と明示できる設計は、個人投資家の最大の損失源
    （過剰行動）に対する直接の対策になる。
    """
    actionable: list[dict] = []

    for f in falsified or []:
        actionable.append({
            "priority": 1, "kind": "belief",
            "title": f"{f.get('symbol') or f.get('label') or '保有'} の反証条件が成立",
            "detail": f.get("message") or f.get("condition"),
        })

    actionable.extend(_falsification_gaps(falsification))

    rec = reconciliation or {}
    if rec.get("unknown_diffs"):
        actionable.append({"priority": 2, "kind": "reconciliation",
                           "title": f"原因不明の差分 {len(rec['unknown_diffs'])}件",
                           "detail": "照合が通っていません。まずこれを解消してください。"})
    if rec.get("ghosts"):
        actionable.append({"priority": 2, "kind": "reconciliation",
                           "title": f"幽霊ポジション {len(rec['ghosts'])}件",
                           "detail": "存在しない資産のリスクを計算している状態です。"})
    if rec.get("unrecorded"):
        actionable.append({"priority": 2, "kind": "reconciliation",
                           "title": f"未記録ポジション {len(rec['unrecorded'])}件",
                           "detail": "口座にあるが模型に無い保有です。CSVを取り込んでください。"})
    # 「照合できていない」は静穏ではない。ここを見逃すと、模型が現実から
    # 乖離したまま『今週は何もしなくていい』と言い切ってしまう。
    if rec.get("status") == "unreconciled":
        actionable.append({"priority": 2, "kind": "reconciliation",
                           "title": "残高の照合ができていません",
                           "detail": "以降の全数値は未照合です。静穏かどうかも判定できません。"})
    elif rec.get("status") == "circular" or rec.get("independently_verified") is False:
        actionable.append({
            "priority": 3, "kind": "reconciliation",
            "title": "残高の独立検証ができていません",
            "detail": "模型の生成元と同じデータを見ています。"
                      "楽天証券から新しいCSVを保存すると独立照合になります。"})
    if rec.get("orphans"):
        burden = rec.get("orphan_burden_pct")
        tail = f"（評価額の {burden}%）" if burden is not None else ""
        actionable.append({
            "priority": 3, "kind": "reconciliation",
            "title": f"孤児ポジション {len(rec['orphans'])}件{tail}",
            "detail": "なぜ持っているかが未記述です。損切りも利確も基準がありません。"})

    for item in (forward or {}).get("actionable", []) or []:
        actionable.append({"priority": 3, "kind": "forward",
                           "title": item.get("title"),
                           "detail": item.get("detail")})

    changes = diff.get("changes") or []
    for c in changes:
        if c.get("kind") in ("holding_new", "holding_gone"):
            actionable.append({"priority": 3, "kind": "holding",
                               "title": f"{c.get('label')}: {c.get('note')}",
                               "detail": None})

    slow = (cumulative or {}).get("slow_drift") or []
    for s in slow[:5]:
        actionable.append({
            "priority": 4, "kind": "slow_drift",
            "title": f"{s.get('label')} が {s.get('window_weeks')}週かけて "
                     f"{s.get('delta'):+.1f}{s.get('unit')} 変化",
            "detail": "週次では閾値に届かない緩慢な変化です。",
        })

    actionable.sort(key=lambda a: a.get("priority", 9))
    quiet = not actionable and len(changes) == 0

    return {
        "verdict": "静穏週" if quiet else "要対応週",
        "quiet": quiet,
        "actionable": actionable,
        "actionable_count": len(actionable),
        "changed_count": len(changes),
        "checked_count": diff.get("checked", 0),
        "folded_count": len(diff.get("folded") or []),
        "guidance": (
            "先週からの実質的な変化はありません。今週は何もしないことが正しい選択です。"
            if quiet else
            f"要対応 {len(actionable)}件。上から順に処理してください。"
        ),
    }


def _falsification_gaps(falsification: Optional[dict]) -> list[dict]:
    """反証条件が無い/点検できなかった thesis を議題に上げる。

    反証条件の無いテーゼは週次で点検する対象を持たない。これを黙って静穏扱いすると、
    「点検していない」が「問題なし」に化ける。
    """
    f = falsification or {}
    out: list[dict] = []
    near = f.get("near") or []
    if near:
        out.append({"priority": 2, "kind": "belief",
                    "title": f"反証条件に接近している保有 {len(near)}件",
                    "detail": "; ".join(
                        str(r.get("name") or r.get("symbol")) for r in near[:5])})
    missing = f.get("missing") or []
    if missing:
        out.append({
            "priority": 3, "kind": "belief",
            "title": f"反証条件が未定義の thesis {len(missing)}件",
            "detail": "『何が起きたら間違いと認めるか』が無いテーゼは点検できません。"})
    unchecked = f.get("unchecked") or []
    if unchecked:
        out.append({
            "priority": 4, "kind": "belief",
            "title": f"指標が取れず点検できなかった thesis {len(unchecked)}件",
            "detail": "これは『問題なし』ではなく『未点検』です。"})
    return out
