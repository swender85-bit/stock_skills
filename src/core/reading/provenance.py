"""provenance のドメインベース判定（読書台帳仕様 v2 第2部 2-4）。

🔴 **本文の自己申告を採用しない。** URL のホスト部とパスだけで決める。

「これは公式資料です」と書いてある個人ブログを一次資料にすると、
深度0の錨として扱われ、系譜監査そのものが無意味になる。
逆方向の誤り（一次資料を個人発信と誤る）は、不当に軽視されるだけで済む。

したがって **不明なものは最も深い側（個人発信・深度2）に倒す。**
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from src.core.reading.schema import BOOK, OWN, PERSONAL, PRESS, PRIMARY, VENDOR

_CONFIG = Path(__file__).resolve().parents[3] / "config" / "provenance_rules.yaml"

_KEY_TO_PROVENANCE = {
    "primary": PRIMARY,
    "vendor": VENDOR,
    "press": PRESS,
    "personal": PERSONAL,
}

#: 判定の優先順。一次資料を最初に見る（IR パスは他ドメインにも現れるため）。
_ORDER = ("primary", "vendor", "press", "personal")


@lru_cache(maxsize=1)
def rules() -> dict:
    try:
        import yaml

        return yaml.safe_load(_CONFIG.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _matches_domain(host: str, domains: list) -> bool:
    for d in domains or []:
        d = str(d).lower().strip()
        if not d:
            continue
        if host == d or host.endswith("." + d):
            return True
    return False


def classify(
    *,
    source_url: Optional[str] = None,
    source_type: str = "text",
    body: Optional[str] = None,
) -> dict:
    """provenance を判定する。

    Returns
    -------
    dict
        `{"provenance", "reason", "matched"}`
    """
    cfg = rules()

    # ローカル作成（URL 無し・テキスト）は自分の考え
    if not source_url and source_type in ("text", "audio_memo"):
        return {"provenance": OWN, "reason": "URL の無いローカル作成物", "matched": "local"}

    # PDF で ISBN 等を含めば書籍
    if source_type == "pdf" and body:
        markers = (cfg.get("book") or {}).get("markers") or []
        for m in markers:
            if str(m) in body:
                return {"provenance": BOOK,
                        "reason": f"PDF 本文に書籍の標識『{m}』を検出",
                        "matched": "book"}

    host = _host(source_url or "")
    path = ""
    try:
        path = (urlparse(source_url or "").path or "").lower()
    except Exception:
        path = ""

    for key in _ORDER:
        block = cfg.get(key) or {}
        if host and _matches_domain(host, block.get("domains") or []):
            return {"provenance": _KEY_TO_PROVENANCE[key],
                    "reason": f"ドメイン {host} が {key} 規則に一致",
                    "matched": host}
        for pat in block.get("path_patterns") or []:
            if str(pat).lower() in path:
                return {"provenance": _KEY_TO_PROVENANCE[key],
                        "reason": f"パス『{pat}』が {key} 規則に一致",
                        "matched": pat}

    default = cfg.get("default") or PERSONAL
    return {"provenance": default,
            "reason": ("いずれの規則にも一致しないため、最も保守的な既定"
                       "（原典から最も遠い側）に倒しました。"
                       "**これは『信用できない』ではなく『出所を確認していない』です。**"),
            "matched": None}
