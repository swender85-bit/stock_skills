#!/usr/bin/env python3
"""楽天証券「保有商品一覧（すべて）」CSV から保有構成を取り込む。

    python scripts/import_rakuten_csv.py            # Downloads の最新CSVを自動検出
    python scripts/import_rakuten_csv.py --path X   # ファイル指定
    python scripts/import_rakuten_csv.py --dry-run  # 差分だけ見る

## 何が自動で、何が手動か

- **保有構成（銘柄・数量・取得単価）**: このCSVを投げたときだけ更新される
- **日々の株価**: CSVとは無関係に、レポート生成のたび yfinance が取り直す

つまり売買したときだけCSVを落とせばよく、価格のために毎日落とす必要はない。

## 取得方法（1クリック）

楽天証券にログイン → マイメニュー → 「保有商品一覧（すべて）」 →
「CSVで保存」。`assetbalance(all)_YYYYMMDD_HHMMSS.csv` が落ちてくる。
MarketSpeed II も Excel の数式も要らない。

既存の `config/weekly_holdings.yaml` にある `leverage` / `unit_divisor` /
`category` / `note` は銘柄ごとに引き継ぐ（CSVには無い情報で、
失うとレバレッジETFの下落シナリオ計算が壊れるため）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import yaml  # noqa: E402

from src.data.rakuten_csv import (  # noqa: E402
    SnapshotUnavailable,
    find_latest_csv,
    read_asset_balance,
)

DEFAULT_TARGET = REPO_ROOT / "config" / "weekly_holdings.yaml"

#: CSV に無く、既存 YAML から引き継ぐキー。失うと計算が壊れる。
_CARRIED_KEYS = ("leverage", "unit_divisor", "category", "note")

#: 投信は yfinance にティッカーが無い。基準価額は CSV の現在値を使う。
_FUND_TYPE = "投資信託"


def _key(holding: dict) -> tuple:
    """同一ポジションの同定キー。同一銘柄でも口座が違えば別建て。

    投信はティッカーが無いので名前で同定するしかないが、楽天の表記が
    「iFreeNEXT FANG+」→「iFreeNEXT FANG+インデックス」のように揺れる。
    そのままだと「消滅＋新規」に化けて note や leverage を取りこぼすため、
    シンボル無しの銘柄は名前を正規化して突き合わせる。
    """
    symbol = (holding.get("quote_symbol") or "").strip()
    ident = symbol or _normalize_fund_name(holding.get("name", ""))
    return (ident, (holding.get("account") or "").strip())


#: 投信名の揺れを吸収するために落とす語
_FUND_NAME_NOISE = ("インデックス", "ファンド", "（", "(", "　", " ")


def _normalize_fund_name(name: str) -> str:
    n = (name or "").strip()
    for noise in _FUND_NAME_NOISE:
        n = n.replace(noise, "")
    return n.lower()


def _normalize_account(account: str) -> str:
    """楽天の口座表記を既存YAMLの表記に寄せる。"""
    a = (account or "").strip()
    return {
        "NISA成長投資枠": "NISA成長",
        "NISAつみたて投資枠": "NISAつみたて",
    }.get(a, a)


def build_holdings(csv_data: dict, existing: list[dict]) -> list[dict]:
    """CSV の保有を YAML 形式に変換し、既存のメタ情報を引き継ぐ。"""
    prior = {_key(h): h for h in existing}
    out: list[dict] = []

    for row in csv_data["holdings"]:
        account = _normalize_account(row.get("account", ""))
        symbol = (row.get("quote_symbol") or "").strip()
        is_fund = row.get("asset_type") == _FUND_TYPE

        entry: dict[str, Any] = {
            "name": row.get("name", ""),
            # 投信はティッカーが無い。null にして価格取得をスキップさせる。
            "quote_symbol": symbol or None,
            "account": account,
            "shares": _int_if_whole(row.get("shares")),
            "cost_price": row.get("cost_price"),
            "currency": row.get("currency", "JPY"),
        }

        old = prior.get(_key({**entry, "quote_symbol": symbol,
                              "account": account})) or {}
        for k in _CARRIED_KEYS:
            if k in old:
                entry[k] = old[k]

        if is_fund:
            # 基準価額は CSV の現在値を正とする（yfinance では引けない）
            entry.setdefault("unit_divisor", old.get("unit_divisor", 10000))
            if row.get("price") is not None:
                entry["last_known_price"] = _int_if_whole(row["price"])

        out.append(entry)

    return out


def _int_if_whole(value: Any) -> Any:
    """150.0 のような整数値を int にする（YAML を読みやすく保つ）。"""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def build_cash(csv_data: dict, existing: list[dict]) -> list[dict]:
    prior_notes = {c.get("currency"): c.get("note") for c in existing}
    out = []
    for c in csv_data.get("cash", []):
        entry = {
            "name": c.get("name", ""),
            "amount": c.get("amount"),
            "currency": c.get("currency", "USD"),
        }
        note = prior_notes.get(entry["currency"])
        if note:
            entry["note"] = note
        out.append(entry)
    return out


def diff_holdings(old: list[dict], new: list[dict]) -> list[str]:
    """人が読める差分。何も変わらなければ空。"""
    old_map = {_key(h): h for h in old}
    new_map = {_key(h): h for h in new}
    lines: list[str] = []

    for key in new_map.keys() - old_map.keys():
        h = new_map[key]
        lines.append(f"  + 新規 {h['name']} ({h.get('account')}) "
                     f"{h['shares']} @ {h['cost_price']}")
    for key in old_map.keys() - new_map.keys():
        h = old_map[key]
        lines.append(f"  - 消滅 {h.get('name')} ({h.get('account')}) "
                     f"{h.get('shares')}")
    for key in old_map.keys() & new_map.keys():
        o, n = old_map[key], new_map[key]
        for field, label in (("shares", "数量"), ("cost_price", "取得単価")):
            if _differs(o.get(field), n.get(field)):
                lines.append(f"  ~ {n['name']} ({n.get('account')}) "
                             f"{label}: {o.get(field)} -> {n.get(field)}")
    return sorted(lines)


def _differs(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is not b
    try:
        return abs(float(a) - float(b)) > 1e-9
    except (TypeError, ValueError):
        return a != b


def render_yaml(config: dict, csv_data: dict) -> str:
    """既存ファイルの解説コメントを保ったまま YAML を書き出す。"""
    source = Path(csv_data["source_path"]).name
    header = f"""\
