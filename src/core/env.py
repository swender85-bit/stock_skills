"""`.env` の読み込みを一箇所に集約する。

## なぜこれがあるか

2026-08-11 の点検で判明した実害:

    $ python -c "from src.data import edgar_client; import os; \
                 print(bool(os.environ.get('SEC_EDGAR_UA')))"
    False

`SEC_EDGAR_UA` は `.env` に**設定済み**だった。にもかかわらず
`edgar_client` からは見えていなかった。理由は、`load_dotenv()` を
呼んでいたのが `finnhub_client` / `grok_client` / `moomoo_client` /
`graph_store.linker` の4つだけで、`edgar_client` と `edinet_client` は
呼んでいなかったから。

`load_dotenv()` は `os.environ` を書き換えるので、**たまたま先に
finnhub を import したプロセスでは EDGAR も動き、しなければ動かない**。
インポート順という、コードのどこにも書かれていない条件で挙動が変わっていた。

結果として **一次観測（SEC EDGAR）は「設定済み」のまま一度も繋がっておらず**、
レポートには「開示を取得できませんでした」とだけ出続けていた。
これは `config/thresholds.yaml` が一度も読めていなかったのと同じ形である
（設定した本人にも気づけない沈黙の失敗）。

## 使い方

資格情報を読むモジュールは、環境変数に触る前にこれを呼ぶ::

    from src.core.env import load_env
    load_env()

何度呼んでも安全（1回だけ実行される）。**既存の環境変数を上書きしない**ので、
シェルで明示的に渡した値が `.env` に負けることはない。
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parents[2] / ".env"

_LOADED = False
#: 読み込みに失敗した理由。None なら成功、または .env が無い（正常）。
LOAD_ERROR: str | None = None


def load_env(force: bool = False) -> bool:
    """プロジェクト直下の `.env` を環境変数へ読み込む。

    Returns
    -------
    bool
        読み込みを実行したか（既に読み込み済みなら False）。
    """
    global _LOADED, LOAD_ERROR
    if _LOADED and not force:
        return False
    try:
        from dotenv import load_dotenv

        load_dotenv(ENV_PATH, override=False)
        LOAD_ERROR = None
    except ImportError:
        # python-dotenv が無くても動く。最小の実装で肩代わりする。
        try:
            _load_manually()
            LOAD_ERROR = None
        except Exception as exc:
            LOAD_ERROR = f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        LOAD_ERROR = f"{type(exc).__name__}: {exc}"
    _LOADED = True
    return True


def _load_manually() -> None:
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def status() -> dict:
    """`.env` を読めたか。呼び出し側が『設定したのに効かない』に気づけるように。"""
    load_env()
    return {"path": str(ENV_PATH), "exists": ENV_PATH.exists(),
            "loaded": LOAD_ERROR is None, "error": LOAD_ERROR}
