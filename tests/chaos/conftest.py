"""カオステストは既定で走らせない (改善7).

## なぜ CI に入れないか

これらは**わざとシステムを壊して、気づくか試す**テスト。ファイル差し替え・
外部依存の落とし込み・vault の削除などを伴い、通常の単体テストより重い。
`pytest tests/ -q` が4,600件で20秒という速さを保つほうが、日々の開発では価値が高い。

代わりに月1回、`scripts/run_chaos.py` で回す。

    python scripts/run_chaos.py            # 全部
    python scripts/run_chaos.py --list     # 何を壊すかだけ見る
    RUN_CHAOS=1 pytest tests/chaos -q      # 直接
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

CHAOS_DIR = Path(__file__).resolve().parent


def pytest_collection_modifyitems(config, items):
    """カオステストだけをスキップする。

    ⚠️ `pytest_collection_modifyitems` は**サブディレクトリの conftest でも
    収集済みの全アイテム**を受け取る。ディレクトリで絞らずにマーカーを付けると
    **スイート4,700件が丸ごとスキップされる**（実装中に踏んだ）。
    しかも pytest は成功扱いで終わるので、緑のまま何も走っていない状態になる。
    """
    if os.environ.get("RUN_CHAOS") == "1":
        return
    skip = pytest.mark.skip(
        reason="カオステストは既定で実行しません（RUN_CHAOS=1 か scripts/run_chaos.py）")
    for item in items:
        try:
            path = Path(str(getattr(item, "path", None) or item.fspath)).resolve()
        except Exception:
            continue
        if CHAOS_DIR in path.parents:
            item.add_marker(skip)