# 週次レポート用の保有銘柄定義
#
# ⚠️ このファイルは scripts/import_rakuten_csv.py が自動生成する。
#    手で直してもよいが、次の取り込みで上書きされる。
#
# 出典: 楽天証券「保有商品一覧（すべて）」CSV
#   {source}
#   取り込み日時: {csv_data['modified_at']}
#
# ■ 何が自動で、何が手動か
#   保有構成（銘柄・数量・取得単価） … このCSVを投げたときだけ更新
#   日々の株価                       … レポート生成のたび yfinance が自動取得
#
#   つまり売買したときだけCSVを落とせばよい。価格のために毎日落とす必要はない。
#
# ■ CSVの取り方（1クリック）
#   楽天証券にログイン → マイメニュー → 保有商品一覧（すべて） → CSVで保存
#   落ちてきたら: python scripts/import_rakuten_csv.py
#
# quote_symbol: 株価取得に使うティッカー。null なら価格取得をスキップし、
#               last_known_price を使う（投信など yfinance に無い銘柄）。
"""
    body = yaml.safe_dump(
        config, allow_unicode=True, sort_keys=False, default_flow_style=False,
    )
    return header + "\n" + body


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", help="CSVのパス（省略時は自動検出）")
    parser.add_argument("--target", default=str(DEFAULT_TARGET),
                        help="書き出す保有定義YAML")
    parser.add_argument("--dry-run", action="store_true",
                        help="書き込まず差分だけ表示")
    args = parser.parse_args(argv)

    csv_path = Path(args.path) if args.path else find_latest_csv()
    if csv_path is None:
        print("[error] assetbalance*.csv が見つかりません。")
        print("        楽天証券 → マイメニュー → 保有商品一覧（すべて） → CSVで保存")
        print("        を実行してから、もう一度このコマンドを走らせてください。")
        return 1

    try:
        csv_data = read_asset_balance(csv_path)
    except SnapshotUnavailable as e:
        print(f"[error] {e}")
        return 1

    target = Path(args.target)
    config = {}
    if target.exists():
        config = yaml.safe_load(target.read_text(encoding="utf-8")) or {}

    old_holdings = config.get("holdings") or []
    new_holdings = build_holdings(csv_data, old_holdings)
    changes = diff_holdings(old_holdings, new_holdings)

    print(f"読み込み: {csv_path.name}（{csv_data['age_hours']}時間前）")
    print(f"保有 {len(new_holdings)} 件 / 現金 {len(csv_data.get('cash', []))} 件")

    if changes:
        print("\n保有構成の変化:")
        for line in changes:
            print(line)
    else:
        print("\n保有構成に変化はありません。")

    # 取り込み元を記録する。これが無いとレポートが
    # 「実口座の売買が反映されていない」と事実と逆の警告を出す。
    config["source"] = {
        "type": "rakuten_csv",
        "file": csv_path.name,
        "exported_at": csv_data["modified_at"],
    }
    config["holdings"] = new_holdings
    config["cash"] = build_cash(csv_data, config.get("cash") or [])
    usd = csv_data.get("fx", {}).get("USD")
    if usd:
        fx = config.setdefault("fx", {})
        fx.setdefault("usdjpy_symbol", "JPY=X")
        fx["fallback_rate"] = usd
        print(f"\n為替 fallback を更新: ¥{usd}/USD")
    config.setdefault("base_currency", "JPY")

    if args.dry_run:
        print("\n[dry-run] 書き込みませんでした。")
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_yaml(config, csv_data), encoding="utf-8")
    print(f"\n✅ 更新: {target}")
    print("   次回のレポートからこの構成で計算されます（株価は毎回自動取得）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
