"""Phase D — 8/8 の失敗パックを、修理後のコードに通し直す。

**レポート生成器（テンプレート・章立て・文体）は1文字も変えていない。**
変えたのは入力データの表現（観測の第一級化）だけである。
よって前後の差は、修理の効果として読める。

    python diagnosis/replay.py                # 前後比較を出す
    python diagnosis/replay.py --show-report  # 打ち切りレポート本文を見る
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.core import observation  # noqa: E402
from src.core.research.briefing_pack import _data_quality, _portfolio_summary  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "weekly_deep_driver", REPO / "scripts" / "weekly_deep_driver.py")
driver = importlib.util.module_from_spec(_spec)
sys.modules["weekly_deep_driver"] = driver
_spec.loader.exec_module(driver)

PACK_PATH = REPO / "output" / "briefing" / "PF_20260808.json"
HEALTHY_PATH = REPO / "output" / "briefing" / "PF_20260809.json"


def _divisors_from_config() -> dict:
    """`config/weekly_holdings.yaml` から unit_divisor を引く（パックには残らない）。"""
    try:
        import yaml

        cfg = yaml.safe_load(
            (REPO / "config" / "weekly_holdings.yaml").read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    out = {}
    for h in cfg.get("holdings") or []:
        for key in (h.get("quote_symbol"), h.get("symbol"), h.get("name")):
            if key:
                out[str(key)] = h.get("unit_divisor") or 1
    return out


def to_analyses(pack: dict) -> list:
    """パックの holdings を、weekly.build_report_data が作る analyses 相当に戻す。

    パックは holdings を痩せさせて保存しているので、cost_jpy を取得単価から
    復元する。**復元できるということ自体が H3 の原因**である
    （分母は価格が無くても作れるので、分子とは母集団が違う）。
    """
    fx = (pack.get("meta") or {}).get("fx_rate") or 160.0
    divisors = _divisors_from_config()
    out = []
    for h in pack.get("holdings") or []:
        shares = float(h.get("shares") or 0.0)
        cost = float(h.get("cost_price") or 0.0)
        cur = h.get("currency") or "JPY"
        row = dict(h)

        # 取得原価は、可能なら実測から逆算する（value - pl は厳密）。
        # 逆算できない行だけ取得単価から組む。そのとき投信は口数建てなので
        # unit_divisor で割る。**パックは divisor を落として保存するため**、
        # config から引き直す。これを落とすと取得原価が1万倍になり、
        # 健全パックで偽の「回帰」が出る（実際に一度出した）。
        if h.get("value_jpy") is not None and h.get("pl_jpy") is not None:
            row["cost_jpy"] = float(h["value_jpy"]) - float(h["pl_jpy"])
        else:
            key = str(h.get("symbol") or h.get("name") or "")
            divisor = float(divisors.get(key) or h.get("unit_divisor") or 1) or 1.0
            cost_local = cost * shares / divisor
            row["cost_jpy"] = cost_local * fx if cur == "USD" else cost_local
        if h.get("price") is None:
            row["price_status"] = observation.UNAVAILABLE
            row["price_unavailable_reason"] = h.get("error") or "possibly delisted"
        else:
            row["price_status"] = observation.OBSERVED
        out.append(row)
    return out


def rebuild(pack: dict) -> dict:
    """修理後のコードで、同じ保有データから集計と表示を作り直す。"""
    analyses = to_analyses(pack)
    invested, cov = observation.partial_total(analyses, "value_jpy", label="評価額")
    base = {
        "analyses": analyses,
        "total_jpy": invested + float((pack.get("portfolio") or {}).get("cash_jpy") or 0.0),
        "invested_jpy": invested,
        "cash_jpy": (pack.get("portfolio") or {}).get("cash_jpy"),
        "fx_rate": (pack.get("meta") or {}).get("fx_rate"),
        "coverage": cov.to_dict(),
        "is_partial": not cov.complete,
        "price_failures": observation.core_failures(analyses),
    }
    return _portfolio_summary(base)


def main(argv: list) -> int:
    pack = json.loads(PACK_PATH.read_text(encoding="utf-8"))
    before = pack.get("portfolio") or {}
    after = rebuild(pack)

    quality = _data_quality(pack.get("holdings") or [],
                            (pack.get("meta") or {}).get("network"))
    report = driver.build_abort_report(pack, quality, "20260808")

    if "--show-report" in argv:
        print(report)
        return 0

    old_report = (REPO / "output" / "週次PF分析_20260808.md").read_text(encoding="utf-8")

    new_pack = dict(pack)
    new_pack["portfolio"] = after
    header = driver.build_header(new_pack)

    def disclosed(pf: dict) -> int:
        n = 0
        n += 1 if (pf.get("coverage") or {}).get("note") is not None and not (
            pf.get("coverage") or {}).get("complete", True) else 0
        return n

    out = {
        "before": {
            "total_jpy": before.get("total_jpy"),
            "pl_pct": before.get("pl_pct"),
            "coverage_disclosed": "coverage" in before,
            "report_lines": len(old_report.splitlines()),
            "unavailable_mentions": old_report.count("取得できず"),
        },
        "after": {
            "total_jpy": after.get("total_jpy"),
            "pl_pct": after.get("pl_pct"),
            "pl_pct_suppressed": after.get("pl_pct_suppressed"),
            "coverage": after.get("coverage"),
            "price_failures": len(after.get("price_failures") or []),
            "report_lines": len(report.splitlines()),
            "unavailable_mentions": report.count("取得できず"),
        },
        "header_after": header,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))

    if HEALTHY_PATH.exists():
        healthy = json.loads(HEALTHY_PATH.read_text(encoding="utf-8"))
        h_before = healthy.get("portfolio") or {}
        h_after = rebuild(healthy)
        print("\n[健全パック 8/9 の対照（数値が変わっていないこと）]")
        print(json.dumps({
            "before_total": h_before.get("total_jpy"),
            "after_total": h_after.get("total_jpy"),
            "before_pl_pct": h_before.get("pl_pct"),
            "after_pl_pct": h_after.get("pl_pct"),
            "after_suppressed": h_after.get("pl_pct_suppressed"),
            "coverage_complete": (h_after.get("coverage") or {}).get("complete"),
        }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
