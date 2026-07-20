"""ガードフック（scripts/hooks/guard_protected.py）のユニットテスト。

保護領域への破壊的操作が exit code 2 でブロックされ、
通常操作は exit 0 で素通りすることを、実際の stdin→exit の契約で検証する。
"""
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD = REPO_ROOT / "scripts" / "hooks" / "guard_protected.py"


def _run(payload: dict) -> int:
    proc = subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return proc.returncode


# --- ブロックされるべきケース（exit 2） ---

def test_block_edit_portfolio_master():
    code = _run({"tool_name": "Edit", "tool_input": {
        "file_path": ".claude/skills/stock-portfolio/data/portfolio.csv"}})
    assert code == 2


def test_block_edit_notes():
    code = _run({"tool_name": "Edit", "tool_input": {
        "file_path": "data/notes/foo.json"}})
    assert code == 2


def test_block_rm_rf_data():
    code = _run({"tool_name": "Bash", "tool_input": {
        "command": "rm -rf data/notes"}})
    assert code == 2


def test_block_rm_rf_vault():
    code = _run({"tool_name": "Bash", "tool_input": {
        "command": "rm -rf /c/Users/swend/iCloudDrive/swender/x"}})
    assert code == 2


def test_block_overwrite_existing_env(tmp_path):
    env = tmp_path / ".env"
    env.write_text("SECRET=1", encoding="utf-8")
    code = _run({"tool_name": "Write", "tool_input": {"file_path": str(env)}})
    assert code == 2


# --- 素通りすべきケース（exit 0） ---

def test_allow_edit_source():
    code = _run({"tool_name": "Edit", "tool_input": {
        "file_path": "src/core/common.py"}})
    assert code == 0


def test_allow_new_env_write():
    # まだ存在しない .env への Write は新規作成扱いで許可（追記型運用）
    code = _run({"tool_name": "Write", "tool_input": {
        "file_path": "does_not_exist_dir/.env"}})
    assert code == 0


def test_allow_normal_bash():
    code = _run({"tool_name": "Bash", "tool_input": {"command": "ls -la"}})
    assert code == 0


def test_allow_rm_rf_nonprotected():
    code = _run({"tool_name": "Bash", "tool_input": {"command": "rm -rf build/"}})
    assert code == 0


def test_allow_rm_in_appdata_not_false_positive():
    # AppData/ の中の "data/" を保護対象と誤検知しないこと（過剰ブロック回帰）
    cmd = "rm -f /c/Users/swend/AppData/Local/Temp/scratchpad/output/x.md"
    code = _run({"tool_name": "Bash", "tool_input": {"command": cmd}})
    assert code == 0


def test_block_rm_history_dir():
    code = _run({"tool_name": "Bash", "tool_input": {
        "command": "rm -rf data/history/trade"}})
    assert code == 2


def test_allow_stderr_redirect_reading_data(tmp_path):
    # `2>/dev/null` は stderr抑制であって破壊ではない。data/ の読取と共起しても許可
    cmd = "ls src/data/graph_query/ 2>/dev/null && grep x src/data/note_manager.py"
    code = _run({"tool_name": "Bash", "tool_input": {"command": cmd}})
    assert code == 0


def test_allow_redirect_to_devnull():
    code = _run({"tool_name": "Bash", "tool_input": {
        "command": "python scripts/run.py > /dev/null"}})
    assert code == 0


def test_block_overwrite_redirect_into_portfolio():
    # 単一 > で portfolio.csv を上書き（truncate）するのはブロック
    code = _run({"tool_name": "Bash", "tool_input": {
        "command": "echo bad > .claude/skills/stock-portfolio/data/portfolio.csv"}})
    assert code == 2


def test_allow_append_redirect_to_log():
    # >> 追記や非保護先への > は許可
    code = _run({"tool_name": "Bash", "tool_input": {
        "command": "echo hi >> build/out.log"}})
    assert code == 0
