"""レポートの保存＆Obsidian同期（upgrade v1.0 Phase 2）

分析結果の Markdown を「output/ に生成 → Obsidian vault へコピー → 検証」まで
一気通貫で行う。既存ファイルは上書きせず _v2, _v3 と連番で退避する（非破壊）。

vault パス未設定 or 実在しない場合は output/ のみで完了する（graceful degradation）。

使い方（ライブラリ）:
    from src.output.sync import save_and_sync
    result = save_and_sync(markdown_text, "7203T_分析_20260718.md")
    print(result["output_path"], result["synced_path"], result["verify"]["ok"])
"""
from __future__ import annotations

import os
import shutil
from typing import Any, Dict, Optional

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

from .verify import verify_report

_DEFAULT_CONFIG_REL = os.path.join("config", "output.yaml")


def _repo_root() -> str:
    # src/output/sync.py → repo ルートは2つ上
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def load_output_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """config/output.yaml を読む。無ければデフォルト。"""
    if config_path is None:
        config_path = os.path.join(_repo_root(), _DEFAULT_CONFIG_REL)
    cfg: Dict[str, Any] = {
        "obsidian_vault_path": "",
        "icloud_path": "",
        "local_output_dir": "output",
        "on_conflict": "version",
    }
    if yaml is None or not os.path.isfile(config_path):
        return cfg
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
        if isinstance(loaded, dict):
            cfg.update({k: v for k, v in loaded.items() if v is not None})
    except Exception:
        pass
    return cfg


def _versioned_path(dest: str) -> str:
    """dest が既に存在する場合、_v2, _v3 ... を付けて衝突しないパスを返す。"""
    if not os.path.exists(dest):
        return dest
    root, ext = os.path.splitext(dest)
    n = 2
    while True:
        candidate = f"{root}_v{n}{ext}"
        if not os.path.exists(candidate):
            return candidate
        n += 1


def save_and_sync(
    content: str,
    filename: str,
    *,
    output_dir: Optional[str] = None,
    vault_path: Optional[str] = None,
    config_path: Optional[str] = None,
) -> Dict[str, Any]:
    """content を output/ に保存し、vault へコピーして検証する。

    Args:
        content: 保存する Markdown 本文
        filename: ファイル名（例: 7203T_分析_20260718.md）
        output_dir: 作業出力ディレクトリ（省略時 config or output/）
        vault_path: 同期先 vault（省略時 config の obsidian_vault_path）
        config_path: config/output.yaml のパス（テスト用）

    Returns:
        {
          "output_path": str,      # output/ に保存した実パス
          "synced_path": str|None, # vault にコピーした実パス（同期時のみ）
          "degraded": bool,        # vault 未設定/不在で output のみだったか
          "verify": {...},         # verify_report の結果（同期先 or output）
          "messages": [str, ...],
        }
    """
    cfg = load_output_config(config_path)
    root = _repo_root()

    if output_dir is None:
        output_dir = cfg.get("local_output_dir") or "output"
    if not os.path.isabs(output_dir):
        output_dir = os.path.join(root, output_dir)

    if vault_path is None:
        vault_path = cfg.get("obsidian_vault_path") or ""

    messages = []

    # 1) output/ へ保存
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, filename)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)
    messages.append(f"output に保存: {output_path}")

    # 2) vault へ同期（非破壊）
    synced_path = None
    degraded = True
    if vault_path and os.path.isdir(vault_path):
        dest = os.path.join(vault_path, filename)
        if cfg.get("on_conflict") == "skip" and os.path.exists(dest):
            messages.append(f"同名ファイルが存在するためスキップ: {dest}")
        else:
            dest = _versioned_path(dest)
            shutil.copy2(output_path, dest)
            synced_path = dest
            degraded = False
            messages.append(f"Obsidian vault へコピー: {dest}")
    elif vault_path:
        messages.append(f"vault パスが存在しません（output のみで完了）: {vault_path}")
    else:
        messages.append("vault パス未設定（output のみで完了）")

    # 3) 検証（同期先があればそちら、無ければ output）
    verify_target = synced_path or output_path
    verify = verify_report(verify_target)

    return {
        "output_path": output_path,
        "synced_path": synced_path,
        "degraded": degraded,
        "verify": verify,
        "messages": messages,
    }
