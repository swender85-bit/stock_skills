#!/usr/bin/env python3
"""synthesis層の eval harness (改善1) + モデル配分スイープ (改善2).

## なぜあるか

    Python層  : テスト 4,584件
    synthesis層: テスト 0件

`.claude/prompts/weekly_deep.md` を触っても、節の文章が §16 の8原則を破り始めたことに
気づく手段が無かった。ここが空いている限り、改善2（モデル配分の実測）も
改善7（カオステスト）も**評価軸が無いので実行できない**。

## 使い方

    # 1節 × 1fixture（`claude -p` を1回呼ぶ）
    python scripts/eval_synthesis.py --section 1 --fixture pack_circular

    # 全節 × 全fixture（重い。週1回）
    python scripts/eval_synthesis.py --all

    # プロンプト編集後の煙試験（節1・節6 × 1fixture）
    python scripts/eval_synthesis.py --changed

    # モデル配分スイープ（改善2 / rightmodel）
    python scripts/eval_synthesis.py --sweep --models haiku,sonnet,opus \
        --sections 0,1,2,3,4 --fixtures pack_quiet_week,pack_circular

    # API を叩かず配線だけ確認
    python scripts/eval_synthesis.py --all --dry-run

## 出力

    data/synthesis_evals/YYYYMMDD.json        … 実行記録（節・モデル・通過率・秒・コスト）
    data/synthesis_evals/sweep_YYYYMMDD.json  … スイープ結果 + 推奨モデル表
    data/synthesis_evals/texts/<day>/...md    … 生成された節の本文（目視確認用）

## 終了コード

    0 … 全検査 pass（または dry-run）
    1 … FAIL あり
    2 … 実行できなかった（claude CLI 不在・使用量上限など）。**これは「合格」ではない**
"""

from __future__ import annotations

import argparse
import json
import os
import sys
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

from scripts.weekly_deep_driver import (  # noqa: E402
    SPEC_PATH,
    build_prompt,
    build_sections,
    claude_bin,
    run_claude,
    slice_pack,
)
from tests.synthesis import assertions as A  # noqa: E402

FIXTURES_DIR = REPO / "tests" / "synthesis" / "fixtures"
EVALS_DIR = REPO / "data" / "synthesis_evals"

EXIT_OK, EXIT_FAILED, EXIT_UNRUNNABLE = 0, 1, 2

#: 節番号 → 節ID（build_sections が付ける id）。
#: 5（機会）は保有数だけ増えるので holding_* と heat の両方が該当する。
SECTION_NUMBERS: dict[int, tuple[str, ...]] = {
    0: ("verdict",),
    1: ("reconcile",),
    2: ("belief",),
    3: ("forward",),
    4: ("constraints",),
    5: ("opportunity", "heat"),
    6: ("decide",),
    7: ("audit",),
    8: ("limits",),
}

#: プロンプト編集直後の煙試験。**通過率100%が必須の2節**だけを見る。
SMOKE_SECTIONS = (1, 6)
SMOKE_FIXTURES = ("pack_circular",)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# fixture / 節の解決
# ---------------------------------------------------------------------------


def list_fixtures() -> list[str]:
    return sorted(p.stem for p in FIXTURES_DIR.glob("pack_*.json"))


def load_fixture(name: str) -> dict:
    path = FIXTURES_DIR / f"{name}.json"
    if not path.exists():
        raise SystemExit(f"fixture が見つかりません: {name}（利用可能: {', '.join(list_fixtures())}）")
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_sections(
    pack: dict, wanted: Optional[list[str]], limit_holdings: int = 1
) -> list[dict]:
    """`--section` の指定を実際の節オブジェクトへ解決する。

    `holding_*` は保有数だけ増えるので既定で先頭 `limit_holdings` 件に絞る。
    **全部回すと fixture の保有数だけ API コストが線形に増える。**
    """
    sections = build_sections(pack)
    if not wanted:
        chosen = sections
    else:
        ids: set[str] = set()
        prefixes: list[str] = []
        for token in wanted:
            token = token.strip()
            if not token:
                continue
            if token.isdigit():
                names = SECTION_NUMBERS.get(int(token), ())
                ids.update(names)
                if int(token) == 5:
                    prefixes.append("holding_")
            else:
                ids.add(token)
                if token in ("holding", "holdings"):
                    prefixes.append("holding_")
        chosen = [
            s for s in sections
            if s["id"] in ids or any(s["id"].startswith(p) for p in prefixes)
        ]

    out: list[dict] = []
    holdings_taken = 0
    for s in sorted(chosen, key=lambda x: x["order"]):
        if s.get("kind") == "holding":
            if holdings_taken >= limit_holdings:
                continue
            holdings_taken += 1
        out.append(s)
    return out


