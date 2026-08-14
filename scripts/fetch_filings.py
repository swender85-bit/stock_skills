#!/usr/bin/env python3
"""開示原文を引く -- 一次観測（primary_observation）の取得 CLI.

## なぜこれがあるか

系譜会計は「一次観測 / 外部言説 / ユーザー言明 / 自己推論」を区別する設計だったが、
**一次情報を取りに行くコードが1行も無かった**（claims 台帳は0件）。
その結果、`reground()` が要求する錨を1つも作れず、深度会計が飾りになっていた。

さらに 2026-08-06、トーメンデバイス(2737.T)が4営業日で +120%（4連続ストップ高）
したとき、**その理由を取得できなかった**。yfinance の英語ニュースは9ヶ月前のもの
1件、Grok は 403。これは「材料が無かった」のではなく「調べられなかった」。

**大量保有報告書は、誰が大量に買ったかを名前で示す唯一の一次資料である。**

## 使い方

    python scripts/fetch_filings.py --status              # 情報源が使えるか診断
    python scripts/fetch_filings.py --symbol QCOM         # 米国株（SEC EDGAR）
    python scripts/fetch_filings.py --symbol 2737.T       # 日本株（EDINET）
    python scripts/fetch_filings.py --symbol 2737.T --explain-move   # 急騰の需給要因
    python scripts/fetch_filings.py --holdings            # 保有全銘柄
    python scripts/fetch_filings.py --symbol QCOM --financials       # XBRL 年度財務

## 有効化（どちらも無料）

    SEC_EDGAR_UA="your-name your@email.com"   # 米国。キー不要・連絡先必須
    EDINET_API_KEY=...                        # 日本。無料登録で購読キー

未設定でも落ちない。**「取得できませんでした」と出るだけで、
「開示が無かった」とは決して書かない。**
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from src.core.primary_source import (  # noqa: E402
    build_primary_section,
    fetch_filings,
    source_status,
)


def print_status() -> int:
    status = source_status()
    print("■ 一次情報源の状態")
    print()
    ok = 0
    for name, s in status.items():
        mark = "✅" if s["available"] else "⛔"
        print(f"{mark} {name:12} 市場={s['market']:3} 環境変数={s['env']:16} {s['cost']}")
        if not s["available"]:
            print(f"     {s['reason']}")
        else:
            ok += 1
    print()
    if ok == 0:
        print("⚠️ 一次観測を1件も取得できない状態です。")
        print("   このとき、システムの全データは**深度1の外部言説**"
              "（yfinance/Finnhub の加工済み指標）だけになり、")
        print("   系譜会計の再接地（reground）は**原理的に不可能**です。")
        print()
        print("   どちらも無料です:")
        print('     SEC_EDGAR_UA="氏名 メールアドレス"   # 米国株。登録不要')
        print("     EDINET_API_KEY=...                    # 日本株。無料登録")
    return 0 if ok else 1


def print_filings(result: dict) -> None:
    symbol = result.get("symbol")
    if not result.get("available"):
        print(f"⛔ {symbol}: **取得できませんでした**")
        print(f"   {result.get('reason') or result.get('note') or ''}")
        print("   ※ これは『開示が無かった』ではありません。")
        return

    filings = result.get("filings") or []
    print(f"✅ {symbol}（{result.get('source')}）— {len(filings)}件")
    if result.get("note"):
        print(f"   {result['note']}")
    for f in filings:
        form = f.get("form") or f.get("doc_type_label") or "?"
        when = f.get("filed_at") or f.get("submitted_at") or "?"
        mark = "🔴" if f.get("supply_demand") else "  "
        print(f"   {mark} [{form:20}] {str(when)[:10]}  {str(f.get('title') or '')[:44]}")
        print(f"        {f.get('url')}")


def main() -> int:
    ap = argparse.ArgumentParser(description="開示原文（一次観測）を引く")
    ap.add_argument("--symbol", action="append", default=[], help="銘柄（複数可）")
    ap.add_argument("--holdings", action="store_true", help="保有全銘柄を対象にする")
    ap.add_argument("--days", type=int, default=30, help="遡る日数")
    ap.add_argument("--limit", type=int, default=8, help="1銘柄あたりの件数上限")
    ap.add_argument("--status", action="store_true", help="情報源の状態を診断して終了")
    ap.add_argument("--explain-move", action="store_true",
                    help="急騰・急落の需給側の説明を探す（日本株のみ）")
    ap.add_argument("--financials", action="store_true",
                    help="XBRL の年度財務を出す（米国株のみ）")
    ap.add_argument("--persist", action="store_true",
                    help="取得した主張を data/claims/ に保存する")
    args = ap.parse_args()

    if args.status:
        return print_status()

    symbols = list(args.symbol)
    if args.holdings:
        try:
            import yaml

            cfg = yaml.safe_load(
                (REPO / "config" / "weekly_holdings.yaml").read_text(encoding="utf-8"))
            symbols += [h.get("quote_symbol") for h in (cfg.get("holdings") or [])
                        if h.get("quote_symbol")]
        except Exception as exc:
            print(f"保有の読み込みに失敗しました: {exc}")
            return 1
    symbols = sorted({s for s in symbols if s})

    if not symbols:
        ap.error("--symbol か --holdings を指定してください")

    status = source_status()
    if not any(s["available"] for s in status.values()):
        print_status()
        print()
        print("——— 情報源が無いため取得を実行しません ———")
        return 1

    if args.explain_move:
        from src.data import edinet_client

        for symbol in symbols:
            print()
            result = edinet_client.explain_move(symbol, days=args.days)
            if not result.get("available"):
                print(f"⛔ {symbol}: {result.get('reason')}")
                continue
            print(f"■ {symbol} の需給要因")
            print(f"  {result['explanation']}")
            for f in result.get("supply_demand_filings") or []:
                print(f"   🔴 {f.get('doc_type_label')} / {f.get('filer')} "
                      f"/ {str(f.get('submitted_at'))[:10]}")
                print(f"      {f.get('url')}")
        return 0

    if args.financials:
        from src.data import edgar_client

        for symbol in symbols:
            print()
            result = edgar_client.key_financials(symbol)
            if not result.get("available"):
                print(f"⛔ {symbol}: {result.get('reason')}")
                continue
            print(f"■ {symbol} 年度財務（XBRL・開示原文）")
            for label, fact in (result.get("facts") or {}).items():
                val = fact.get("value")
                shown = f"{val:,.0f}" if isinstance(val, (int, float)) else str(val)
                window = "時点" if fact.get("window") == "instant" else "期間"
                period = (f"〜{fact.get('end')}" if fact.get("window") == "instant"
                          else f"{fact.get('start')}〜{fact.get('end')}")
                mark = "⚠️" if (fact.get("stale_years") or 0) >= 1.0 else "  "
                print(f"   {mark} {label:16} {shown:>20}  "
                      f"[{window} {period}] {fact.get('form')}")
            if result.get("stale"):
                print(f"   ⚠️ **古い値**: {', '.join(result['stale'])}")
                print("      最新として読まないでください。")
            if result.get("missing"):
                print(f"   ℹ️ 取得できなかった項目: {', '.join(result['missing'])}")
                print("      そのタグを使っていない可能性。**0ではありません。**")
        return 0

    section = build_primary_section(symbols, days=args.days,
                                    limit_per_symbol=args.limit,
                                    persist=args.persist)
    print("■ 開示原文（一次観測）")
    print()
    for symbol in symbols:
        print_filings(section["by_symbol"].get(symbol) or
                      {"symbol": symbol, "available": False})
        print()

    print(f"一次観測（深度0）: {section['primary_count']}件")
    print(f"  {section['note']}")
    if args.persist and section["claims"]:
        print(f"  data/claims/ に {len(section['claims'])}件を保存しました。")
        print("  → これで reground()（再接地）が使える錨ができました。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
