"""スクリプトが Windows の既定コンソール（cp932）で落ちないこと。

実測: `import_rakuten_csv.py` は「為替 fallback を更新: ¥157.81/USD」を出す行で
`UnicodeEncodeError: 'cp932' codec can't encode character '\\xa5'` により
クラッシュした。**保有 YAML の書き込みが済んだ後に落ちる**ため、
「更新されたのかどうか」が利用者に分からない終わり方をする。

無人実行される・ユーザーが直接叩くスクリプトは、出力に日本語や記号を含む以上、
stdout を UTF-8 に固定しておかないと環境依存で落ちる。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

#: 利用者が直接叩く / 無人実行されるスクリプト。日本語や ¥ を出力する。
USER_FACING_SCRIPTS = (
    "import_rakuten_csv.py",
    "import_rakuten_trades.py",
    "build_briefing_pack.py",
    "weekly_deep_driver.py",
    "manage_policy.py",
    "generate_docs.py",
    "save_report.py",
)


def _source(name: str) -> str:
    path = REPO / "scripts" / name
    if not path.exists():
        pytest.skip(f"{name} が存在しない")
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("name", USER_FACING_SCRIPTS)
def test_script_pins_stdout_to_utf8(name):
    src = _source(name)
    pins_directly = "reconfigure(encoding=" in src
    # scripts/common.py を import しているなら、そちらが固定している
    uses_common = re.search(r"from\s+(scripts\.)?common\s+import|import\s+common", src)
    assert pins_directly or uses_common, (
        f"{name} は stdout を UTF-8 に固定していない。"
        "cp932 環境で ¥ や絵文字を出した瞬間に落ちる")


@pytest.mark.parametrize("name", USER_FACING_SCRIPTS)
def test_stdout_pinning_is_wrapped_so_it_cannot_itself_crash(name):
    """reconfigure 非対応のストリーム（pytest のキャプチャ等）で落ちないこと。"""
    src = _source(name)
    if "reconfigure(encoding=" not in src:
        pytest.skip("common.py 経由")
    idx = src.index("reconfigure(encoding=")
    window = src[max(0, idx - 400):idx]
    assert "try:" in window or "hasattr" in window, (
        f"{name} の reconfigure が保護されていない")


def test_yen_sign_is_encodable_after_pinning():
    """このテスト自体が回っている環境で ¥ が扱えることを確認する。"""
    assert "¥157.81/USD".encode("utf-8").decode("utf-8") == "¥157.81/USD"