# ---------------------------------------------------------------------------
# 1回の評価
# ---------------------------------------------------------------------------


def evaluate_once(
    pack: dict,
    pack_name: str,
    section: dict,
    spec: str,
    model: str,
    timeout: int,
    dry_run: bool,
    text_dir: Path,
) -> dict:
    """1節を1モデルで書かせ、assertions を全件かける。

    **失敗しても止めない。** 8件中いくつ通ったかが知りたいので、
    最初の FAIL で例外を投げてはいけない。
    """
    material = slice_pack(pack, section)
    prompt = build_prompt(spec, section, material, body_so_far="")
    record: dict[str, Any] = {
        "fixture": pack_name,
        "section_id": section["id"],
        "section_kind": section.get("kind"),
        "order": section["order"],
        "model": model,
        "prompt_chars": len(prompt),
    }

    if dry_run:
        would = [name for name, (_fn, kinds) in A.CHECKS.items()
                 if kinds is None or section.get("kind") in kinds]
        record.update({"status": "dry_run", "would_check": would})
        return record

    res = run_claude(prompt, model, timeout)
    record["duration_sec"] = res.get("duration_sec")
    record["cost_usd"] = res.get("cost_usd")
    record["usage"] = res.get("usage")

    if not res.get("ok"):
        # **実行できなかったことを「合格」と数えない**（§16-1 を harness 自身に適用）
        record.update({
            "status": "unrunnable",
            "interrupted": bool(res.get("interrupted")),
            "error": res.get("error"),
        })
        return record

    text = res["text"]
    text_dir.mkdir(parents=True, exist_ok=True)
    out_path = text_dir / f"{pack_name}__{section['id']}__{model}.md"
    out_path.write_text(text + "\n", encoding="utf-8")

    results = A.run_checks(text, pack, section_kind=section.get("kind") or "report")
    summary = A.summarize(results)
    record.update({
        "status": "ok",
        "text_path": str(out_path.relative_to(REPO)),
        "lines": len(A.content_lines(text)),
        "summary": summary,
        "results": results,
    })
    return record


# ---------------------------------------------------------------------------
# 集計・表示
# ---------------------------------------------------------------------------


def print_run_table(records: list[dict]) -> None:
    log("")
    log(f"{'fixture':22} {'節':22} {'model':8} {'判定':14} {'秒':>6} {'USD':>8}")
    log("-" * 88)
    for r in records:
        if r.get("status") == "dry_run":
            verdict = f"dry-run({len(r.get('would_check') or [])}検査)"
        elif r.get("status") == "unrunnable":
            verdict = "実行不能"
        else:
            summary = r.get("summary") or {}
            verdict = summary.get("label", "?")
        log(f"{r['fixture']:22} {r['section_id']:22} {r['model']:8} {verdict:14} "
            f"{(r.get('duration_sec') or 0):6.1f} {(r.get('cost_usd') or 0):8.4f}")


def print_failures(records: list[dict]) -> None:
    for r in records:
        for res in r.get("results") or []:
            if res["status"] != A.FAIL:
                continue
            log("")
            log(f"❌ [{r['fixture']} / {r['section_id']} / {r['model']}] {res['name']}")
            log(f"   原則: {res['principle']}")
            log(f"   {res['message']}")
            for ev in res["evidence"]:
                log(f"     - {ev}")


