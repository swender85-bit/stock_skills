#!/usr/bin/env python3
"""孤児ポジションに政策を入れる (改善3).

## 名指しする問題

`docs/STRUCTURE.md` §17.2 の最大の穴:

> 孤児ポジション6件（評価額の79.1%）— thesis も政策も無い。
> 損切りも利確も判断基準が無い状態

政策台帳（`src/core/policy/`）も判断パッケージ（`src/core/decision/`）も
**機械は完成しているが、中身が入っていない**。空の台帳は
`deviation.py`（逸脱監査）も `evaluator.py`（トリガー距離）も無意味にする。
急変時に「政策ドメインを先に引く」というルーティングも、引くものが無ければ機能しない。

## QCOM の二層

STRUCTURE.md §17.2 は「撤退ライン $158」、実運用は「$172.12（リスクライン）/
$211（出口）」で、**同じ銘柄に2つの真実**があった。ユーザーの判断は
**両方を別レイヤーとして残す**:

| 水準 | 層 | 意味 | 行動 |
|:---|:---|:---|:---|
| $211 | 政策（conditional_commit） | 出口 | 段階売却を起動 |
| $172.12 | 政策（awaiting_trigger） | リスクライン | **弾再生順序をMDT優先に固定。損切りはしない** |
| $158 | **反証条件**（thesis 側） | テーゼが間違っていた証拠 | 売却ではなく**テーゼ書き直しの議題** |

撤退＝売却ではない。$158 は「行動の引き金」ではなく「信念の反証」なので、
政策台帳ではなく thesis の `falsification` に入る（両者は評価器を共有するが用途が違う）。

## 使い方

    python scripts/seed_policies.py                # 差分プレビュー（何も書かない）
    python scripts/seed_policies.py --apply        # 登録する
    python scripts/seed_policies.py --apply --force  # 既存の同等政策があっても再登録

`--apply` しない限り 1バイトも書かない。政策は実弾の判断基準なので、
プレビューを見ずに入る経路を作らない。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from src.core.policy.ledger import (  # noqa: E402
    build_policy,
    list_policies,
    save_policy,
)

#: 保有方針の再審査日。**無期限政策は登録できない**（硬直化の抑制策）ので、
#: 「1億到達まで」「永久保有」のような期限の無い方針には再審査日を置く。
#: 期限切れ＝再審査の強制であり、方針の否定ではない。
REVIEW_1Y = "2027-08-05"
REVIEW_Q = "2026-11-05"

#: FANG+ はティッカーが無い。模型側のキーは名前なので、政策も同じ名前で持つ
#: （`_load_intent` が名前でも引けるようにしてある）。
FANG_PLUS = "iFreeNEXT FANG+インデックス"


def _p(symbol: str, response: str, triggers: list, expires: str,
       rationale: str, intent: str, tag: str) -> dict:
    return {"symbol": symbol, "response": response, "triggers": triggers,
            "expires_on": expires, "rationale": rationale, "intent": intent,
            "tag": tag}


def policy_specs() -> list[dict]:
    """登録する政策の定義。

    **すべてユーザーが既に運用している方針の書き起こしであり、新しい判断ではない。**
    機械が方針を発明してはいけない（§16-6 提案と登録を分ける）。
    """
    specs: list[dict] = []

    # --- 3xスリーブ: 損切りなし・下落で積み増し・評価額1億で全利確 -------------
    for sym in ("SOXL", "TECL", "TQQQ"):
        specs.append(_p(
            sym,
            "評価額が1億円に到達するまで売却しない。株数が唯一のKPI。到達時に全利確しVT/VTI等へ移行",
            [{"metric": "position_value_jpy", "op": ">=", "value": 100_000_000}],
            REVIEW_1Y,
            "株式介護手法の中核。3xスリーブは損切りなし・下落で株数を積み増す。"
            "価格の上下は判断材料にならず、見るのは株数と評価額の到達だけ。",
            "deliberate_inaction",
            f"{sym}:hold_to_100m",
        ))
        specs.append(_p(
            sym,
            "第1トランシェ投入（弾の1/3）。優先順は TECL > TQQQ > SOXL",
            [{"metric": "drawdown_pct", "op": "<=", "value": -50}],
            REVIEW_Q,
            "ATH比▲50%（Lv2）を機械条件とする。実行には補助シグナル1つ以上・"
            "除外条件①②非成立・弾>0 が必要で、これらは機械判定できないため"
            "トリガー成立は『投入の起動』であって『投入の自動実行』ではない。"
            "現時点で弾（USD現金）はほぼ枯渇しており、成立しても投入できない可能性がある。",
            "awaiting_trigger",
            f"{sym}:tranche1",
        ))

    # --- MDT: 二段の出口 ------------------------------------------------------
    specs.append(_p(
        "MDT", "売却検討を起動（弾再生の第一候補）",
        [{"metric": "price", "op": ">=", "value": 86.00}],
        "2026-08-18",
        "第一出口。弾（USD現金）が枯渇しているため、MDT は再生源として最優先。"
        "失効日は決算日で、決算を跨いだら必ず再審査する。",
        "conditional_commit", "MDT:exit1",
    ))
    specs.append(_p(
        "MDT", "第二出口。残数量の売却を検討する",
        [{"metric": "price", "op": ">=", "value": 100.50}],
        "2026-08-18",
        "第二出口。第一出口を通過した場合の続き。",
        "conditional_commit", "MDT:exit2",
    ))

    # --- QCOM: 三層（出口 / リスクライン / 決算を跨ぐ） -----------------------
    specs.append(_p(
        "QCOM", "段階売却を起動する（一括ではなく分割）",
        [{"metric": "price", "op": ">=", "value": 211.00}],
        "2026-10-31",
        "出口。$211 は実運用のターゲット。到達時に段階売却を起動する。"
        "失効日は Q4決算（10月末）で、決算後は必ず再審査する。",
        "conditional_commit", "QCOM:exit",
    ))
    specs.append(_p(
        "QCOM",
        "弾再生順序を「MDT優先」に固定する。**損切りはしない**",
        [{"metric": "price", "op": "<=", "value": 172.12}],
        "2026-10-31",
        "リスクライン。ここで行うのは売却ではなく『弾を作る順番の変更』のみ。"
        "STRUCTURE.md §17.2 の $158 とは層が違う（あちらは反証条件で、"
        "テーゼが間違っていた証拠として thesis 側に登録する）。",
        "awaiting_trigger", "QCOM:risk_line",
    ))
    specs.append(_p(
        "QCOM", "決算をまたぐ。事前確定済みにつき決算前に再審理しない",
        [],
        "2026-10-31",
        "決算前の再審理は、ストレス下の即興判断を招く典型的な経路。"
        "またぐことを事前に確定させ、決算後に政策の失効とともに再審査する。",
        "deliberate_inaction", "QCOM:hold_through_earnings",
    ))

    # --- 味の素: 口座で扱いが違う --------------------------------------------
    specs.append(_p(
        "2802.T",
        "税務通算のため必要株数まで売却してよい（特定口座分のみ）",
        [{"metric": "price", "op": ">=", "value": 5068}],
        "2026-12-31",
        "特定口座分は通算の原資として使える。¥5,068 は通算に足る水準の下限。"
        "**節税を理由にした売却ではない**（§16-4 税は判断を却下する側にのみ使う）。"
        "通算相手が実際に存在するときだけ起動する。NISA分（39株）には適用しない。",
        "conditional_commit", "2802.T:tax_offset",
    ))
    specs.append(_p(
        "2802.T",
        "NISA成長枠の39株は永久保有。売却禁止",
        [],
        REVIEW_1Y,
        "NISA内の損失は損益通算できず、枠は売っても復活しない（当年分）。"
        "税務上の理由で動かす対象にならないので、意図的不作為として確定させる。"
        "特定口座分（400株）の政策とは別レイヤーで、混同しないこと。",
        "deliberate_inaction", "2802.T:nisa_never_sell",
    ))

    # --- ニトリ / トーメン: 決算で判定する -----------------------------------
    specs.append(_p(
        "9843.T",
        "8月末Q1決算の内容で、損切り実行か温存かを判定する",
        [{"metric": "operating_margin", "op": "<=", "value": 10.0}],
        "2026-09-30",
        "含み損 -31%。価格ではなく**採算**で判定する。営業利益率10%割れを"
        "テーゼ崩壊の目安とし、決算の実数で判定する。"
        "価格を条件にすると下落そのものが判断材料になり、循環する。",
        "awaiting_trigger", "9843.T:q1_verdict",
    ))
    specs.append(_p(
        "2737.T",
        "10月末Q2決算の内容で売却判断。**為替とは紐づけない**",
        [{"metric": "per", "op": ">=", "value": 18.0}],
        "2026-10-31",
        "半導体商社。過熱・サイクルピーク接近で利確する。"
        "為替を条件に混ぜると、為替の変動が銘柄の判断に流れ込んで"
        "『地域は分散、為替は集中』の二重ロングを見えなくする。",
        "awaiting_trigger", "2737.T:q2_verdict",
    ))

    # --- FANG+: 非売却の保険コア ---------------------------------------------
    specs.append(_p(
        FANG_PLUS,
        "月5万円のDCAを無条件で継続する。売却禁止",
        [],
        REVIEW_1Y,
        "壊れない1倍の保険コア。3xスリーブが逆回転したときに残る部分であり、"
        "**3xの損失を理由にここを売ると保険が消える**。非課税複利を止めない。",
        "deliberate_inaction", "FANG+:dca_forever",
    ))

    return specs


#: thesis 側に入れる反証条件（政策ではない）。
#: 成立しても売り推奨は作らない。テーゼ書き直しの議題を作る。
FALSIFICATIONS: list[dict] = [
    {
        "symbol": "QCOM",
        "condition": "price <= 158",
        "why": (
            "本文に「撤退ライン約$158(取得-15%)」と書かれていたが反証条件として"
            "登録されておらず、7/29 終値 $155.68 で**既に割れていたのにシステムは"
            "検出していなかった**。$172.12（リスクライン・政策）とは層が違い、"
            "こちらは『テーゼが間違っていた』の判定であって売却の引き金ではない。"
        ),
    },
]


# ---------------------------------------------------------------------------
# 差分計算
# ---------------------------------------------------------------------------


def _same_policy(existing: dict, spec: dict) -> bool:
    """同等の政策が既にあるか。**銘柄とトリガー集合だけで見る。**

    intent は比較に入れない。同じ条件の政策を intent 違いで二重登録すると、
    同じ状態に対して2つの応答が並び、`evaluator` がどちらを返すか分からなくなる。
    「同じ条件・違う意図」は重複ではなく**衝突**であり、黙って足すべきものではない。
    """
    if existing.get("symbol") != spec["symbol"]:
        return False
    a = {(t.get("metric"), t.get("op"), float(t.get("value")))
         for t in existing.get("triggers") or []}
    b = {(t["metric"], t["op"], float(t["value"])) for t in spec["triggers"]}
    return a == b


def plan(specs: list[dict], base_dir: Optional[str] = None) -> dict:
    """登録するもの / 既にあるものに分ける。"""
    kwargs: dict[str, Any] = {"active_only": True}
    if base_dir:
        kwargs["base_dir"] = base_dir
    existing = list_policies(**kwargs)

    to_add: list[dict] = []
    skipped: list[dict] = []
    for spec in specs:
        dup = next((e for e in existing if _same_policy(e, spec)), None)
        if dup is not None:
            skipped.append({**spec, "existing_id": dup["id"],
                            "existing_intent": dup.get("intent"),
                            "existing_response": dup.get("response")})
        else:
            to_add.append(spec)
    return {"existing": existing, "to_add": to_add, "skipped": skipped}


def _num(value: float) -> str:
    """1e+08 のような指数表記を出さない。金額の桁は目で確認するもの。"""
    v = float(value)
    return f"{v:,.0f}" if abs(v) >= 1000 and v == int(v) else f"{v:g}"


def preview(result: dict) -> None:
    print("## 登録する政策")
    print()
    if not result["to_add"]:
        print("（新規なし）")
    for spec in result["to_add"]:
        conds = ", ".join(f"{t['metric']} {t['op']} {_num(t['value'])}"
                          for t in spec["triggers"]) or "条件なし（意図的不作為）"
        print(f"- **{spec['symbol']}** [{spec['intent']}] {spec['tag']}")
        print(f"    応答  : {spec['response']}")
        print(f"    条件  : {conds}")
        print(f"    失効  : {spec['expires_on']}")
        print(f"    理由  : {spec['rationale']}")
    print()

    if result["skipped"]:
        print("## 既に同じ条件の政策があるため飛ばすもの")
        print()
        for spec in result["skipped"]:
            print(f"- {spec['symbol']} {spec['tag']} → {spec['existing_id']}")
            print(f"    既存: [{spec.get('existing_intent')}] {spec.get('existing_response')}")
            if spec.get("existing_intent") != spec["intent"]:
                print(f"    ⚠️ 意図が違います（今回の案は {spec['intent']}）。"
                      "同じ条件に2つの応答を並べないため登録しません。"
                      "変えたい場合は manage_policy.py で既存を改訂してください。")
        print()

    print("## thesis 側に入れる反証条件（政策ではない）")
    print()
    for f in FALSIFICATIONS:
        print(f"- **{f['symbol']}**: `{f['condition']}`")
        print(f"    {f['why']}")
    print()
    print("⚠️ 反証条件の成立は**売り推奨ではありません**。テーゼを書き直すか退出するかの議題です。")


# ---------------------------------------------------------------------------
# 適用
# ---------------------------------------------------------------------------


def apply_policies(specs: list[dict], base_dir: Optional[str] = None) -> list[dict]:
    saved: list[dict] = []
    for spec in specs:
        policy = build_policy(
            symbol=spec["symbol"],
            response=spec["response"],
            triggers=spec["triggers"],
            expires_on=spec["expires_on"],
            rationale=spec["rationale"],
            intent=spec["intent"],
        )
        policy["seed_tag"] = spec["tag"]
        policy["seeded_by"] = "scripts/seed_policies.py"
        path = save_policy(policy, **({"base_dir": base_dir} if base_dir else {}))
        saved.append({"id": policy["id"], "symbol": policy["symbol"],
                      "tag": spec["tag"], "path": str(path)})
    return saved


def apply_falsifications(notes_dir: Optional[str] = None) -> list[dict]:
    """既存 thesis に反証条件を書き足す。**本文は書き換えない。**

    thesis 本文はユーザーの言明であり、機械が言い換えると
    `provenance` 上の user_statement が self_inference に汚染される。
    """
    from src.data.note_manager import load_notes

    kwargs: dict[str, Any] = {}
    if notes_dir:
        kwargs["base_dir"] = notes_dir

    out: list[dict] = []
    for item in FALSIFICATIONS:
        notes = load_notes(symbol=item["symbol"], note_type="thesis", **kwargs) or []
        if not notes:
            out.append({"symbol": item["symbol"], "status": "no_thesis",
                        "message": "thesis が無いため反証条件を付けられません。"})
            continue
        note = notes[0]
        if note.get("falsification"):
            out.append({"symbol": item["symbol"], "status": "already",
                        "existing": note["falsification"]})
            continue

        raw_path = str(note.get("_path") or "").strip()
        # `Path("")` は `Path(".")` になり、ディレクトリを開こうとして落ちる。
        path: Optional[Path] = Path(raw_path) if raw_path else None
        if path is None or not path.is_file():
            path = _find_note_file(note, notes_dir)
        if path is None or not path.is_file():
            out.append({"symbol": item["symbol"], "status": "file_not_found"})
            continue

        data = json.loads(path.read_text(encoding="utf-8"))
        target = _locate_note(data, note)
        if target is None:
            out.append({"symbol": item["symbol"], "status": "not_in_file"})
            continue
        target["falsification"] = item["condition"]
        target["falsification_note"] = item["why"]
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        out.append({"symbol": item["symbol"], "status": "added",
                    "condition": item["condition"], "path": str(path)})
    return out


def _find_note_file(note: dict, notes_dir: Optional[str]) -> Optional[Path]:
    root = Path(notes_dir or "data/notes")
    if not root.exists():
        return None
    note_id = note.get("id")
    for path in root.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if _locate_note(data, {"id": note_id}) is not None:
            return path
    return None


def _locate_note(data: Any, note: dict) -> Optional[dict]:
    """ノートファイルの構造（単体 / notes 配列）差を吸収する。"""
    note_id = note.get("id")
    if isinstance(data, dict):
        if data.get("id") == note_id:
            return data
        for row in data.get("notes") or []:
            if isinstance(row, dict) and row.get("id") == note_id:
                return row
        if note_id is None and "content" in data:
            return data
    if isinstance(data, list):
        for row in data:
            if isinstance(row, dict) and row.get("id") == note_id:
                return row
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="孤児ポジションに政策を入れる (改善3)")
    ap.add_argument("--apply", action="store_true", help="実際に登録する（既定はプレビュー）")
    ap.add_argument("--force", action="store_true", help="同等の政策があっても登録する")
    ap.add_argument("--base-dir", help="政策の保存先（テスト用）")
    ap.add_argument("--notes-dir", help="ノートの保存先（テスト用）")
    ap.add_argument("--skip-falsification", action="store_true")
    args = ap.parse_args()

    specs = policy_specs()
    result = plan(specs, args.base_dir)
    if args.force:
        result["to_add"] = specs
        result["skipped"] = []

    preview(result)

    if not args.apply:
        print()
        print("——— これはプレビューです。1バイトも書いていません。———")
        print("登録するには: python scripts/seed_policies.py --apply")
        return 0

    saved = apply_policies(result["to_add"], args.base_dir)
    print()
    print(f"✅ 政策 {len(saved)}件を登録しました（既存のため飛ばした: {len(result['skipped'])}件）")
    for row in saved:
        print(f"   {row['symbol']:26} {row['tag']:28} {row['id']}")

    if not args.skip_falsification:
        fals = apply_falsifications(args.notes_dir)
        print()
        print("反証条件（thesis 側）:")
        for row in fals:
            print(f"   {row['symbol']:8} {row['status']:16} {row.get('condition', '')}")

    print()
    print("次に確認すること:")
    print("   python .claude/skills/stock-portfolio/scripts/run_portfolio.py reconcile")
    print("   → 孤児ポジションが減っているか（減らなければ symbol の表記揺れを疑う）")
    print(f"   政策は {date.today().isoformat()} 時点のもの。失効日ごとに再審査が強制されます。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
