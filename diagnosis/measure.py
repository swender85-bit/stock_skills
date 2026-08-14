"""Phase B/D 共通の測定器。

**着手後にこのファイルを変更してはならない。** 成功基準を後から動かさないため。
パック(JSON)とレポート(Markdown)から、診断書 Phase B の指標を機械的に出す。

使い方:
    python diagnosis/measure.py output/briefing/PF_20260808.json output/週次PF分析_20260808.md
    python diagnosis/measure.py <pack> --json
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _analyses(pack: dict) -> list:
    """パック内の保有分析リスト。構造が変わっても拾えるよう複数経路を見る。"""
    for key in ("holdings",):
        v = pack.get(key)
        if isinstance(v, list) and v:
            return v
    pf = pack.get("portfolio") or {}
    for key in ("analyses", "positions"):
        v = pf.get(key)
        if isinstance(v, list) and v:
            return v
    return []


def _has_price(a: dict) -> bool:
    for k in ("price", "current_price", "last_price"):
        if a.get(k) is not None:
            return True
    return a.get("value_jpy") is not None


def _has_technicals(a: dict) -> bool:
    t = a.get("technicals") or a.get("technical") or {}
    if not isinstance(t, dict):
        return False
    return any(t.get(k) is not None for k in ("rsi", "rsi14", "sma50", "sma200", "volatility_pct"))


def measure_pack(pack: dict) -> dict:
    rows = _analyses(pack)
    n = len(rows)
    priced = sum(1 for a in rows if _has_price(a))
    tech = sum(1 for a in rows if _has_technicals(a))

    fals = pack.get("falsification") or {}
    thesis_total = fals.get("total")
    checked = fals.get("checked")
    if thesis_total is None:
        items = fals.get("items") or fals.get("theses") or []
        thesis_total = len(items) if isinstance(items, list) else 0
        checked = sum(1 for i in items
                      if isinstance(i, dict) and i.get("status") not in (None, "unknown", "未点検"))

    pf = pack.get("portfolio") or {}
    # 分子と分母が同じ集合から来ているか（H3）。
    # 分子(pl_jpy)は価格が要る。分母(cost_jpy)は取得単価だけで出るので、
    # 価格が欠けた銘柄も分母には残る。よって
    #   「損益率が出ている」かつ「価格が全数揃っていない」= 混合分母。
    pl_rows = [a for a in rows if a.get("pl_jpy") is not None]
    cost_rows = [a for a in rows if a.get("cost_price") is not None]
    mixed = pf.get("pl_pct") is not None and priced < len(cost_rows)

    return {
        "positions_total": n,
        "positions_priced": priced,
        "positions_technicals": tech,
        "thesis_total": thesis_total or 0,
        "thesis_checked": checked or 0,
        "pl_numerator_rows": len(pl_rows),
        "pl_denominator_rows": len(cost_rows),
        "pl_pct_mixed_denominator": mixed,
        "total_jpy": pf.get("total_jpy"),
        "invested_jpy": pf.get("invested_jpy"),
        "pl_pct": pf.get("pl_pct"),
        "coverage_disclosed": bool((pf.get("coverage") or {}) if isinstance(pf.get("coverage"), dict) else False),
    }


UNAVAILABLE = re.compile(r"取得できず|取得できなかった|取得不可|未取得|取れていません|取得に失敗")
MEASURED = re.compile(r"実測")


def measure_report(text: str) -> dict:
    lines = text.splitlines()
    body = [l for l in lines if l.strip()]
    return {
        "report_lines": len(lines),
        "report_lines_nonblank": len(body),
        "unavailable_mentions": len(UNAVAILABLE.findall(text)),
        "measured_mentions": len(MEASURED.findall(text)),
        "measured_ratio_per_100_lines": round(
            len(MEASURED.findall(text)) / max(len(body), 1) * 100, 1),
        "sections": len(re.findall(r"^##+ ", text, flags=re.M)),
    }


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    as_json = "--json" in argv
    out: dict = {}
    for path in args:
        p = Path(path)
        if not p.exists():
            print(f"missing: {p}", file=sys.stderr)
            return 1
        if p.suffix == ".json":
            out["pack"] = measure_pack(json.loads(p.read_text(encoding="utf-8")))
            out["pack_path"] = str(p)
        else:
            out["report"] = measure_report(p.read_text(encoding="utf-8"))
            out["report_path"] = str(p)
    if as_json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        for section, data in out.items():
            if isinstance(data, dict):
                print(f"[{section}]")
                for k, v in data.items():
                    print(f"  {k}: {v}")
            else:
                print(f"{section}: {data}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
