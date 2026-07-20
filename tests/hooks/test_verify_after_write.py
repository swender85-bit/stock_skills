"""出力検証フック（scripts/hooks/verify_after_write.py）のユニットテスト。

output/ 配下の .md を対象に、空ファイル・体裁不備を exit 2 で警告し、
対象外や体裁OKは exit 0 で素通りすることを検証する。
"""
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFY = REPO_ROOT / "scripts" / "hooks" / "verify_after_write.py"


def _run(payload: dict) -> int:
    proc = subprocess.run(
        [sys.executable, str(VERIFY)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return proc.returncode


def test_warn_empty_md(tmp_path):
    # output/ を含むパスにするため output ディレクトリを作る
    out = tmp_path / "output"
    out.mkdir()
    md = out / "empty.md"
    md.write_text("", encoding="utf-8")
    assert _run({"tool_name": "Write", "tool_input": {"file_path": str(md)}}) == 2


def test_warn_no_structure_md(tmp_path):
    out = tmp_path / "output"
    out.mkdir()
    md = out / "plain.md"
    md.write_text("ただの本文で見出しもfrontmatterも無い", encoding="utf-8")
    assert _run({"tool_name": "Write", "tool_input": {"file_path": str(md)}}) == 2


def test_ok_md_with_frontmatter(tmp_path):
    out = tmp_path / "output"
    out.mkdir()
    md = out / "ok.md"
    md.write_text("---\ntitle: x\n---\n# 見出し\n本文", encoding="utf-8")
    assert _run({"tool_name": "Write", "tool_input": {"file_path": str(md)}}) == 0


def test_skip_non_target(tmp_path):
    # output/ でも vault でもないパスは検証対象外（空でも素通り）
    other = tmp_path / "src.md"
    other.write_text("", encoding="utf-8")
    assert _run({"tool_name": "Write", "tool_input": {"file_path": str(other)}}) == 0


def test_skip_non_write_tool(tmp_path):
    assert _run({"tool_name": "Bash", "tool_input": {"command": "ls"}}) == 0
