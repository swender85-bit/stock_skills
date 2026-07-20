"""src/output/verify.py のユニットテスト（upgrade v1.0 Phase 2）。"""
import pytest

from src.output.verify import verify_report

pytestmark = pytest.mark.smoke


def test_missing_file(tmp_path):
    r = verify_report(str(tmp_path / "nope.md"))
    assert r["ok"] is False
    assert r["checks"]["exists"] is False


def test_empty_file(tmp_path):
    p = tmp_path / "empty.md"
    p.write_text("", encoding="utf-8")
    r = verify_report(str(p))
    assert r["ok"] is False
    assert r["checks"]["exists"] is True
    assert r["checks"]["non_empty"] is False


def test_no_structure(tmp_path):
    p = tmp_path / "plain.md"
    p.write_text("見出しもfrontmatterも無い本文", encoding="utf-8")
    r = verify_report(str(p))
    assert r["ok"] is False
    assert r["checks"]["markdown"] is False


def test_ok_with_heading(tmp_path):
    p = tmp_path / "h.md"
    p.write_text("# タイトル\n本文", encoding="utf-8")
    r = verify_report(str(p))
    assert r["ok"] is True


def test_ok_frontmatter_warns_without_tags(tmp_path):
    p = tmp_path / "fm.md"
    p.write_text("---\ntitle: x\n---\n# 本文", encoding="utf-8")
    r = verify_report(str(p))
    assert r["ok"] is True
    joined = " ".join(r["warnings"])
    assert "tags" in joined
    assert "created" in joined


def test_ok_full_obsidian_frontmatter(tmp_path):
    p = tmp_path / "full.md"
    p.write_text(
        "---\ntitle: x\ntags: [investing]\ncreated: 2026-07-18\n---\n# 本文",
        encoding="utf-8",
    )
    r = verify_report(str(p))
    assert r["ok"] is True
    assert r["warnings"] == []


def test_non_markdown_ok(tmp_path):
    p = tmp_path / "data.csv"
    p.write_text("a,b\n1,2", encoding="utf-8")
    r = verify_report(str(p))
    assert r["ok"] is True
