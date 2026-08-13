#!/usr/bin/env python3
"""週次PF深掘りレポートの完全無人ヘッドレス駆動。

## なぜ2層なのか

従来の週次レポート（`weekly_report.py`）は Python の文字列整形で数字を並べるだけで、
「材料 → 保有株への含意」をつなぐ解釈層が無かった。ここでは:

    層1  ブリーフィングパック生成（Python・トークン0）… build_briefing_pack.py
    層2  Claude synthesis（headless `claude -p`）……… .claude/prompts/weekly_deep.md

の2層に分け、**執筆を節単位のチェックポイント**にする。1節=1回の `claude -p` 呼び出しで、
書けた節はファイルに落として state.json に記録する。使用量上限に当たったら中断コード(2)で
落ち、**次の起動が続きから再開する**。人の操作は要らない。

## 使い方

    python scripts/weekly_deep_driver.py                # 生成→執筆→vault同期
    python scripts/weekly_deep_driver.py --dry-run      # vault同期せず output/ のみ
    python scripts/weekly_deep_driver.py --restart      # 途中状態を捨てて最初から
    python scripts/weekly_deep_driver.py --max-sections 3   # 1回の起動で3節だけ書く
    python scripts/weekly_deep_driver.py --pack output/briefing/PF_20260725.json

## 終了コード

    0 … 全節完了・保存済み
    2 … 使用量上限などで中断（state は残る。再実行すれば続きから）
    1 … 異常終了
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

SPEC_PATH = REPO / ".claude" / "prompts" / "weekly_deep.md"
DEFAULT_OUT = REPO / "output" / "weekly_deep"

#: 中断（＝再開すればよい）と判定する文言。異常終了と区別する。
LIMIT_MARKERS = (
    "usage limit", "rate limit", "rate_limit", "quota",
    "上限", "too many requests", "overloaded", "capacity",
    "insufficient credit", "credit balance",
)

EXIT_OK, EXIT_ERROR, EXIT_INTERRUPTED = 0, 1, 2


def log(msg: str) -> None:
    # 出力先が閉じても（`| head` 等）執筆を止めない。無人実行の途中死を防ぐ。
    try:
        print(f"[deep] {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# パックのスライス — 節ごとに必要な材料だけ渡してトークンを節約する
# ---------------------------------------------------------------------------


def _slim_holding(h: dict) -> dict:
    """一覧用の軽い保有ビュー（節の文脈用）。"""
    return {k: h.get(k) for k in (
        "name", "symbol", "weight_pct", "value_jpy", "pl_pct",
        "week_change_pct", "leverage",
    )}


#: 過熱の判定に要る中身の項目だけ。**ニュース本文は落とす**（別の節の材料）。
_CONSTITUENT_HEAT_KEYS = (
    "symbol", "name", "effective_pct", "via", "held_directly",
    "week_change_pct", "month_change_pct", "quarter_change_pct",
    "rsi14", "sma200_deviation_pct", "range_52w_position", "from_high_pct",
    "volatility_pct", "next_earnings", "days_to_earnings", "signals",
)


def _slim_constituents(data: Optional[dict]) -> Optional[dict]:
    """構成銘柄を過熱判定に要る項目だけに絞る。

    全 dossier をそのまま渡すと1節で3万字を超える。落とすのはニュース本文と
    ファンダの明細で、**取得できたかどうか（`available` / `covered_pct` /
    `missing_news`）は必ず残す** — 縮めたことが「材料が無かった」に化けないため。
    """
    if not isinstance(data, dict):
        return data
    out = {k: v for k, v in data.items() if k != "dossiers"}
    out["dossiers"] = [{k: d.get(k) for k in _CONSTITUENT_HEAT_KEYS if k in d}
                       for d in data.get("dossiers") or []]
    out["slimmed"] = "ニュース本文とファンダ明細は他節の材料のため省略"
    return out


def _sum(rows: list[dict], key: str) -> Optional[float]:
    vals = [r.get(key) for r in rows if isinstance(r.get(key), (int, float))]
    return sum(vals) if vals else None


def _aggregate_rows(symbol: str, rows: list[dict]) -> dict:
    """同一銘柄が複数口座（特定/NISA等）に分かれている場合の合算ビュー。"""
    return {
        "symbol": symbol,
        "name": rows[0].get("name") if rows else None,
        "accounts": [r.get("account") for r in rows],
        "shares": _sum(rows, "shares"),
        "value_jpy": _sum(rows, "value_jpy"),
        "pl_jpy": _sum(rows, "pl_jpy"),
        "weight_pct": _sum(rows, "weight_pct"),
        "rows": len(rows),
    }


def holding_key(h: dict) -> str:
    """節をまとめるキー。ティッカーが無い投信（FANG+等）は名前で束ねる。

    シンボル無しを捨てると**保有の一部が黙ってレポートから消える**ので、必ず拾う。
    """
    return str(h.get("symbol") or f"name:{h.get('name') or '無名'}")


def group_holdings(pack: dict) -> list[tuple[str, list[dict]]]:
    """保有をキーでまとめ、合算比率の降順で返す。

    同じ銘柄を特定口座と NISA で持っている場合に**節が二重になるのを防ぐ**。
    """
    groups: dict[str, list[dict]] = {}
    for h in pack.get("holdings") or []:
        groups.setdefault(holding_key(h), []).append(h)
    return sorted(groups.items(),
                  key=lambda kv: (_sum(kv[1], "weight_pct") or 0.0), reverse=True)


def slice_pack(pack: dict, section: dict) -> dict:
    """節が必要とする材料だけを抜き出す。"""
    kind = section["kind"]
    # 照合の状態は**全節に渡す**。未照合のまま数値を確定値として語らせないため。
    meta = {
        "meta": pack.get("meta"),
        "portfolio": pack.get("portfolio"),
        "reconciliation_status": _rec_status(pack),
        "information": pack.get("information"),
    }

    # --- 土曜設計書 第3章 の固定骨格 ---

    if kind == "verdict":
        # 判定は本文を全部書いた後に書くが、**仕様 §0 は本文に無い材料も要求する**:
        # 監視対象（`watch_plan`）とレジーム（`regime`）である。
        # ⚠️ この2つが欠けていたため、仕様の 🔴 指示が両方とも空振りしていた。
        # 「ETF経由の曝露を保有と並べるな」「レジームを判定の直後に置け」は、
        # 材料が届いて初めて守れる。
        return {**meta,
                "week_diff": pack.get("week_diff"),
                "cumulative_diff": pack.get("cumulative_diff"),
                "reconciliation": pack.get("reconciliation"),
                "falsification": pack.get("falsification"),
                "watch_plan": pack.get("watch_plan"),
                "regime": pack.get("regime"),
                "indices": pack.get("indices"),
                "forward_actionable": (pack.get("forward") or {}).get("actionable")}

    if kind == "intake":
        # 🔴 読書台帳は**価格に依存しない**。打ち切り週でもこの節は出す。
        return {**meta, "reading": pack.get("reading")}

    if kind == "reconcile":
        # 節1 は保有の記述状態（V0）も書く。`reconciliation.description` に入っている。
        return {**meta, "reconciliation": pack.get("reconciliation")}

    if kind == "belief":
        # 信念の変化は**開示原文**で検証するのが筋。価格ではなく事実で見る。
        return {**meta,
                "falsification": pack.get("falsification"),
                "primary_filings": pack.get("primary_filings"),
                "holdings_overview": [_slim_holding(h)
                                      for h in pack.get("holdings") or []],
                # 系譜の根（V5）と概念の健全性（V4）。**価格に依存しない材料。**
                "reading_genealogy": (pack.get("reading") or {}).get("genealogy")}

    if kind == "forward":
        ext = pack.get("external_views") or {}
        return {**meta,
                "forward": pack.get("forward"),
                # 仕様 §3 は `forward_horizon`（数ヶ月先）と `constituents`
                # （中身の企業の決算）を「必ず書け」と指示している。
                # ⚠️ ここに渡していなかったため、その指示は**届いていなかった**。
                # パックに入れただけでは節には届かない。
                "forward_horizon": pack.get("forward_horizon"),
                "constituents": pack.get("constituents"),
                "moomoo": pack.get("moomoo"),
                "indices": pack.get("indices"),
                # マクロ系の外部見解だけ渡す（改善5）。銘柄別は holding 節へ。
                "external_views": {"available": ext.get("available"),
                                   "note": ext.get("note"),
                                   "views": ext.get("macro_views") or []},
                "holdings_overview": [_slim_holding(h)
                                      for h in pack.get("holdings") or []]}

    if kind == "constraints":
        return {**meta,
                "constraints": pack.get("constraints"),
                "positions_assumptions": pack.get("positions_assumptions"),
                "vol_calibration": pack.get("vol_calibration"),
                "holdings_overview": [_slim_holding(h)
                                      for h in pack.get("holdings") or []]}

    if kind == "decide":
        # 事前決定は「これまでに書いた本文」から導く。政策の材料だけ渡す。
        return {**meta,
                "forward": pack.get("forward"),
                "constraints": pack.get("constraints"),
                "falsification": pack.get("falsification")}

    if kind == "audit":
        return {**meta,
                "cumulative_diff": pack.get("cumulative_diff"),
                "week_diff": {"folded_count": len(
                    (pack.get("week_diff") or {}).get("folded") or [])},
                "execution_audit": pack.get("execution_audit"),
                "model_audit": pack.get("model_audit"),
                # 投信の構成が指数と整合しているかの自己検証。**模型の健全性**なので
                # 監査節の材料。これも今までどの節にも渡っていなかった。
                "composition_check": pack.get("composition_check"),
                "prior_context": pack.get("prior_context")}

    # --- 機会セクション（銘柄別・過熱）は従来のスライスを流用 ---

    if kind == "macro":
        return {**meta,
                "indices": pack.get("indices"),
                "market_news": pack.get("market_news"),
                "moomoo": pack.get("moomoo"),
                "forward_schedule": pack.get("forward_schedule"),
                "holdings_overview": [_slim_holding(h) for h in pack.get("holdings") or []]}

    if kind == "schedule":
        return {**meta,
                "forward_schedule": pack.get("forward_schedule"),
                "moomoo": pack.get("moomoo"),
                "holdings_overview": [_slim_holding(h) for h in pack.get("holdings") or []],
                "competitors": {h.get("symbol"): h.get("competitors")
                                for h in pack.get("holdings") or [] if h.get("competitors")}}

    if kind == "holding":
        key = section["key"]
        sym = section.get("symbol")
        rows = [h for h in pack.get("holdings") or [] if holding_key(h) == key]
        cal = (pack.get("forward") or {}).get("calendar") or {}
        return {**meta,
                "holding_rows": rows,
                "aggregate": _aggregate_rows(key, rows),
                "indices": pack.get("indices"),
                "news": (pack.get("holding_news") or {}).get(sym) or [],
                "forward_schedule": [e for e in pack.get("forward_schedule") or []
                                     if not e.get("symbol") or e.get("symbol") == sym],
                # 日程の「状態」。空リスト＝取得失敗ではないことを明示する材料。
                "schedule_status": (pack.get("schedule_status") or {}).get(sym),
                # 翌週の確定イベント（提案4）はこの銘柄の分だけ渡す
                "next_week_events": [
                    e for e in (cal.get("events") or []) + (cal.get("folded") or [])
                    if e.get("symbol") == sym],
                # 物語混雑度（提案7）— 「上がっているが持つ理由がない」の判定材料
                "crowding": ((pack.get("narrative") or {}).get("crowding") or {}).get(
                    sym or f"name:{rows[0].get('name') if rows else ''}"),
                # この銘柄に言及した外部見解（改善5）。citation に従って書く。
                "external_views": {
                    "available": (pack.get("external_views") or {}).get("available"),
                    "note": (pack.get("external_views") or {}).get("note"),
                    "views": ((pack.get("external_views") or {}).get("by_symbol")
                              or {}).get(sym) or [],
                },
                "prior_context": pack.get("prior_context")}

    if kind == "heat":
        return {**meta,
                "holdings_technicals": [
                    {"symbol": h.get("symbol"), "name": h.get("name"),
                     "weight_pct": h.get("weight_pct"), "pl_jpy": h.get("pl_jpy"),
                     "pl_pct": h.get("pl_pct"), "technicals": h.get("technicals")}
                    for h in pack.get("holdings") or []],
                # 仕様 §5 の「レバレッジ・スリーブの実体を必ず書く」に必要。
                # **ETF の過熱は、中身の過熱である。** ETF 単体の RSI が中立でも
                # 中身が過熱していることがあるので、両方を同じ皿に載せる。
                "leverage_sleeve": pack.get("leverage_sleeve"),
                "constituents": _slim_constituents(pack.get("constituents")),
                "indices": pack.get("indices")}

    if kind == "outlook":
        # 短期(1ヶ月)/中期(3ヶ月)/長期(半年以降)の見通しを書く節。
        # **ここまでの材料を全部使う節なので、渡す材料も一番広い。**
        # 数字（projection）だけ渡すと「レンジを転記しただけ」の節になるため、
        # 何がそのレンジを決めるか（中身の決算・レジーム・ドラッグ）を必ず添える。
        return {**meta,
                "projection": pack.get("projection"),
                "vol_calibration": pack.get("vol_calibration"),
                "positions_assumptions": pack.get("positions_assumptions"),
                "scenarios": pack.get("scenarios"),
                "regime": pack.get("regime"),
                "leverage_sleeve": pack.get("leverage_sleeve"),
                "forward_horizon": pack.get("forward_horizon"),
                # 見通しに要るのは「どの決算がいつ来るか」と signal の分布であって
                # ニュース本文ではない（それは §3 と銘柄別節が扱う）。
                "constituents": _slim_constituents(pack.get("constituents")),
                "monthly_contribution": pack.get("monthly_contribution"),
                "holdings_overview": [_slim_holding(h)
                                      for h in pack.get("holdings") or []]}

    if kind == "cumulative":
        return {**meta,
                "prior_context": pack.get("prior_context"),
                "wow_deltas": [{"symbol": h.get("symbol"), "name": h.get("name"),
                                "wow_delta": h.get("wow_delta")}
                               for h in pack.get("holdings") or []],
                "projection": pack.get("projection"),
                "scenarios": pack.get("scenarios")}

    if kind == "limits":
        # 系譜サマリを書く節。一次観測が何件あったかがここの中身になる。
        return {**meta,
                "reading": pack.get("reading"),
                "vol_calibration": pack.get("vol_calibration"),
                "positions_assumptions": pack.get("positions_assumptions"),
                "primary_filings": {
                    k: v for k, v in (pack.get("primary_filings") or {}).items()
                    if k != "by_symbol"},
                "projection": pack.get("projection")}

    # actions / summary は「これまでに書いた本文」が材料なので meta だけ
    return meta


# ---------------------------------------------------------------------------
# 節の定義
# ---------------------------------------------------------------------------


def _rec_status(pack: dict) -> dict:
    """照合の状態を全節に渡すための最小ビュー。

    未照合・独立検証なしのまま、下流の節が数値を確定値として語るのを防ぐ。
    """
    rec = pack.get("reconciliation") or {}
    return {
        "status": rec.get("status"),
        "blocking": rec.get("blocking"),
        "independently_verified": rec.get("independently_verified"),
        "messages": rec.get("messages"),
    }


def is_quiet_week(pack: dict) -> bool:
    """静穏週かどうか。真ならレポートを数行で終える（提案8）。"""
    return bool((pack.get("information") or {}).get("quiet"))


def build_sections(pack: dict) -> list[dict]:
    """土曜設計書 第3章の固定骨格で節リストを組み立てる（実行順）。

    ```
    0. 判定        — 情報量（静穏週かどうか）※最後に書いて先頭に置く
    1. 照合        — 私の模型は現実と一致しているか
    2. 信念の変化  — 反証条件に触れたテーゼはあるか
    3. 前方イベント — 翌週に何が確定して起きるか
    4. 制約        — 政策・税・現金・流動性・注意
    5. 機会        — 制約内での保有の立ち位置（銘柄別・過熱横断）
    6. 事前決定    — 翌週の条件付き政策 ← 土曜の唯一の「行動」
    7. 監査        — 執行・模型の健全性（静穏時は折り畳み）
    8. 前提と限界  — 系譜サマリ
    ```

    **順序は意思決定論的に固定されており、変更しない。** 機会を先に出すのは
    自宅が燃えているかを確認する前に買い物に行くのと同じ。

    静穏週（`information.quiet`）は銘柄別の節を作らず、機会は1節に畳む。
    分量をスケジュールでなく情報量に比例させるため（提案8）。
    """
    quiet = is_quiet_week(pack)

    sections: list[dict] = [
        {"id": "reconcile", "kind": "reconcile", "order": 20,
         "heading": "## 1. 照合 — 模型は現実と一致しているか",
         "spec": ("仕様の「### 1. 照合」に従って書く。"
                  "原因不明の差分・幽霊・未記録・孤児を、材料にあるものだけ書く。"
                  "照合できなかった銘柄は『一致』でも『幽霊』でもなく**残高不明**と書く。")},
        {"id": "belief", "kind": "belief", "order": 30,
         "heading": "## 2. 信念の変化 — 反証条件の点検",
         "spec": ("仕様の「### 2. 信念の変化」に従って書く。"
                  "価格ではなく信念の変化を見る節。反証成立は売り推奨ではなく議題。"
                  "`unchecked` は『問題なし』ではなく『未点検』と書く。")},
        {"id": "forward", "kind": "forward", "order": 40,
         "heading": "## 3. 前方イベント — 翌週に確定して起きること",
         "spec": ("仕様の「### 3. 前方イベント」に従って書く。"
                  "**政策カバレッジの穴**を必ず書く（土曜に決めるしかない）。"
                  "先物・ADRは予測ではなく市場の織り込み。断定しない。"
                  "日程が取れなかった銘柄は『予定なし』ではないと明記する。")},
        {"id": "constraints", "kind": "constraints", "order": 50,
         "heading": "## 4. 制約 — 行動可能な空間",
         "spec": ("仕様の「### 4. 制約」に従って書く。"
                  "実現損益が null なら『未記録』と書き、0円と書かない。"
                  "NISA使用額が推定なら『（推定）』と明示する。"
                  "含み損の税務価値には必ず『損切りの推奨ではない』を添える。")},
    ]

    if quiet:
        # 静穏週は銘柄別に展開しない。1節に畳んで折り畳み見出しだけ残す。
        sections.append({
            "id": "opportunity", "kind": "heat", "order": 60,
            "heading": "## 5. 機会 — 保有の立ち位置（静穏週のため要約）",
            "spec": ("静穏週なので**10行以内**で書く。"
                     "過熱/売られすぎの多数決判定とPF加重RSIだけを述べ、"
                     "銘柄別の詳細は `[折り畳み] 銘柄別の詳細 N件` と書いて省略する。"
                     "算出できる指標が1つも無ければ『中立』ではなく『判定不能』と書く。"),
        })
    else:
        for i, (key, rows) in enumerate(group_holdings(pack)):
            sym = rows[0].get("symbol")
            name = rows[0].get("name") or key
            label = f"{name}（{sym}）" if sym else name
            sections.append({
                "id": "holding_" + "".join(
                    c if c.isalnum() else "_" for c in key)[:40],
                "kind": "holding", "key": key, "symbol": sym, "order": 60 + i,
                "heading": f"### {label}",
                "spec": ("仕様の「### 5. 機会」の6点（値動きと損益／前週からの変化／"
                         "翌週の日程／競合の含意／物語混雑度／翌週決めるべきこと）を"
                         "**この銘柄について**順に書く。"
                         "見出しは渡した heading をそのまま使う。"
                         "同一銘柄が複数口座にある場合は `holding_rows` に全行が入るので"
                         "**合算(`aggregate`)で語り**、口座差は必要なときだけ触れる。"
                         "**銘柄推奨を書くな。** 書くのは立ち位置と『翌週どういう条件なら"
                         "何をするか』の材料（第0原則: 土曜は買う銘柄を答える日ではない）。"),
            })
        sections.append({
            "id": "heat", "kind": "heat", "order": 78,
            "heading": "### 過熱 / 売られすぎ 横断ビュー",
            "spec": ("仕様の「### 5. 機会」末尾の横断ビューに従って書く。"
                     "多数決で判定し、算出できる指標が無ければ『判定不能』と書く。"
                     "PF加重RSIが過熱圏なら新規買い増しを戒める。"),
        })

    sections += [
        # **静穏週でも必ず書く。** 「今週は何も起きなかった」と
        # 「これからどこへ向かうか分からない」は別のことだから。
        {"id": "outlook", "kind": "outlook", "order": 79,
         "heading": "## 5.9 これから — 短期(1ヶ月) / 中期(3ヶ月) / 長期(半年以降)",
         "spec": ("仕様の「### 5.9 これから」に従って書く。"
                  "各ホライズンで **①統計レンジ ②何がその範囲を決めるか（具体的な出来事）"
                  "③外れるとしたら何が起きたときか（観測点つき）** の3点を必ず書く。"
                  "**確率を書くな。** レバレッジの非対称性（往復すると戻らない）を"
                  "必ず具体額で示す。短期・中期・長期で結論が同じなら分析していない —"
                  "時間軸ごとに効く要因が違うことを書く。"
                  "レンジは前提の帰結であって予測ではない旨を添え、"
                  "`vol_calibration` があれば前提σと実測σの乖離に触れる。"),
         "needs_body": True},
        {"id": "decide", "kind": "decide", "order": 80,
         "heading": "## 6. 事前決定 — 翌週の条件付き政策",
         "spec": ("仕様の「### 6. 事前決定」に従って書く。**これが土曜の唯一の行動。**"
                  "材料は**これまでに書いた本文**。政策は測定可能な条件＋失効期限必須。"
                  "登録コマンドをそのまま貼れる形で書く。"
                  "売買を伴う政策には税引後の損益分岐と入金代替案を添える。"
                  "『何もしない』を明示的な決定として書いてよい。"),
         "needs_body": True},
        {"id": "audit", "kind": "audit", "order": 90,
         "heading": "## 7. 監査 — 執行と模型の健全性",
         "spec": ("仕様の「### 7. 監査」に従って書く。"
                  "累積差分（4週・13週）の緩慢な変化を必ず見る。"
                  "材料が薄ければ数行で終えてよい。"
                  "**偏食監査（`reading.audit`）をここに含める。**"
                  "最小サンプル未達の指標は値を出さず『蓄積中(N/M件)』とだけ書く。"
                  "禁止表現: 『〜すべきです』『〜できていません』『偏っています』、目標値の提示。"),
         "needs_body": True},
        {"id": "intake", "kind": "intake", "order": 92,
         "heading": "## 8. 今週の取り込み",
         "spec": ("材料は `reading`。**取り込み0件でも必ず1行出す**（折り畳まない）。"
                  "取り込みが止まっていることは、この層における最も重要な故障だから。"
                  "件数・provenance の内訳・情報遅延の中央値・概念の更新・"
                  "thesis 草稿の提案を書く。"
                  "🔴 vault が読めなかった場合は『取り込み0件』ではなく"
                  "**『読書台帳を読めなかった』**と書く。"),
         "needs_body": False},
        {"id": "limits", "kind": "limits", "order": 95,
         "heading": "## 9. 前提と限界 / 根拠の系譜",
         "spec": "仕様の「### 8. 前提と限界 + 系譜サマリ」に従って書く。",
         "needs_body": True},
        {"id": "verdict", "kind": "verdict", "order": 10,
         "heading": "## 0. 今週の判定",
         "spec": ("仕様の「### 0. 今週の判定」に従って書く。"
                  "材料は `information` と**これまでに書いた本文全体**。"
                  "`information.quiet` が真なら『静穏週（要対応0件）』とし、"
                  "『先週からの実質的な変化はありません。今週は何もしないことが"
                  "正しい選択です』と明言し、点検項目数も示す。"
                  "偽なら `information.actionable` を優先度順に番号付きで列挙する。"
                  "**銘柄の買い推奨を書かないこと**（第0原則）。"),
         "needs_body": True},
    ]
    return sections


# ---------------------------------------------------------------------------
# 節ごとのモデル配分（改善2 / rightmodel）
# ---------------------------------------------------------------------------

MODELS_CFG_PATH = REPO / "config" / "synthesis_models.yaml"

#: 通過率100%を必須とする節。ここは安い方に落とさない。
#:   節1 照合 … ここが通らないと以降の全数値が条件付きになる
#:   節6 事前決定 … 土曜の唯一の成果物であり、誤りが直接ポジションに効く
CRITICAL_SECTIONS = ("reconcile", "decide")


def load_section_models(path: Optional[Path] = None) -> dict:
    """`config/synthesis_models.yaml` を読む。無ければ空（＝全節が既定モデル）。

    **実測前は意図的に空に近い状態で出荷する。** 推測でモデルを落とすと、
    どの節が劣化したのか分からないまま品質が下がる。埋めるのは
    `eval_synthesis.py --sweep` の実測が出てから。
    """
    p = path or MODELS_CFG_PATH
    if not p.exists():
        return {}
    try:
        import yaml

        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        log(f"synthesis_models.yaml を読めませんでした（既定モデルを使います）: {exc}")
        return {}


def resolve_section_model(section: dict, fallback: str, cfg: Optional[dict] = None) -> str:
    """この節をどのモデルで書くか。

    優先順: 節ID指定 > 節kind指定 > 設定のdefault > CLI/環境変数の既定。
    **critical な節は、実測が無い限り既定（＝最上位）から下げない。**
    """
    cfg = cfg or {}
    sections = cfg.get("sections") or {}
    kinds = cfg.get("kinds") or {}
    critical = tuple(cfg.get("critical_sections") or CRITICAL_SECTIONS)

    chosen = (
        sections.get(section.get("id"))
        or kinds.get(section.get("kind"))
        or cfg.get("default")
        or fallback
    )
    if section.get("id") in critical and not cfg.get("measured_as_of"):
        # 実測されていない状態で重要節を下げるのは、根拠のない品質低下でしかない
        return cfg.get("default") or fallback
    return str(chosen)


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------


def state_path(out_dir: Path, day: str) -> Path:
    return out_dir / f"state_{day}.json"


def load_state(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def find_resumable(out_dir: Path, max_age_days: int = 3) -> Optional[Path]:
    """未完了の state を新しい順に探す。

    上限中断からの再開が翌日にずれ込んでも、**同じ週のレポートを書き継ぐ**ため。
    日付をまたぐと別ファイルになって最初からやり直す、という事故を防ぐ。
    """
    today = date.today()
    best: Optional[tuple[str, Path]] = None
    for p in out_dir.glob("state_*.json"):
        st = load_state(p)
        if not st or st.get("finished"):
            continue
        day = str(st.get("date") or "")
        try:
            age = (today - datetime.strptime(day, "%Y%m%d").date()).days
        except Exception:
            continue
        if 0 <= age <= max_age_days and (best is None or day > best[0]):
            best = (day, p)
    return best[1] if best else None


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# headless claude 呼び出し
# ---------------------------------------------------------------------------


def claude_bin() -> Optional[str]:
    return os.environ.get("CLAUDE_BIN") or shutil.which("claude")


def kill_tree(pid: int) -> None:
    """子孫ごと止める。Windows では孫（node）が残るとタイムアウトが効かない。"""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=30)
        else:
            os.kill(pid, 9)
    except Exception:
        pass


def looks_like_limit(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in LIMIT_MARKERS)


def run_claude(prompt: str, model: str, timeout: int) -> dict:
    """`claude -p` を1回叩く。

    戻り値: {ok, text, interrupted, error, cost_usd, duration_sec, usage}

    `cost_usd` / `duration_sec` / `usage` を返すのは改善2（モデル配分を実測で決める）
    のため。**節ごとのコストと所要時間が記録されていない**と、どのモデルを
    どの節に置くべきかを推測でしか決められない。
    """
    exe = claude_bin()
    if not exe:
        return {"ok": False, "interrupted": False,
                "error": "claude CLI が見つかりません（CLAUDE_BIN で指定可）"}
    started = time.time()

    cmd = [exe, "-p", "--output-format", "json", "--allowedTools", ""]
    if model:
        cmd += ["--model", model]

    # パイプではなくファイルで受け渡す。Windows では子を kill しても孫がパイプを
    # 掴んだままだと subprocess.run が復帰せず、タイムアウトが効かない事故になる。
    with tempfile.TemporaryDirectory(prefix="weekly_deep_") as tmp:
        tmpd = Path(tmp)
        in_p, out_p, err_p = tmpd / "in.txt", tmpd / "out.json", tmpd / "err.txt"
        in_p.write_text(prompt, encoding="utf-8")
        with in_p.open("r", encoding="utf-8") as fin, \
                out_p.open("w", encoding="utf-8") as fout, \
                err_p.open("w", encoding="utf-8") as ferr:
            proc = subprocess.Popen(cmd, stdin=fin, stdout=fout, stderr=ferr,
                                    cwd=str(REPO))
            try:
                returncode = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                kill_tree(proc.pid)
                return {"ok": False, "interrupted": True,
                        "duration_sec": round(time.time() - started, 1),
                        "error": f"タイムアウト({timeout}s)"}
        raw = out_p.read_text(encoding="utf-8", errors="replace").strip()
        errtxt = err_p.read_text(encoding="utf-8", errors="replace").strip()

    payload: dict[str, Any] = {}
    for candidate in (raw, raw[raw.find("{"):] if "{" in raw else ""):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            payload = parsed
            break

    text = (payload.get("result") or "").strip() if payload else raw
    elapsed = round(time.time() - started, 1)
    usage = (payload.get("usage") if isinstance(payload.get("usage"), dict) else {}) or {}

    if returncode != 0 or (payload and payload.get("is_error")):
        blob = f"{text}\n{errtxt}"
        return {"ok": False, "interrupted": looks_like_limit(blob),
                "duration_sec": elapsed,
                "error": (blob.strip()[:600] or f"exit={returncode}")}
    if not text:
        return {"ok": False, "interrupted": False, "duration_sec": elapsed,
                "error": "空の応答"}
    return {"ok": True, "interrupted": False, "text": text,
            "cost_usd": (payload.get("total_cost_usd") if payload else None),
            "duration_sec": elapsed,
            "usage": {
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
            }}


def build_prompt(spec: str, section: dict, material: dict, body_so_far: str) -> str:
    parts = [
        "あなたは投資家本人に向けた週次ポートフォリオ深掘りレポートの、**指定された1節だけ**を書く。",
        "",
        "# 執筆仕様（全体）",
        spec,
        "",
        "# いま書く節",
        f"- 見出し: `{section['heading']}`",
        f"- 指示: {section['spec']}",
        "",
        "# 出力ルール（厳守）",
        "- **この節の Markdown 本文だけ**を出力する。前置き・後書き・コードフェンス・"
        "「承知しました」の類は一切書かない。",
        "- 冒頭は必ず上記の見出し行から始める。",
        "- 渡された材料に無い数字を作らない。空・欠損は「取得できず」と明示する。",
        "- 他の節の内容を重複して書かない。",
        "",
        "# 材料（ブリーフィングパックの該当部分・JSON）",
        "```json",
        json.dumps(material, ensure_ascii=False, indent=2)[:180000],
        "```",
    ]
    if section.get("needs_body") and body_so_far:
        parts += [
            "",
            "# これまでに書いた本文（この節の材料）",
            body_so_far[-60000:],
        ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 組み立て
# ---------------------------------------------------------------------------


def _fmt(v, unit: str = "", digits: int = 0) -> str:
    if v is None:
        return "取得できず"
    if isinstance(v, (int, float)):
        return f"{v:,.{digits}f}{unit}"
    return str(v)


def _import_label(source: Any) -> str:
    """`source:` ブロックを1行の読める形にする（dict をそのまま出さない）。"""
    if not isinstance(source, dict):
        return str(source or "不明")
    kind = source.get("type") or "不明"
    when = str(source.get("exported_at") or "")[:10]
    name = source.get("file")
    parts = [kind]
    if when:
        parts.append(when)
    if name:
        parts.append(str(name))
    return " / ".join(parts)


def build_header(pack: dict) -> str:
    meta = pack.get("meta") or {}
    pf = pack.get("portfolio") or {}
    today = meta.get("as_of") or date.today().isoformat()
    lines = [
        "---",
        f"title: 週次PF分析 {today}",
        "tags: [投資, ポートフォリオ, 週次レポート]",
        f"created: {today}",
        "---",
        "",
        f"# 週次ポートフォリオ深掘り分析 {today}",
        "",
        f"- 総資産: ¥{_fmt(pf.get('total_jpy'))}"
        f"（うち現金 ¥{_fmt(pf.get('cash_jpy'))}）"
        f"{(pf.get('coverage') or {}).get('note') or ''}",
        # 損益率は分子と分母の母集団が一致するときだけ出る（混合分母の抑制）。
        # 金額のほうにも母数を必ず添える。**抑制した比率の隣に、母数のない
        # 合計が並ぶのは同じ誤読を招く。**
        f"- 評価損益: ¥{_fmt(pf.get('total_pl_jpy'))}"
        f"{(pf.get('pl_coverage') or {}).get('note') or ''}"
        + (f"（{_fmt(pf.get('pl_pct'), '%', 1)}）" if not pf.get("pl_pct_suppressed")
           else f" ⚠️ 損益率は非表示: {pf.get('pl_pct_suppressed_reason')}"),
        f"- 為替: {_fmt(meta.get('fx_rate'), ' 円/USD', 2)}",
        f"- 保有データ源: {meta.get('holdings_source') or '不明'}"
        f" / 取り込み: {_import_label(meta.get('holdings_import'))}",
    ]
    # 照合の状態はヘッダに出す。未照合のまま数値を読ませないため（提案1）。
    rec = pack.get("reconciliation") or {}
    if rec:
        label = {
            "ok": "✅ 一致", "differences_explained": "🟡 差分あり（説明済み）",
            "differences": "🔴 要対応の差分あり", "circular": "🟡 独立検証なし",
            "unreconciled": "⛔ 照合不能",
        }.get(rec.get("status"), rec.get("status") or "不明")
        counts = rec.get("counts") or {}
        lines.append(f"- 三点照合: {label}"
                     f"（口座 {counts.get('broker', '—')}銘柄 / "
                     f"模型 {counts.get('model', '—')}銘柄 / "
                     f"孤児 {counts.get('orphans', '—')}件）")

    info = pack.get("information") or {}
    if info:
        checked = info.get("checked_count", 0)
        # 初回は前週スナップショットが無く点検0になる。動いていないと誤読させない。
        checked_txt = (f"点検 {checked}項目" if checked else
                       "前週比較なし（初回 or 前週データ欠損）")
        lines.append(f"- 今週の判定: {info.get('verdict')}"
                     f"（要対応 {info.get('actionable_count', 0)}件 / {checked_txt}）")

    for w in meta.get("warnings") or []:
        lines.append(f"- ⚠️ {w}")
    lines.append("")
    return "\n".join(lines)


def missing_sections(state: dict, out_dir: Path) -> list:
    """本文に載らなかった節を、理由つきで列挙する。

    🔴 2026-08-08、執筆に失敗した3節（監査・限界・判定）が本文から
    **無言で削除**され、目次にも痕跡が残らなかった。
    「取得できなかったものを『無かった』と書くな」を全節に課しているシステムが、
    自分自身の欠落についてだけはそれを守らなかった。
    """
    out = []
    for s in sorted(state.get("sections") or [], key=lambda x: x.get("order", 0)):
        readable = False
        if s.get("status") == "done" and s.get("file"):
            try:
                readable = bool((out_dir / s["file"]).read_text(encoding="utf-8").strip())
            except Exception:
                readable = False
        if readable:
            continue
        out.append({
            "order": s.get("order"),
            "heading": str(s.get("heading") or s.get("key") or "(無題)").lstrip("# "),
            "status": s.get("status") or "unknown",
            "attempts": s.get("attempts", 0),
            "reason": s.get("error") or {
                "pending": "未着手のまま打ち切られました",
                "failed": "執筆に失敗しました",
                "done": "ファイルが読めませんでした（保存に失敗）",
            }.get(s.get("status"), "理由不明"),
        })
    return out


def build_missing_block(missing: list) -> str:
    """欠落節の告知ブロック。**本文の最初に置く。**"""
    if not missing:
        return ""
    lines = ["## ⚠️ このレポートには欠落があります", "",
             f"以下の {len(missing)} 節は本文に含まれていません。"
             "**書かれなかったのであって、「該当なし」ではありません。**", "",
             "| 節 | 状態 | 試行 | 理由 |", "|---|---|---|---|"]
    for m in missing:
        lines.append(f"| {m['heading']} | {m['status']} | {m['attempts']}回 | {m['reason']} |")
    lines += ["", "この欠落を埋めるには次を実行してください:", "",
              "```bash", "python scripts/weekly_deep_driver.py --resume-only", "```"]
    return "\n".join(lines)


def build_abort_report(pack: dict, quality: dict, day: str) -> str:
    """データが揃わない週の、正しい出力（診断書 C-3）。

    **保有の過半で価格が取れない週は、投資分析を打ち切る。**
    770行の欠測報告を書くのではなく、次の4つだけを出す。

    1. 何が取れなかったか
    2. どこで落ちたか
    3. 修理のために次に走らせるコマンド
    4. 分析は行っていないという明示

    行数上限は `config/thresholds.yaml` の `data_quality.abort_report_max_lines`。
    """
    meta = pack.get("meta") or {}
    pf = pack.get("portfolio") or {}
    net = meta.get("network") or {}
    missing = quality.get("missing") or []
    reasons = quality.get("reasons") or {}
    failures = pf.get("price_failures") or []

    lines = [
        "---",
        f"title: 週次PF分析 {day}（欠測により分析なし）",
        "tags: [投資, ポートフォリオ, 週次レポート, 欠測]",
        f"created: {day}",
        "---",
        "",
        f"# 週次PF分析 {day} — 分析は行っていません",
        "",
        "## 結論",
        "",
        f"**このレポートに投資判断はありません。** 価格カバレッジが "
        f"{quality.get('priced', 0)}/{quality.get('total', 0)} "
        f"（{(quality.get('price_coverage') or 0) * 100:.0f}%、必要 "
        f"{(quality.get('min_required') or 0) * 100:.0f}%）で、"
        "**書ける材料が揃っていない**ためです。",
        "",
        "これは「値動きが無かった」でも「材料が無かった」でもありません。"
        "**取りに行けなかった**という意味です。",
        "",
        "## 何が取れなかったか",
        "",
    ]
    if missing:
        lines += ["| 銘柄 | 取得側が残した理由 |", "|---|---|"]
        for sym in missing[:30]:
            lines.append(f"| {sym} | {reasons.get(sym) or '理由未記録'} |")
    else:
        lines.append("（欠測銘柄の記録がありません。これ自体が取得層の不備です）")

    lines += ["", "## どこで落ちたか", ""]
    if net:
        lines.append(f"- ネットワーク: ready={net.get('ready')} / {net.get('message') or '—'}")
    timings = meta.get("timings_sec") or {}
    if timings:
        lines.append(f"- 価格・保有の取得にかかった時間: {timings.get('prices_and_holdings')}秒"
                     "（極端に短ければ、繋がる前に取りに行っています）")
    for f in failures[:10]:
        lines.append(f"- {f.get('id')}: {f.get('reason')}")
    if not net and not timings and not failures:
        lines.append("- 落下点の記録がありません（取得層がエラーを残していません）")

    lines += [
        "",
        "## 次に走らせるコマンド",
        "",
        "```bash",
        "python scripts/weekly_deep_driver.py --resume-only   # 取り直して書き継ぐ",
        "python scripts/build_briefing_pack.py                # パックだけ作り直す",
        "python scripts/weekly_deep_driver.py --force-low-quality  # 承知の上で書かせる",
        "```",
        "",
        "## 明示",
        "",
        "分析・推奨・事前決定は**一切行っていません**。"
        "この週のポジションについて何かを判断する材料として、この文書を使わないでください。",
        "",
    ]
    return "\n".join(lines)


def save_report(text: str, day: str, dry_run: bool = False) -> str:
    """レポートを output/ と vault に届ける。欠測報告も同じ経路で届ける。"""
    filename = f"週次PF分析_{day}.md"
    if dry_run:
        return filename
    from src.output.sync import save_and_sync

    result = save_and_sync(text, filename)
    for m in result.get("messages", []):
        log(f"save: {m}")
    return str(result.get("synced_path") or result.get("output_path") or filename)


def assemble(state: dict, out_dir: Path, pack: dict) -> str:
    body = [build_header(pack)]
    missing = missing_sections(state, out_dir)
    if missing:
        body.append(build_missing_block(missing))
    opportunity_heading_done = False
    for s in sorted(state["sections"], key=lambda x: x["order"]):
        if s.get("status") != "done" or not s.get("file"):
            continue
        try:
            text = (out_dir / s["file"]).read_text(encoding="utf-8").strip()
        except Exception:
            continue
        # 機会セクションの小見出し群（`###`）に親見出しを1つだけ立てる。
        # 静穏週は機会が1節に畳まれて自前の `##` を持つので、ここは通らない。
        if (s.get("kind") in ("holding", "heat")
                and str(s.get("heading", "")).startswith("### ")
                and not opportunity_heading_done):
            body.append("## 5. 機会 — 保有の立ち位置（保有比率順）")
            opportunity_heading_done = True
        body.append(text)
    return "\n\n".join(p for p in body if p) + "\n"


def body_for_prompt(state: dict, out_dir: Path) -> str:
    parts = []
    for s in sorted(state["sections"], key=lambda x: x["order"]):
        if s.get("status") == "done" and s.get("file"):
            try:
                parts.append((out_dir / s["file"]).read_text(encoding="utf-8").strip())
            except Exception:
                pass
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def refresh_critics(days: int = 7) -> None:
    """外部批評家の直近発言を取り込む（改善5）。

    パック生成の**前**に走らせる。パック側は台帳を読むだけなので、
    ここで取れなければ「今週の見解なし」ではなく「取得できなかった」として
    レポートに出る。失敗しても週次は止めない（全体が opt-in の材料）。
    """
    try:
        proc = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "fetch_critics.py"),
             "--days", str(days), "--apply"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(REPO), timeout=600)
        tail = [l for l in (proc.stdout or "").splitlines() if l.strip()][-3:]
        for line in tail:
            log(f"critics: {line}")
    except Exception as exc:
        log(f"critics: 取り込みをスキップしました（{type(exc).__name__}）")


def ensure_pack(pack_arg: Optional[str], out_dir: Path, no_moomoo: bool) -> Optional[Path]:
    if pack_arg:
        p = Path(pack_arg)
        return p if p.exists() else None
    cmd = [sys.executable, str(REPO / "scripts" / "build_briefing_pack.py")]
    if no_moomoo:
        cmd.append("--no-moomoo")
    log("ブリーフィングパックを生成中...")
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd=str(REPO))
    for line in (proc.stderr or "").splitlines():
        log(line)
    if proc.returncode != 0:
        return None
    last = [l.strip() for l in (proc.stdout or "").splitlines() if l.strip()]
    return Path(last[-1]) if last else None


def main() -> int:
    ap = argparse.ArgumentParser(description="週次PF深掘りレポート 無人駆動")
    ap.add_argument("--pack", help="既存のブリーフィングパック JSON")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument("--model", default=os.environ.get("WEEKLY_DEEP_MODEL", "opus"))
    ap.add_argument("--timeout", type=int, default=900, help="1節あたりの秒数")
    ap.add_argument("--max-sections", type=int, default=0, help="1回の起動で書く節数上限(0=無制限)")
    ap.add_argument("--max-attempts", type=int, default=2, help="1節あたりの再試行回数")
    ap.add_argument("--restart", action="store_true", help="途中状態を捨てて最初から")
    ap.add_argument("--resume-within-days", type=int, default=3,
                    help="日付をまたいだ未完了レポートを引き継ぐ日数")
    ap.add_argument("--resume-only", action="store_true",
                    help="未完了の途中状態がある時だけ動く（再開タスク用。無ければ何もせず終了）")
    ap.add_argument("--dry-run", action="store_true", help="vault 同期せず output/ のみ")
    ap.add_argument("--no-moomoo", action="store_true")
    ap.add_argument("--no-critics", action="store_true",
                    help="外部批評家(X)の取り込みをスキップする")
    ap.add_argument("--critic-days", type=int, default=7,
                    help="批評家の発言を何日分取り込むか")
    ap.add_argument("--force-low-quality", action="store_true",
                    help="価格が取れていないパックでも強制的にレポートを書く（非推奨）")
    args = ap.parse_args()

    # 🔒 実行中はスリープへ戻らせない。
    #
    # 「PC はスタンバイで蓋を閉じたまま自動で出る」という前提で組んでいるのに、
    # **実行中にスリープへ戻らない保証を一切していなかった。**
    # システムログ実測では3時間ごとに起床して数秒で寝ており、
    # 5〜10分かかる週次が途中で寝落ちすればネットワークが切れる
    # （2026-08-08 の「前半全滅・後半成功」と同じ形になる）。
    # 画面は点けないので、蓋を閉じたまま暗いまま動く。
    from src.core.power import keep_awake

    with keep_awake("週次レポート"):
        return _run(args)


def _run(args) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    day = date.today().strftime("%Y%m%d")
    spath = state_path(out_dir, day)

    state = None if args.restart else load_state(spath)

    if state and state.get("finished"):
        log(f"本日分は完了済み: {state.get('report_path')}")
        return EXIT_OK

    # 日付をまたいだ再開（上限中断が翌日に持ち越された場合）
    if state is None and not args.restart:
        prior = find_resumable(out_dir, args.resume_within_days)
        if prior is not None:
            state = load_state(prior)
            spath = prior
            day = str(state.get("date") or day)
            log(f"未完了の週次({day})を引き継いで再開します。")

    # 欠測で打ち切った週の state は「取り直す予定がある」という記録である。
    # 節を1つも持たないので、そのまま再開すると空のレポートを組み立ててしまう。
    # 再開時はパックから作り直す（＝取得のやり直し）。
    # `--resume-only` でもここは動く。**動かないと打ち切りが放置になる**（H5）。
    retry_after_abort = bool(state and state.get("aborted_low_quality"))
    if retry_after_abort:
        log("前回は欠測で打ち切っています。パックを作り直して取り直します。")
        state = None

    if state:
        pack_path = Path(state["pack_path"])
        if not pack_path.exists():
            log("記録されたパックが見つかりません。作り直します。")
            state = None

    if not state and args.resume_only and not retry_after_abort:
        log("再開すべき途中状態はありません（--resume-only のため何もしません）。")
        return EXIT_OK

    if not state:
        if not args.no_critics:
            refresh_critics(args.critic_days)
        pack_path = ensure_pack(args.pack, out_dir, args.no_moomoo)
        if not pack_path or not pack_path.exists():
            log("❌ パックを用意できませんでした")
            return EXIT_ERROR
        pack = json.loads(pack_path.read_text(encoding="utf-8"))

        # 🔴 価格が取れていないパックからレポートを書かない。
        #
        # 2026-08-08、10銘柄中9銘柄の価格が null のままレポートが生成・保存され、
        # 全節が「取得できず」で埋まった状態で vault に届いた。
        # **書けない材料から書かせたことが最大の失敗**であり、
        # 正しい動作は「書かずに中断し、次の起動で取り直す」。
        # WeeklyDeepResume が3時間ごとに動くので、放置しても自動で回復する。
        quality = (pack.get("meta") or {}).get("data_quality") or {}
        if quality and not quality.get("usable", True) and not args.force_low_quality:
            log(f"⛔ {quality.get('verdict')}")
            log("   投資分析は行いません。欠測報告だけを出します。")
            log("   すぐ書かせたい場合: --force-low-quality")
            try:
                (out_dir / f"lowquality_{day}.json").write_text(
                    json.dumps({"at": datetime.now(timezone.utc).isoformat(),
                                "pack": str(pack_path), "quality": quality,
                                "network": (pack.get("meta") or {}).get("network")},
                               ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass

            # 🔴 打ち切り時も state を必ず残す（H5）。
            #
            # 以前はここで state を保存する前に return しており、
            # `--resume-only` の再開タスクは state が無いと何もしないため、
            # **その週はレポートが出ないまま誰も気づかない**状態だった。
            # ログの「3時間後に取り直します」は事実に反していた。
            state = {
                "date": day,
                "pack_path": str(pack_path.resolve()),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "model": args.model,
                "finished": False,
                "report_path": None,
                "aborted_low_quality": True,
                "quality": quality,
                "sections": [],
            }
            save_state(spath, state)

            report = build_abort_report(pack, quality, day)
            if args.dry_run:
                print(report)
            else:
                saved = save_report(report, day, dry_run=False)
                log(f"欠測報告を保存しました: {saved}")
            return EXIT_INTERRUPTED
        state = {
            "date": day,
            "pack_path": str(pack_path.resolve()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "model": args.model,
            "finished": False,
            "report_path": None,
            "sections": [
                {**{k: v for k, v in s.items()},
                 "status": "pending", "attempts": 0, "file": None}
                for s in build_sections(pack)
            ],
        }
        save_state(spath, state)
    else:
        pack = json.loads(Path(state["pack_path"]).read_text(encoding="utf-8"))

    models_cfg = load_section_models()
    if models_cfg.get("measured_as_of"):
        log(f"節別モデル配分を適用します（実測 {models_cfg['measured_as_of']}）")

    spec = SPEC_PATH.read_text(encoding="utf-8") if SPEC_PATH.exists() else ""
    if not spec:
        log(f"❌ 執筆仕様が見つかりません: {SPEC_PATH}")
        return EXIT_ERROR

    sec_dir = out_dir / day
    sec_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    for s in state["sections"]:
        if s.get("status") == "done":
            continue
        if s.get("attempts", 0) >= args.max_attempts:
            log(f"⏭️  {s['id']}: 再試行上限に達しているため飛ばします")
            continue
        if args.max_sections and written >= args.max_sections:
            log(f"この起動の上限({args.max_sections}節)に達しました。次回続きから。")
            save_state(spath, state)
            return EXIT_INTERRUPTED

        section_model = resolve_section_model(s, args.model, models_cfg)
        log(f"✍️  {s['id']} を執筆中（{written + 1}節目 / model={section_model}）...")
        material = slice_pack(pack, s)
        body = body_for_prompt(state, out_dir) if s.get("needs_body") else ""
        prompt = build_prompt(spec, s, material, body)

        t0 = time.time()
        s["attempts"] = s.get("attempts", 0) + 1
        res = run_claude(prompt, section_model, args.timeout)

        if res.get("ok"):
            fname = f"{s['order']:02d}_{s['id']}.md"
            (sec_dir / fname).write_text(res["text"] + "\n", encoding="utf-8")
            s["status"] = "done"
            s["file"] = f"{day}/{fname}"
            # 節ごとの実測（改善2: モデル配分を推測でなく実測で決めるための入力）
            s["cost_usd"] = res.get("cost_usd")
            s["duration_sec"] = res.get("duration_sec")
            s["usage"] = res.get("usage")
            s["model_used"] = section_model
            written += 1
            # ログより先に state を確定させる（ログ側の失敗で節を書き直さないため）
            save_state(spath, state)
            log(f"   ✅ {s['id']} 完了（{time.time() - t0:.0f}s）")
            continue

        s["status"] = "failed"
        s["error"] = res.get("error")
        save_state(spath, state)
        if res.get("interrupted"):
            log(f"⏸️  中断（使用量上限/タイムアウトの可能性）: {res.get('error')}")
            log("   次の起動で続きから再開します。")
            return EXIT_INTERRUPTED
        log(f"   ⚠️ {s['id']} 失敗: {res.get('error')}")

    remaining = [s for s in state["sections"]
                 if s.get("status") != "done" and s.get("attempts", 0) < args.max_attempts]
    if remaining:
        log(f"未完了 {len(remaining)} 節が残っています。次の起動で再開します。")
        save_state(spath, state)
        return EXIT_INTERRUPTED

    report = assemble(state, out_dir, pack)
    done = sum(1 for s in state["sections"] if s.get("status") == "done")
    log(f"全{len(state['sections'])}節中 {done}節を執筆。レポートを組み立てました。")

    if args.dry_run:
        print(report)
        state["finished"] = True
        save_state(spath, state)
        return EXIT_OK

    filename = f"週次PF分析_{day}.md"
    try:
        from src.output.sync import resync_missing, save_and_sync

        # 過去に届けたはずのレポートが vault から消えていないか毎回見る。
        # 8/1 分は同期ログ上「成功」だったのに翌日には消えていた。
        try:
            back = resync_missing()
            for m in back.get("messages", []):
                log(f"resync: {m}")
        except Exception as e:
            log(f"resync: スキップ（{type(e).__name__}）")

        result = save_and_sync(report, filename)
        for m in result.get("messages", []):
            log(f"save: {m}")
        state["report_path"] = result.get("output_path")
        state["synced_path"] = result.get("synced_path")
        verify = result.get("verify") or {}
        if verify and verify.get("ok") is False:
            log(f"❌ 検証NG: {verify}")
            save_state(spath, state)
            return EXIT_ERROR
        state["finished"] = True
        save_state(spath, state)
        print(f"✅ 週次深掘りレポート: {result.get('output_path')}")
        if result.get("synced_path"):
            print(f"   vault: {result['synced_path']}")
        elif result.get("degraded"):
            print("   ⚠️ vault へ同期できず output/ のみ保存しました。")
    except Exception as e:
        log(f"❌ 保存に失敗: {e}")
        save_state(spath, state)
        return EXIT_ERROR

    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