def build_sweep_table(records: list[dict], models: list[str]) -> list[dict]:
    """節 × モデル の表を作り、規約に沿って推奨を出す。

    規約:
      1. 通過率が同じなら安い方（＝models の並び順で先＝安い想定）を選ぶ
      2. critical な節（照合・事前決定）は通過率100%を必須とする
      3. 判定できた検査が0件のセルからは推奨を出さない（**「判定不能」であって「合格」ではない**）
    """
    from scripts.weekly_deep_driver import CRITICAL_SECTIONS

    by_section: dict[str, dict[str, list[dict]]] = {}
    for r in records:
        by_section.setdefault(r["section_id"], {}).setdefault(r["model"], []).append(r)

    rows: list[dict] = []
    for section_id, per_model in sorted(by_section.items(),
                                        key=lambda kv: min(x["order"] for m in kv[1].values() for x in m)):
        cells: dict[str, dict] = {}
        for model in models:
            runs = per_model.get(model) or []
            usable = [x for x in runs if x.get("status") == "ok"]
            if not usable:
                cells[model] = {"available": False,
                                "reason": "実行できませんでした（合格ではありません）"}
                continue
            passed = sum(x["summary"]["passed"] for x in usable)
            judged = sum(x["summary"]["judged"] for x in usable)
            cells[model] = {
                "available": True,
                "passed": passed,
                "judged": judged,
                "pass_rate": (passed / judged) if judged else None,
                "seconds": round(sum((x.get("duration_sec") or 0) for x in usable) / len(usable), 1),
                "cost_usd": round(sum((x.get("cost_usd") or 0) for x in usable), 4),
                "runs": len(usable),
            }

        critical = section_id in CRITICAL_SECTIONS
        recommended, reason = _recommend(cells, models, critical)
        rows.append({
            "section_id": section_id,
            "critical": critical,
            "cells": cells,
            "recommended": recommended,
            "reason": reason,
        })
    return rows


def _recommend(cells: dict, models: list[str], critical: bool) -> tuple[Optional[str], str]:
    scored = [(m, c) for m, c in cells.items()
              if c.get("available") and c.get("pass_rate") is not None]
    if not scored:
        return None, "判定できた検査が無いため推奨を出しません（実行不能は合格ではありません）"

    best_rate = max(c["pass_rate"] for _m, c in scored)
    if critical and best_rate < 1.0:
        return None, ("通過率100%を必須とする節ですが、どのモデルも100%に届いていません。"
                      "モデルではなくプロンプト側を直す必要があります。")

    # models の並び順を「安い順」とみなす（haiku,sonnet,opus）
    order = {m: i for i, m in enumerate(models)}
    tied = [m for m, c in scored if c["pass_rate"] >= best_rate]
    cheapest = min(tied, key=lambda m: order.get(m, 99))
    label = "通過率100%を維持できる最安" if critical else "通過率が同じなら安い方"
    return cheapest, f"{label}（通過率 {best_rate:.0%}、同率 {len(tied)}モデル）"


