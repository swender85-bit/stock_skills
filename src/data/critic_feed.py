"""X（Twitter）から批評家の発言を取得する -- 改善5 の入力経路.

## 何をするか

`config/critics.yaml` に登録したアカウントの直近発言を Grok の X Search で取得し、
**主張として採点できる形**に落とす。取得したものは `data/critics/<source_id>.json` に
`pending`（未検証）として積まれ、後で `scripts/score_critics.py` が採点する。

## 絶対に守ること

**「発言が無かった」と「取得できなかった」を混同しない。**

これを混ぜると、API キー未設定やレート制限が「今週この人は何も言わなかった」に
化ける。批評家の発言が無いことは情報だが、取れなかったことは情報ではない。
`available` フラグで必ず区別する（§16-1）。

## 系譜上の位置

ここで取れるものは全て `external_discourse`（外部言説・深度1）である。
**一次観測ではない。** 本人が開示原文を引用していても、又聞きは又聞きとして扱う
（系譜の偽装を許すと深度会計そのものが無意味になる）。
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

_CONFIG_PATH = "config/critics.yaml"


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def load_critics_config(path: Optional[str] = None) -> dict:
    """`config/critics.yaml` を読む。無ければ空（＝取得対象なし）。"""
    p = Path(path) if path else _repo_root() / _CONFIG_PATH
    if not p.exists():
        return {"meta": {}, "critics": []}
    try:
        import yaml

        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {"meta": {}, "critics": []}
    data.setdefault("meta", {})
    data.setdefault("critics", [])
    return data


def enabled_critics(config: Optional[dict] = None) -> list[dict]:
    cfg = config or load_critics_config()
    return [c for c in cfg.get("critics") or []
            if isinstance(c, dict) and c.get("enabled", True) and c.get("handle")]


# ---------------------------------------------------------------------------
# 取得
# ---------------------------------------------------------------------------


def _build_prompt(handle: str, days: int, limit: int) -> str:
    """特定アカウントの直近発言だけを拾わせる。

    要約させず**原文に近い形**で返させる。要約させると、その時点で
    自己推論（深度+1）が混入し、後から「本人が何と言ったか」を検証できなくなる。
    """
    return (
        f"Use X (Twitter) search to retrieve posts from the account @{handle} "
        f"published in the last {days} days. Return at most {limit} posts.\n\n"
        "For EACH post, return the ORIGINAL text (do not summarize, do not translate, "
        "do not paraphrase). Only include posts that contain a market view, a forecast, "
        "an assertion about a stock/sector/macro, or an analysis. "
        "Skip pure greetings, retweets without comment, and off-topic chatter.\n\n"
        "Respond with ONLY a JSON array, no prose:\n"
        "[\n"
        "  {\n"
        '    "posted_at": "YYYY-MM-DD",\n'
        '    "text": "<original post text, verbatim>",\n'
        '    "url": "<permalink to the post, or empty string>",\n'
        '    "symbols": ["<ticker mentioned, e.g. 7203.T or NVDA>"],\n'
        '    "topic": "<one short phrase describing what the post is about>"\n'
        "  }\n"
        "]\n\n"
        "If the account has no qualifying posts in the period, return an empty array []. "
        "If you cannot access the account at all, return exactly: "
        '{"error": "inaccessible"}'
    )


def fetch_recent_posts(
    handle: str,
    days: int = 7,
    limit: int = 20,
    timeout: int = 45,
    caller: Any = None,
) -> dict:
    """1アカウントの直近発言を取る。

    Returns
    -------
    dict
        {"handle", "available", "posts", "error", "fetched_at", "days"}

    **`available=False` は「発言が無かった」ではなく「取得できなかった」。**
    posts が空でも available=True なら「この期間に該当発言が無かった」であり、
    これは情報として意味がある。
    """
    from src.data.grok_client import _common

    out: dict[str, Any] = {
        "handle": handle,
        "days": days,
        "available": False,
        "posts": [],
        "error": None,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    if not _common.is_available():
        out["error"] = ("XAI_API_KEY が未設定のため取得できませんでした。"
                        "これは『発言が無かった』ではありません。")
        return out

    call = caller or _common._call_grok_api
    try:
        raw = call(_build_prompt(handle, days, limit), timeout=timeout)
    except Exception as exc:
        out["error"] = f"取得に失敗しました: {type(exc).__name__}: {exc}"
        return out

    if not raw or not str(raw).strip():
        # サーバが理由を返しているなら、それをそのまま見せる。
        # 「空の応答」だけだと、クレジット切れ（待っても直らない）と
        # 一時的な失敗（待てば直る）が同じ文面になり、対処を誤らせる。
        status = _common.get_error_status() or {}
        reason = status.get("message") or ""
        if status.get("status") == "no_credits":
            out["error"] = (f"Grok API に拒否されました（{reason}）。"
                            "console.x.ai でクレジットを購入してください。"
                            "**待っても直りません。**")
        elif reason:
            out["error"] = f"Grok から取得できませんでした（{reason}）。"
        else:
            out["error"] = "Grok から空の応答が返りました（取得できませんでした）。"
        return out

    parsed = _parse_posts(raw)
    if parsed is None:
        out["error"] = "応答を解釈できませんでした（取得できませんでした）。"
        return out
    if isinstance(parsed, dict) and parsed.get("error"):
        out["error"] = f"アカウントにアクセスできませんでした: {parsed['error']}"
        return out

    cutoff = date.today() - timedelta(days=days + 1)
    posts = []
    for row in parsed:
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        posted = _parse_date(row.get("posted_at"))
        if posted and posted < cutoff:
            continue
        posts.append({
            "posted_at": posted.isoformat() if posted else None,
            "text": text,
            "url": str(row.get("url") or "").strip(),
            "symbols": [str(s).strip() for s in (row.get("symbols") or []) if str(s).strip()],
            "topic": str(row.get("topic") or "").strip(),
        })

    out["available"] = True
    out["posts"] = posts[:limit]
    return out


def _parse_posts(raw: str) -> Any:
    """JSON 配列 / エラーオブジェクトを取り出す。前後の散文は捨てる。

    ⚠️ **エラーオブジェクトの判定を配列パースより先に行う。**
    汎用パーサは解釈できないとき空配列を返すことがあり、それを先に受けると
    「アクセスできなかった」が**「発言0件」に化ける**（実装中に踏んだ）。

    解釈できなければ `None` を返す。空配列（＝発言なし）と区別するため、
    ここで `[]` にフォールバックしてはいけない。
    """
    text = str(raw or "").strip()
    if not text:
        return None

    # コードフェンスを剥がす
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    # 1) そのまま JSON として読めるか（エラーオブジェクトはここで捕まる）
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed if parsed.get("error") else [parsed]
        if isinstance(parsed, list):
            return parsed
    except Exception:
        pass

    # 2) 散文に埋もれたエラーオブジェクトを先に探す
    obj = re.search(r"\{[^{}]*\"error\"[^{}]*\}", text, re.DOTALL)
    if obj:
        try:
            candidate = json.loads(obj.group(0))
            if isinstance(candidate, dict) and candidate.get("error"):
                return candidate
        except Exception:
            pass

    # 3) 散文に埋もれた JSON 配列を切り出す
    arr = re.search(r"\[.*\]", text, re.DOTALL)
    if arr:
        try:
            candidate = json.loads(arr.group(0))
            if isinstance(candidate, list):
                return candidate
        except Exception:
            return None
    return None


def _parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    text = str(value)[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def fetch_all(
    days: Optional[int] = None,
    config: Optional[dict] = None,
    caller: Any = None,
) -> dict:
    """登録済みアカウントを全部取る。

    1つ落ちても他は続ける。**全滅と一部失敗を区別できる形**で返す
    （全部落ちたのを「今週は静かだった」と読ませない）。
    """
    cfg = config or load_critics_config()
    meta = cfg.get("meta") or {}
    base_days = days or int(meta.get("default_days") or 7)
    limit = int(meta.get("max_posts_per_fetch") or 20)
    trust_days = int(meta.get("trust_default_days") or base_days)
    trust_mult = int(meta.get("trust_fetch_multiplier") or 1)

    results: dict[str, dict] = {}
    for critic in enabled_critics(cfg):
        # trust: primary は取りこぼしを減らす方向に倒す。
        # **取得量を増やすだけで、的中率には一切触れない。**
        primary = str(critic.get("trust") or "") == "primary"
        results[critic["source_id"]] = fetch_recent_posts(
            critic["handle"],
            days=(trust_days if (primary and days is None) else base_days),
            limit=(limit * trust_mult if primary else limit),
            caller=caller)
        results[critic["source_id"]]["trust"] = critic.get("trust")

    ok = [k for k, v in results.items() if v["available"]]
    failed = [k for k, v in results.items() if not v["available"]]
    total_posts = sum(len(v["posts"]) for v in results.values())

    if not results:
        summary = "取得対象のアカウントが登録されていません（config/critics.yaml）。"
    elif not ok:
        summary = (f"{len(failed)}アカウント全てで取得に失敗しました。"
                   "**これは『発言が無かった』ではありません。**")
    elif failed:
        summary = (f"{len(ok)}アカウントから {total_posts}件を取得。"
                   f"{len(failed)}アカウントは取得できませんでした（発言なしではありません）: "
                   + ", ".join(failed))
    else:
        summary = f"{len(ok)}アカウントから {total_posts}件を取得しました。"

    # 最重要情報源が落ちたときは、それを名指しする。
    # 他が取れていても「今週は材料が揃った」と読ませない。
    primary_ids = {c["source_id"] for c in enabled_critics(cfg)
                   if str(c.get("trust") or "") == "primary"}
    primary_failed = sorted(primary_ids & set(failed))
    if primary_failed:
        summary += (f" ⚠️ **最重要情報源 {', '.join(primary_failed)} が取得できていません。**"
                    "他が取れていても、材料が揃った週ではありません。")

    return {
        "results": results,
        "available_sources": ok,
        "failed_sources": failed,
        "primary_failed": primary_failed,
        "total_posts": total_posts,
        "days": base_days,
        "summary": summary,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def trust_map(config: Optional[dict] = None) -> dict[str, str]:
    """source_id → trust ティア。未指定は `standard`。"""
    return {c["source_id"]: str(c.get("trust") or "standard")
            for c in enabled_critics(config or load_critics_config())}
