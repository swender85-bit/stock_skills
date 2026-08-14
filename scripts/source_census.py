"""情報源の全数点検 — 「繋がっている」と「黙って死んでいる」を分ける。

## なぜこれがあるか

2026-08-11 の修理で、同じ形の障害が**2件**見つかった。

1. `config/thresholds.yaml` が Windows で一度も読めていなかった
   （cp932 で UTF-8 を開いて例外 → 握り潰し → 全閾値が既定値に沈黙で落ちる）
2. `SEC_EDGAR_UA` が `.env` に設定済みなのに `edgar_client` が `load_dotenv` を
   呼んでおらず、**一次観測が設定済みのまま一度も繋がっていなかった**

どちらも「設定した」と「効いている」が食い違っていた。**設定した本人にも
気づけない**のが最悪の性質で、レポートには「取得できませんでした」とだけ出る。

この点検は各情報源を**新しいプロセスで**叩き、次の3つを厳密に分ける。

- ``live``       : 実際に値が返った
- ``configured`` : 資格情報はあるが、可否をこの場で確認していない／確認できなかった
- ``dark``       : **確実に使えない**（資格情報が見えていない、または相手に拒否された）

判定の軸は「資格情報の有無」ではなく **「今この瞬間に使えるか」** である。
2026-08-13 に `XAI_API_KEY` が設定済みのまま全リクエストが 403 で拒否されていた
（チームにクレジットが無い）ため、キーの有無だけを見る旧実装は
**使えない情報源を 🟡 で通し、「黙って死んでいるもの」の一覧から漏らしていた。**
拒否は待っても直らないので ``dark`` に置き、直し方を必ず添える。

使い方::

    python scripts/source_census.py            # 全点検
    python scripts/source_census.py --json     # 機械可読
    python scripts/source_census.py --offline  # ネットワークを叩かない（資格情報だけ）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# Windows のコンソール既定は cp932 で、絵文字も一部の記号も出せずに
# UnicodeEncodeError で落ちる。**点検器が出力で落ちるのは本末転倒**なので
# 標準出力を UTF-8 に固定し、出せない文字は置換する。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 🔴 最初に .env を読む。個々のモジュールに任せると「読んだモジュールを
# 先に import したときだけ動く」というインポート順依存になる。
from src.core.env import load_env  # noqa: E402

load_env()

LIVE, CONFIGURED, DARK = "live", "configured", "dark"


def _result(name: str, kind: str, status: str, detail: str,
            fix: str = "") -> dict:
    return {"source": name, "kind": kind, "status": status,
            "detail": detail, "fix": fix}


def probe_yahoo(offline: bool) -> dict:
    if offline:
        return _result("yahoo", "price", CONFIGURED, "資格情報不要（未接続確認）")
    try:
        from src.data import yahoo_client

        info = yahoo_client.get_stock_info("AAPL")
        price = (info or {}).get("price")
        if price:
            return _result("yahoo", "price", LIVE, f"AAPL = {price}")
        return _result("yahoo", "price", CONFIGURED, "応答はあるが価格が空")
    except Exception as exc:
        return _result("yahoo", "price", CONFIGURED, f"{type(exc).__name__}: {exc}")


def probe_finnhub(offline: bool) -> dict:
    key = os.environ.get("FINNHUB_API_KEY", "").strip()
    if not key:
        return _result("finnhub", "news", DARK, "FINNHUB_API_KEY が見えていません",
                       ".env に FINNHUB_API_KEY を置く")
    if offline:
        return _result("finnhub", "news", CONFIGURED, f"キーあり（{len(key)}文字）")
    try:
        from src.data import finnhub_client

        news = finnhub_client.get_company_news("AAPL", days=3)
        if news:
            return _result("finnhub", "news", LIVE, f"AAPL ニュース {len(news)}件")
        return _result("finnhub", "news", CONFIGURED, "キーは通るが0件")
    except Exception as exc:
        return _result("finnhub", "news", CONFIGURED, f"{type(exc).__name__}: {exc}")


def probe_edgar(offline: bool) -> dict:
    try:
        from src.data import edgar_client

        ua = edgar_client.user_agent()
    except Exception as exc:
        return _result("sec_edgar", "primary", DARK, f"{type(exc).__name__}: {exc}")
    if not ua:
        return _result("sec_edgar", "primary", DARK,
                       "SEC_EDGAR_UA がモジュールから見えていません",
                       ".env に SEC_EDGAR_UA=氏名 <メール> を置く / load_env() を通す")
    if offline:
        return _result("sec_edgar", "primary", CONFIGURED, f"UA あり: {ua[:20]}...")
    try:
        res = edgar_client.recent_filings("QCOM", limit=1)
        rows = (res or {}).get("filings") or []
        if rows:
            return _result("sec_edgar", "primary", LIVE,
                           f"QCOM 直近開示 {rows[0].get('form')} "
                           f"{rows[0].get('filed_at')}")
        return _result("sec_edgar", "primary", CONFIGURED,
                       f"UA は通るが開示0件: {(res or {}).get('reason') or ''}")
    except Exception as exc:
        return _result("sec_edgar", "primary", CONFIGURED, f"{type(exc).__name__}: {exc}")


def probe_edinet(offline: bool) -> dict:
    key = os.environ.get("EDINET_API_KEY", "").strip()
    if not key:
        return _result("edinet", "primary", DARK,
                       "EDINET_API_KEY が見えていません（日本株の一次観測が全滅）",
                       "https://api.edinet-fsa.go.jp で登録し .env に置く")
    if offline:
        return _result("edinet", "primary", CONFIGURED, "キーあり")
    try:
        from src.data import edinet_client

        res = edinet_client.recent_filings("2802.T", limit=1)
        rows = (res or {}).get("filings") or []
        if rows:
            return _result("edinet", "primary", LIVE, f"2802.T 直近 {rows[0].get('form')}")
        return _result("edinet", "primary", CONFIGURED, "キーは通るが開示0件")
    except Exception as exc:
        return _result("edinet", "primary", CONFIGURED, f"{type(exc).__name__}: {exc}")


def probe_grok(offline: bool) -> dict:
    """🔴 **「キーがある」と「使える」は別。**

    最小のリクエスト（``max_tokens=1``）を1回だけ投げて実際の可否を見る。
    拒否されれば課金されず、通っても1トークン分なので、この確認は安全である。
    ここを実叩きにしないと、クレジット切れのキーが 🟡 のまま残り、
    Grok に依存する経路（trending / --auto-theme / critics / market-research）が
    全部死んでいることに気づけない。
    """
    key = os.environ.get("XAI_API_KEY", "").strip()
    if not key:
        return _result("grok", "discourse", DARK, "XAI_API_KEY が見えていません",
                       ".env に XAI_API_KEY を置く")
    if offline:
        return _result("grok", "discourse", CONFIGURED, f"キーあり（{len(key)}文字・未確認）")
    try:
        import requests

        res = requests.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": "grok-4-1-fast-non-reasoning", "max_tokens": 1,
                  "messages": [{"role": "user", "content": "hi"}]},
            timeout=20)
    except Exception as exc:
        # 回線側で落ちた場合は「使えない」とは言い切れない。dark に落とさない。
        return _result("grok", "discourse", CONFIGURED,
                       f"キーはあるが確認できず（{type(exc).__name__}）")
    if res.status_code == 200:
        return _result("grok", "discourse", LIVE, "応答あり")
    detail = ""
    try:
        detail = str((res.json() or {}).get("error") or "")[:120]
    except Exception:
        detail = res.text[:120]
    fix = ("https://console.x.ai でクレジットを購入する"
           if res.status_code in (402, 403) else "キーの権限を確認する")
    return _result("grok", "discourse", DARK,
                   f"HTTP {res.status_code}: {detail}", fix)


def probe_neo4j(offline: bool) -> dict:
    if offline:
        return _result("neo4j", "context", CONFIGURED, "未接続確認")
    try:
        from src.data.graph_store._common import _get_driver, is_available

        if not is_available():
            return _result("neo4j", "context", DARK,
                           "接続できません（Docker/Neo4j 未起動、または未設定）",
                           "Docker Desktop を起動してから docker compose up -d neo4j")
        drv = _get_driver()
        with drv.session() as s:
            n = s.run("MATCH (n) RETURN count(n) AS c").single()["c"]
        return _result("neo4j", "context", LIVE, f"ノード {n}件")
    except Exception as exc:
        return _result("neo4j", "context", DARK, f"{type(exc).__name__}: {exc}",
                       "docker compose up -d neo4j")


def probe_tei(offline: bool) -> dict:
    url = os.environ.get("TEI_URL", "http://localhost:8081")
    if offline:
        return _result("tei", "context", CONFIGURED, f"URL {url}")
    try:
        import urllib.request

        with urllib.request.urlopen(f"{url}/health", timeout=3) as r:
            ok = r.status == 200
        return _result("tei", "context", LIVE if ok else DARK, f"{url} status={r.status}")
    except Exception as exc:
        return _result("tei", "context", DARK, f"{type(exc).__name__}",
                       "docker compose up -d tei")


def probe_moomoo(offline: bool) -> dict:
    if os.environ.get("MOOMOO_ENABLED", "").lower() not in ("on", "1", "true"):
        return _result("moomoo", "news", DARK, "MOOMOO_ENABLED が on ではありません")
    try:
        import futu  # noqa: F401
    except Exception:
        return _result("moomoo", "news", DARK, "futu-api が入っていません",
                       "pip install futu-api")
    return _result("moomoo", "news", CONFIGURED, "SDK あり（OpenD 起動は週次時のみ）")


def probe_thresholds(offline: bool) -> dict:
    from src.core._thresholds import load_status

    st = load_status()
    if st["loaded"]:
        return _result("thresholds.yaml", "config", LIVE,
                       f"{len(st['sections'])}セクション: {', '.join(st['sections'])}")
    return _result("thresholds.yaml", "config", DARK, st["error"] or "不明")


def probe_holdings(offline: bool) -> dict:
    p = REPO / "config" / "weekly_holdings.yaml"
    if not p.exists():
        return _result("weekly_holdings.yaml", "holdings", DARK, "ファイルがありません")
    try:
        import yaml

        cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        n = len(cfg.get("holdings") or [])
        src = cfg.get("source") or {}
        return _result("weekly_holdings.yaml", "holdings", LIVE if n else DARK,
                       f"{n}銘柄 / 取り込み {src.get('exported_at', '不明')}")
    except Exception as exc:
        return _result("weekly_holdings.yaml", "holdings", DARK, f"{type(exc).__name__}: {exc}")


PROBES = [probe_thresholds, probe_holdings, probe_yahoo, probe_finnhub,
          probe_edgar, probe_edinet, probe_grok, probe_neo4j, probe_tei, probe_moomoo]


def run(offline: bool = False) -> list:
    out = []
    for fn in PROBES:
        try:
            out.append(fn(offline))
        except Exception as exc:  # 点検器自身が落ちても他を止めない
            out.append(_result(fn.__name__, "?", DARK, f"点検器が落ちました: {exc}"))
    return out


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description="情報源の全数点検")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--offline", action="store_true", help="ネットワークを叩かない")
    args = ap.parse_args(argv)

    rows = run(args.offline)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    icon = {LIVE: "✅", CONFIGURED: "🟡", DARK: "⛔"}
    width = max(len(r["source"]) for r in rows)
    print(f"{'情報源':<{width}}  状態  種別        詳細")
    print("-" * 100)
    for r in rows:
        print(f"{r['source']:<{width}}  {icon[r['status']]}    "
              f"{r['kind']:<10}  {r['detail']}")
    dark = [r for r in rows if r["status"] == DARK]
    print()
    print(f"live {sum(1 for r in rows if r['status'] == LIVE)} / "
          f"configured {sum(1 for r in rows if r['status'] == CONFIGURED)} / "
          f"dark {len(dark)}")
    if dark:
        print("\n⛔ 黙って死んでいるもの（直し方）:")
        for r in dark:
            print(f"  - {r['source']}: {r['detail']}")
            if r["fix"]:
                print(f"      → {r['fix']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
