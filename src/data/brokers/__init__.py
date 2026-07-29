"""ブローカー抽象層 — moomoo / 楽天CSV / 手動 を同一形で扱う (土曜設計書 提案1)。

    from src.data.brokers import collect_snapshots
    snaps = collect_snapshots()          # 使えるソースを全部集める

各スナップショットは `available` / `as_of` / `scope` を必ず持ち、
**取れなかったことと保有ゼロを区別できる**。
"""

from __future__ import annotations

import os
from typing import Optional

from src.data.brokers.base import (  # noqa: F401
    DEFAULT_MAX_AGE_HOURS,
    make_position,
    make_snapshot,
    snapshot_summary,
)

#: 既定で試すソース。moomoo は MOOMOO_ENABLED=on のときだけ実際に動く。
DEFAULT_SOURCES = ("rakuten_csv", "moomoo")


def collect_snapshots(
    sources: Optional[list[str]] = None,
    holdings_config_path: str = "config/weekly_holdings.yaml",
    csv_path: Optional[str] = None,
    autostart_opend: bool = True,
) -> list[dict]:
    """指定ソースの実在残高スナップショットを集める。

    1つのソースが落ちても他は返す。全部落ちても空リストではなく
    `available=False` のスナップショットを返す（＝理由が残る）。
    """
    names = list(sources or DEFAULT_SOURCES)
    out: list[dict] = []

    for name in names:
        try:
            if name == "rakuten_csv":
                from src.data.brokers import rakuten_csv_broker

                out.append(rakuten_csv_broker.fetch(
                    holdings_config_path=holdings_config_path, csv_path=csv_path))
            elif name == "moomoo":
                from src.data.brokers import moomoo_broker

                out.append(moomoo_broker.fetch(autostart=autostart_opend))
            else:
                out.append(make_snapshot(name, available=False,
                                         error=f"未知のブローカーソース: {name}"))
        except Exception as e:  # ソース単位で握り潰す
            out.append(make_snapshot(name, available=False,
                                     error=f"{type(e).__name__}: {str(e)[:160]}"))
    return out


def merged_scope(snapshots: list[dict]) -> set[str]:
    """取得**成功した**ソースが実際にカバーしている市場の集合。

    照合エンジンはこの集合の外にある銘柄を「口座に不在」と判定してはならない。
    失敗したソースの scope を含めないのが肝心（含めると、取得失敗した市場の
    保有が全部「幽霊」に化ける）。
    """
    scope: set[str] = set()
    for s in snapshots:
        if s.get("available"):
            scope.update(s.get("scope") or [])
    return scope


def is_reconcilable(snapshots: list[dict]) -> bool:
    """照合できる状態か（成功したソースが1つ以上あるか）。"""
    return any(s.get("available") for s in snapshots)


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("on", "1", "true", "yes")
