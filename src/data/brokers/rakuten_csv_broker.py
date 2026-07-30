"""楽天証券CSV由来の実在残高 (土曜設計書 提案1)。

楽天は API を持たない。MS2 RSS は公式ヘルプ上 **国内株式のみ**が対象で、
このPFの評価額の約8割を占める米国株・投信をカバーできない。
したがって「保有商品一覧（すべて）」CSV の取り込み結果を実在残高として扱う。

## ここが模型と実在の両方を兼ねる問題について

`config/weekly_holdings.yaml` は CSV 取り込みの出力であり、同時に週次レポートの
入力（＝模型）でもある。つまりこのソース単体では「模型 vs 実在」の照合にならない。

そこで本モジュールは **CSV の生ファイルを直接読み直す**ことを既定とする。
YAML は取り込み後に手で編集され得るし、売買後に取り込みを忘れれば古くなる。
CSV が見つからない場合のみ YAML の `source:` ブロックを時点情報として使い、
その旨を `detail['degraded']` に明示する（黙って同一物を突き合わせない）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

from src.data.brokers.base import make_position, make_snapshot

#: 楽天CSVがカバーする範囲。国内株・米国株・投信・外貨預り金がすべて1ファイルに入る。
SCOPE = ["JP", "US", "FUND", "CASH"]

_SOURCE = "rakuten_csv"


def _load_yaml(path: str | Path) -> dict:
    try:
        import yaml

        p = Path(path)
        if not p.exists():
            return {}
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _positions_from_yaml(config: dict) -> list[dict]:
    out: list[dict] = []
    for h in config.get("holdings") or []:
        if not isinstance(h, dict):
            continue
        out.append(make_position(
            h.get("quote_symbol"),
            h.get("shares"),
            name=h.get("name"),
            account=h.get("account"),
            cost_price=h.get("cost_price"),
            currency=h.get("currency"),
            market="FUND" if not h.get("quote_symbol") else None,
            raw={"unit_divisor": h.get("unit_divisor"),
                 "last_known_price": h.get("last_known_price")},
        ))
    return out


def _cash_from_yaml(config: dict) -> list[dict]:
    out: list[dict] = []
    for c in config.get("cash") or []:
        if not isinstance(c, dict):
            continue
        amount = c.get("amount")
        out.append({
            "currency": (c.get("currency") or "JPY").upper(),
            "amount": float(amount) if isinstance(amount, (int, float)) else None,
            "name": c.get("name"),
            "purpose": c.get("purpose"),  # 提案9: 現金の目的（未設定なら警告対象）
        })
    return out


def _positions_from_csv(parsed: dict) -> tuple[list[dict], list[dict]]:
    """`read_asset_balance` の戻り（dict）をポジション/現金に振り分ける。"""
    positions: list[dict] = []
    for r in parsed.get("holdings") or []:
        if not isinstance(r, dict):
            continue
        positions.append(make_position(
            r.get("quote_symbol") or r.get("code"),
            r.get("shares"),
            name=r.get("name"),
            account=r.get("account"),
            cost_price=r.get("cost_price"),
            currency=r.get("currency"),
            market_value=r.get("market_value_jpy"),
            market=r.get("asset_type"),
            raw={k: v for k, v in r.items() if k not in ("name", "shares")},
        ))

    cash: list[dict] = []
    for c in parsed.get("cash") or []:
        if not isinstance(c, dict):
            continue
        cash.append({
            "currency": (c.get("currency") or "JPY").upper(),
            "amount": c.get("amount"),
            "name": c.get("name"),
            "purpose": None,  # 提案9: 目的未割当の現金は警告対象
        })
    return positions, cash


def fetch(
    holdings_config_path: str | Path = "config/weekly_holdings.yaml",
    csv_path: Optional[str | Path] = None,
    prefer_csv: bool = True,
) -> dict:
    """楽天の実在残高スナップショットを返す。

    CSV の生ファイルが読めればそれを実在とする（模型 YAML とは独立)。
    読めない場合は YAML の `source:` を時点として縮退し、`degraded` を立てる。
    """
    config = _load_yaml(holdings_config_path)
    src_meta = config.get("source") or {}
    exported_at = src_meta.get("exported_at")

    if prefer_csv:
        found = _try_read_csv(csv_path, src_meta)
        if found is not None:
            positions, cash, used_path, as_of = found
            # 模型を作った元CSVと同一ファイルなら、この照合は循環している。
            # 「一致した」ではなく「取り込み後の売買は検出できない」が正しい。
            circular = _same_file(used_path, src_meta.get("file"))
            detail = {"csv_path": str(used_path), "degraded": False,
                      "circular": circular}
            if circular:
                detail["circular_reason"] = (
                    "この照合は、模型を生成した元CSVとの突合です。取り込み後に約定した"
                    "売買は原理的に検出できません。独立した照合にするには、次に楽天証券から"
                    "CSVを保存したとき、**取り込む前に** reconcile を実行してください"
                    "（新しいCSV × 前回取り込み時点の模型 の突合になります）。"
                )
            return make_snapshot(
                _SOURCE, available=True, positions=positions, cash=cash,
                as_of=as_of or exported_at, scope=SCOPE, detail=detail,
            )

    if not config.get("holdings"):
        return make_snapshot(
            _SOURCE, available=False, scope=SCOPE,
            error="楽天CSVも保有YAMLも読めませんでした",
            detail={"degraded": True},
        )

    return make_snapshot(
        _SOURCE, available=True,
        positions=_positions_from_yaml(config),
        cash=_cash_from_yaml(config),
        as_of=exported_at, scope=SCOPE,
        detail={
            "degraded": True,
            "degraded_reason": (
                "CSV原本が見つからないため取り込み済みYAMLを実在残高として扱いました。"
                "模型と同一ソースのため、この照合は取り込み漏れを検出できません。"
            ),
            "csv_file": src_meta.get("file"),
        },
    )


def _try_read_csv(csv_path, src_meta: dict):
    """CSV原本を読む。読めなければ None。"""
    try:
        from src.data.rakuten_csv import find_latest_csv, read_asset_balance
    except Exception:
        return None

    candidates: list[Path] = []
    if csv_path:
        candidates.append(Path(csv_path))
    else:
        try:
            latest = find_latest_csv()
            if latest:
                candidates.append(Path(latest))
        except Exception:
            pass
        # 取り込み時に使ったファイル名が Downloads に残っていることがある
        name = src_meta.get("file")
        if name:
            candidates.append(Path.home() / "Downloads" / str(name))

    for p in candidates:
        try:
            if not p.exists():
                continue
            parsed = read_asset_balance(str(p))
        except Exception:
            continue
        if not isinstance(parsed, dict) or not parsed.get("holdings"):
            continue
        positions, cash = _positions_from_csv(parsed)
        as_of = parsed.get("modified_at") or _mtime_iso(p)
        return positions, cash, p, as_of
    return None


def _same_file(used: Path, recorded_name: Any) -> bool:
    """読んだCSVが、模型を生成した元CSVと同じか。"""
    if not recorded_name:
        return False
    return used.name.strip().lower() == str(recorded_name).strip().lower()


def _mtime_iso(p: Path) -> Optional[str]:
    from datetime import datetime, timezone

    try:
        return datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat()
    except Exception:
        return None


if __name__ == "__main__":  # 手動確認用
    import json

    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(fetch(), ensure_ascii=False, indent=2, default=str))