def print_sweep_table(rows: list[dict], models: list[str]) -> None:
    log("")
    log("## モデル配分スイープ結果")
    log("")
    header = f"| {'節':20} | " + " | ".join(f"{m:16}" for m in models) + " | 推奨 |"
    log(header)
    log("|" + "-" * 22 + "|" + "|".join("-" * 18 for _ in models) + "|------|")
    for row in rows:
        cells = []
        for m in models:
            c = row["cells"].get(m) or {}
            if not c.get("available"):
                cells.append(f"{'実行不能':16}")
            else:
                cells.append(f"{c['passed']}/{c['judged']} · {c['seconds']:.0f}s".ljust(16))
        mark = row["recommended"] or "—"
        if row["critical"]:
            mark += " ★必須100%"
        log(f"| {row['section_id']:20} | " + " | ".join(cells) + f" | {mark} |")
    log("")
    for row in rows:
        log(f"- {row['section_id']}: {row['reason']}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="synthesis層 eval harness / モデル配分スイープ")
    ap.add_argument("--section", action="append", default=[],
                    help="節番号(0-8) または節ID。複数指定可・カンマ区切り可")
    ap.add_argument("--sections", help="節のカンマ区切り（--section の別名）")
    ap.add_argument("--fixture", action="append", default=[], help="fixture 名（複数可）")
    ap.add_argument("--fixtures", help="fixture のカンマ区切り")
    ap.add_argument("--all", action="store_true", help="全節 × 全fixture")
    ap.add_argument("--changed", action="store_true",
                    help="プロンプト編集後の煙試験（節1・節6 × pack_circular）")
    ap.add_argument("--sweep", action="store_true", help="モデル配分スイープ（改善2）")
    ap.add_argument("--models", default="", help="スイープするモデル（安い順・カンマ区切り）")
    ap.add_argument("--model", default=os.environ.get("SYNTHESIS_EVAL_MODEL", "sonnet"),
                    help="単発評価で使うモデル")
    ap.add_argument("--limit-holdings", type=int, default=1,
                    help="節5で評価する銘柄節の数（既定1。増やすとコストが線形に増える）")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--dry-run", action="store_true", help="API を叩かず配線だけ確認")
    ap.add_argument("--out-dir", default=str(EVALS_DIR))
    args = ap.parse_args()

    section_tokens: list[str] = []
    for token in args.section:
        section_tokens += [t for t in str(token).split(",") if t.strip()]
    if args.sections:
        section_tokens += [t for t in args.sections.split(",") if t.strip()]

    fixture_names: list[str] = []
    for token in args.fixture:
        fixture_names += [t for t in str(token).split(",") if t.strip()]
    if args.fixtures:
        fixture_names += [t for t in args.fixtures.split(",") if t.strip()]

    if args.changed:
        section_tokens = section_tokens or [str(n) for n in SMOKE_SECTIONS]
        fixture_names = fixture_names or list(SMOKE_FIXTURES)
    if args.all:
        section_tokens = []
        fixture_names = list_fixtures()
    if not fixture_names:
        fixture_names = ["pack_busy_week"]

    models = [m.strip() for m in args.models.split(",") if m.strip()] if args.models else []
    if args.sweep and not models:
        models = ["haiku", "sonnet", "opus"]
    if not models:
        models = [args.model]

    if not args.dry_run and not claude_bin():
        log("❌ claude CLI が見つかりません（CLAUDE_BIN で指定可）。")
        log("   これは『検査に合格した』ではなく『評価できなかった』です。")
        return EXIT_UNRUNNABLE

    spec = SPEC_PATH.read_text(encoding="utf-8") if SPEC_PATH.exists() else ""
    if not spec:
        log(f"❌ 執筆仕様が見つかりません: {SPEC_PATH}")
        return EXIT_UNRUNNABLE

    day = date.today().strftime("%Y%m%d")
    out_dir = Path(args.out_dir)
    text_dir = out_dir / "texts" / day

    records: list[dict] = []
    planned = 0
    for name in fixture_names:
        pack = load_fixture(name)
        for section in resolve_sections(pack, section_tokens, args.limit_holdings):
            planned += len(models)

    log(f"評価計画: fixture {len(fixture_names)}種 × モデル {len(models)}種 = {planned} 回の呼び出し")
    if not args.dry_run and planned > 12:
        log("⚠️ 呼び出しが多いです。API コストがかかります（--dry-run で配線だけ確認できます）。")

    for name in fixture_names:
        pack = load_fixture(name)
        for section in resolve_sections(pack, section_tokens, args.limit_holdings):
            for model in models:
                log(f"▶ {name} / {section['id']} / {model}")
                records.append(evaluate_once(
                    pack, name, section, spec, model, args.timeout, args.dry_run, text_dir))

    print_run_table(records)
    print_failures(records)

    payload: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "sweep" if args.sweep else "eval",
        "models": models,
        "fixtures": fixture_names,
        "sections": section_tokens or "all",
        "dry_run": args.dry_run,
        "runs": records,
    }

    if args.sweep:
        rows = build_sweep_table(records, models)
        payload["sweep"] = rows
        print_sweep_table(rows, models)

    totals = {
        "runs": len(records),
        "ok": sum(1 for r in records if r.get("status") == "ok"),
        "unrunnable": sum(1 for r in records if r.get("status") == "unrunnable"),
        "failed_checks": sum(len([x for x in (r.get("results") or [])
                                  if x["status"] == A.FAIL]) for r in records),
        "cost_usd": round(sum((r.get("cost_usd") or 0) for r in records), 4),
        "seconds": round(sum((r.get("duration_sec") or 0) for r in records), 1),
    }
    payload["totals"] = totals

    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{'sweep_' if args.sweep else ''}{day}.json"
    (out_dir / fname).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log("")
    log(f"記録: {out_dir / fname}")
    log(f"合計: 実行 {totals['runs']}件 / 成功 {totals['ok']}件 / 実行不能 {totals['unrunnable']}件 "
        f"/ 検査FAIL {totals['failed_checks']}件 / ${totals['cost_usd']} / {totals['seconds']}秒")

    if args.dry_run:
        return EXIT_OK
    if totals["failed_checks"]:
        return EXIT_FAILED
    if totals["ok"] == 0:
        log("⚠️ 1件も評価できませんでした。これは合格ではありません。")
        return EXIT_UNRUNNABLE
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
