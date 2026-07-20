"""src/output/sync.py のユニットテスト（upgrade v1.0 Phase 2）。

実際の Obsidian vault には触れず、tmp_path を vault/output に見立てて検証する。
"""
import pytest

from src.output.sync import save_and_sync, _versioned_path, load_output_config

pytestmark = pytest.mark.smoke


def test_save_only_when_no_vault(tmp_path):
    out = tmp_path / "output"
    r = save_and_sync("# レポート\n本文", "test_20260718.md",
                      output_dir=str(out), vault_path="")
    assert r["degraded"] is True
    assert r["synced_path"] is None
    assert (out / "test_20260718.md").is_file()
    assert r["verify"]["ok"] is True


def test_save_and_sync_to_vault(tmp_path):
    out = tmp_path / "output"
    vault = tmp_path / "vault"
    vault.mkdir()
    r = save_and_sync("# レポート\n本文", "toyota_20260718.md",
                      output_dir=str(out), vault_path=str(vault))
    assert r["degraded"] is False
    assert r["synced_path"] is not None
    assert (vault / "toyota_20260718.md").is_file()
    assert r["verify"]["ok"] is True


def test_nonexistent_vault_degrades(tmp_path):
    out = tmp_path / "output"
    r = save_and_sync("# x\ny", "a.md",
                      output_dir=str(out), vault_path=str(tmp_path / "missing"))
    assert r["degraded"] is True
    assert r["synced_path"] is None


def test_no_overwrite_versions(tmp_path):
    out = tmp_path / "output"
    vault = tmp_path / "vault"
    vault.mkdir()
    # 既存ファイルを置く
    (vault / "dup.md").write_text("既存", encoding="utf-8")
    r = save_and_sync("# 新規\n本文", "dup.md",
                      output_dir=str(out), vault_path=str(vault))
    # 既存は上書きされず _v2 として保存
    assert (vault / "dup.md").read_text(encoding="utf-8") == "既存"
    assert r["synced_path"].endswith("dup_v2.md")
    assert (vault / "dup_v2.md").is_file()


def test_versioned_path_helper(tmp_path):
    base = tmp_path / "f.md"
    assert _versioned_path(str(base)) == str(base)  # 存在しなければそのまま
    base.write_text("x", encoding="utf-8")
    assert _versioned_path(str(base)).endswith("f_v2.md")


def test_load_config_defaults_when_missing(tmp_path):
    cfg = load_output_config(str(tmp_path / "no_such.yaml"))
    assert cfg["local_output_dir"] == "output"
    assert cfg["obsidian_vault_path"] == ""
