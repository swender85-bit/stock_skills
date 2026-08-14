"""エンティティ解決（名寄せ）— `config/entity_aliases.yaml` を引く。

未解決は**捨てない**。`entities_unresolved` に残し、週次で人間が確認できるようにする。
捨てると「その銘柄について読んでいない」という誤った統計になる。
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

_CONFIG = Path(__file__).resolve().parents[3] / "config" / "entity_aliases.yaml"

#: 4桁数字＋.T、または2〜5文字の英大文字
_TICKER_RE = re.compile(r"\b(\d{4}\.T|\d{4}|[A-Z]{2,5})\b")


@lru_cache(maxsize=1)
def _rules() -> dict:
    try:
        import yaml

        return yaml.safe_load(_CONFIG.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


@lru_cache(maxsize=1)
def _lookup() -> dict:
    """別名 → 正規シンボル。長い別名から先に当てるため長さ順に持つ。"""
    table: dict = {}
    for canonical, names in (_rules().get("aliases") or {}).items():
        table[str(canonical).upper()] = canonical
        table[str(canonical).lower()] = canonical
        for n in names or []:
            table[str(n)] = canonical
            table[str(n).upper()] = canonical
            table[str(n).lower()] = canonical
    return table


def canonical(name: str) -> Optional[str]:
    """1つの表記を正規シンボルへ。解決できなければ None。"""
    if not name:
        return None
    raw = str(name).strip()
    table = _lookup()
    for key in (raw, raw.upper(), raw.lower()):
        if key in table:
            return table[key]
    return None


def underlying(symbol: str) -> Optional[str]:
    """レバレッジETF・投信の原資産。"""
    return (_rules().get("derives_from") or {}).get(symbol)


def watch_only() -> list:
    return list(_rules().get("watch_only") or [])


def extract(text: str, max_entities: int = 12) -> dict:
    """本文から銘柄を抽出する。

    Returns
    -------
    dict
        `{"entities": [...正規シンボル], "raw": [...原文表記], "unresolved": [...]}`

    **未解決を捨てない。** 「解決できなかった」を「言及が無かった」にしない。
    """
    text = str(text or "")
    found: dict = {}
    unresolved: list = []

    # 別名テーブルの長い順に走査（「ニトリホールディングス」が「ニトリ」に負けない）
    for alias in sorted(_lookup().keys(), key=len, reverse=True):
        if len(alias) < 2:
            continue
        if alias in text:
            sym = _lookup()[alias]
            found.setdefault(sym, []).append(alias)

    # ティッカー様の文字列のうち、解決できなかったものを退避
    for m in _TICKER_RE.findall(text):
        if canonical(m):
            continue
        if m in ("THE", "AND", "FOR", "USD", "JPY", "ETF", "IPO", "CEO", "CFO", "GDP"):
            continue
        if m not in unresolved:
            unresolved.append(m)

    return {
        "entities": sorted(found.keys())[:max_entities],
        "raw": sorted({a for v in found.values() for a in v}),
        "unresolved": unresolved[:max_entities],
    }


def held_symbols(config: Optional[dict] = None) -> list:
    """現在の保有銘柄（正規シンボル）。偏食監査の分子に使う。

    **口座区分で重複させない。** 味の素は特定とNISAで2ポジションだが、
    読んだ記事は口座に紐づかないので1銘柄として数える。
    """
    try:
        import yaml

        root = Path(__file__).resolve().parents[3]
        cfg = config or yaml.safe_load(
            (root / "config" / "weekly_holdings.yaml").read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    out = []
    for h in cfg.get("holdings") or []:
        sym = h.get("quote_symbol") or h.get("symbol")
        resolved = canonical(sym) if sym else None
        if not resolved:
            resolved = canonical(h.get("name") or "")
        if resolved and resolved not in out:
            out.append(resolved)
    return out
