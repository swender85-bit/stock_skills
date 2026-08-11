"""Centralised threshold loader (KIK-446).

Reads ``config/thresholds.yaml`` once and exposes a simple accessor:

    from src.core._thresholds import th
    value = th("health", "rsi_drop_threshold", 40)

If the YAML file is missing or unreadable the accessor falls back to the
caller-supplied default, so existing behaviour is always preserved.
"""

import yaml
from pathlib import Path

_THRESHOLDS: dict | None = None

#: 読み込みに失敗した理由。None なら成功。**握り潰さずここに残す。**
LOAD_ERROR: str | None = None

_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "thresholds.yaml"


def get_thresholds() -> dict:
    """Return the full thresholds dict, loading from disk on first call.

    🔴 2026-08-11 の修理まで、この関数は Windows 上で**一度も成功していなかった**。

    ``open(p)`` が既定エンコーディング（cp932）でファイルを開くため、
    日本語コメントを含む UTF-8 の YAML が ``UnicodeDecodeError`` になり、
    それを ``except Exception`` が握り潰して空 dict を返していた。
    結果として ``th()`` の呼び出しは**すべて呼び出し側の既定値**に沈黙で落ちており、
    長期金利ゲート（``rates.ust30y_warning``）を含む全閾値の設定ファイルが
    事実上ハードコードだった。

    「設定を読めなかった」が「設定が無い」に化けていた。これは今回の修理が
    対象とした根本原因（失敗が情報ではなく穴になる）と同じ形である。

    既定値へのフォールバックは残す（設定ファイルが無くても動くべき）。
    ただし**失敗したことは ``LOAD_ERROR`` に残す**。
    """
    global _THRESHOLDS, LOAD_ERROR
    if _THRESHOLDS is None:
        try:
            with open(_PATH, encoding="utf-8") as f:
                _THRESHOLDS = yaml.safe_load(f) or {}
            LOAD_ERROR = None
        except FileNotFoundError:
            _THRESHOLDS = {}
            LOAD_ERROR = f"設定ファイルがありません: {_PATH}"
        except Exception as exc:
            _THRESHOLDS = {}
            LOAD_ERROR = f"{type(exc).__name__}: {exc}"
    return _THRESHOLDS


def load_status() -> dict:
    """閾値ファイルを読めたか。呼び出し側が『既定値で動いている』と気づけるように。"""
    get_thresholds()
    return {"path": str(_PATH), "loaded": LOAD_ERROR is None,
            "error": LOAD_ERROR,
            "sections": sorted((_THRESHOLDS or {}).keys())}


def th(section: str, key: str, default):
    """Look up *section.key* in thresholds, returning *default* on miss."""
    return get_thresholds().get(section, {}).get(key, default)
