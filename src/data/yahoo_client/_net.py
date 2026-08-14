"""ネットワーク準備待ちとリトライ -- 取得の一時失敗を確定にしない.

## 名指しする問題（2026-08-08 に実害が出た）

週次レポートが**ほぼ全項目 null** で出力された。原因はレート制限でも
yfinance の仕様変更でもなく、**実行開始時点でネットワークが繋がっていなかった**こと。

パックの実測タイミングが決定的な証拠だった:

    prices_and_holdings   17.4s  → 価格 全滅・FX もフォールバック(157.81)
    competitors            0.4s  → null
    news                   1.1s  → 「取得できませんでした」
    moomoo                55.3s
    narrative            135.9s  → **成功**（finnhub 記事数が取れている）
    FANG+ technicals             → **成功**（RSI 68.1）

**前半が全滅し、後半が成功している。** 時間が経ってネットワークが復旧した形。

タスクは `WakeToRun=True` で PC をスタンバイから起こして 07:12 に起動する。
**起床直後は Wi-Fi の再接続が終わっておらず、最初の数十秒の通信が全部失敗する。**
そして `get_stock_info()` には**リトライが1回も無かった**ため、
その一瞬の失敗がそのまま「今週の価格は取得不能」として確定した。

## 設計

1. **取得の前にネットワークの準備を待つ**（`wait_for_network`）。
   起床直後の数十秒を待てば、以降は正常に取れる。
2. **一時失敗はリトライする**（`with_retry`）。指数バックオフ。
   ネットワーク不通・タイムアウト・レート制限は**待てば直る**種類の失敗。
3. **「データが無い」と「取れなかった」を区別する。**
   前者はリトライしても無駄（上場廃止・ETFに財務諸表が無い等）。
   後者だけ再試行する。混同すると、無駄な待ち時間か、諦めの早すぎる失敗になる。

**yahoo_client は全経路の唯一の入口**なので、ここを直せば週次レポートも
個別銘柄の問い合わせも同時に直る。
"""

from __future__ import annotations

import os
import socket
import time
from typing import Any, Callable, Optional

#: 既定のリトライ回数（初回 + 再試行）
DEFAULT_ATTEMPTS = int(os.environ.get("YF_RETRY_ATTEMPTS", "4"))
#: 初回の待ち秒数。以降 2倍ずつ伸ばす
DEFAULT_BACKOFF_SEC = float(os.environ.get("YF_RETRY_BACKOFF", "1.5"))
#: 待ちの上限
MAX_BACKOFF_SEC = 20.0

#: ネットワーク準備待ちの上限秒数。
#: スタンバイ復帰後の Wi-Fi 再接続は通常30秒以内に終わるが、余裕を持たせる。
NETWORK_WAIT_SEC = float(os.environ.get("YF_NETWORK_WAIT", "180"))

#: 疎通確認先。片方が落ちていても判定できるよう複数持つ。
PROBE_HOSTS = (("query1.finance.yahoo.com", 443), ("8.8.8.8", 53))

_network_ready = [False]
_warned = [False]


def _probe(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def network_available(timeout: float = 3.0) -> bool:
    """いまネットワークに出られるか。"""
    return any(_probe(h, p, timeout) for h, p in PROBE_HOSTS)


def wait_for_network(max_wait: Optional[float] = None, quiet: bool = False) -> dict:
    """ネットワークが繋がるまで待つ。

    **無人実行の入口で必ず呼ぶこと。** スタンバイから起こされた直後は
    Wi-Fi の再接続が終わっておらず、そのまま取得を始めると全滅する
    （2026-08-08 の週次がそれで壊れた）。

    Returns
    -------
    dict
        {"ready", "waited_sec", "attempts", "message"}
        `ready=False` でも呼び出し側は続行してよいが、**その場合の欠損は
        「データが無い」ではなく「取りに行けなかった」**として扱うこと。
    """
    limit = NETWORK_WAIT_SEC if max_wait is None else max_wait
    started = time.time()
    attempts = 0
    delay = 1.0

    while True:
        attempts += 1
        if network_available():
            waited = round(time.time() - started, 1)
            _network_ready[0] = True
            if waited > 2 and not quiet:
                print(f"[yahoo_client] ネットワーク復帰を確認しました（{waited}秒待機）")
            return {"ready": True, "waited_sec": waited, "attempts": attempts,
                    "message": f"ネットワーク疎通OK（{waited}秒）"}

        waited = time.time() - started
        if waited >= limit:
            msg = (f"ネットワークに {limit:.0f}秒 繋がりませんでした。"
                   "以降の取得失敗は**『データが無い』ではなく『取りに行けなかった』**です。")
            if not quiet:
                print(f"⚠️  [yahoo_client] {msg}")
            return {"ready": False, "waited_sec": round(waited, 1),
                    "attempts": attempts, "message": msg}

        if attempts == 1 and not quiet:
            print(f"[yahoo_client] ネットワーク未接続。最大 {limit:.0f}秒 待機します"
                  "（スタンバイ復帰直後の可能性）...")
        time.sleep(min(delay, limit - waited))
        delay = min(delay * 1.5, 10.0)


# ---------------------------------------------------------------------------
# リトライ
# ---------------------------------------------------------------------------

#: 待てば直る可能性がある失敗。ここに当たるものだけ再試行する。
_TRANSIENT_MARKERS = (
    "timed out", "timeout", "connection", "temporarily", "unreachable",
    "reset by peer", "429", "too many requests", "rate limit",
    "503", "502", "504", "gateway", "ssl", "handshake", "getaddrinfo",
    "name resolution", "max retries", "remote end closed",
)


def is_transient(exc: BaseException) -> bool:
    """待てば直る失敗か。

    上場廃止・シンボル不正のような**待っても直らない失敗**を再試行すると、
    無駄に時間を使ったうえで結局失敗する。区別する。
    """
    if isinstance(exc, (socket.timeout, TimeoutError, ConnectionError, OSError)):
        return True
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(m in text for m in _TRANSIENT_MARKERS)


def with_retry(
    fn: Callable[[], Any],
    *,
    label: str = "",
    attempts: int = DEFAULT_ATTEMPTS,
    backoff: float = DEFAULT_BACKOFF_SEC,
    is_empty: Optional[Callable[[Any], bool]] = None,
    quiet: bool = False,
) -> tuple[Any, Optional[str]]:
    """一時失敗をリトライして実行する。

    Parameters
    ----------
    is_empty : callable, optional
        戻り値が「空（取得できていない）」かを判定する。
        **例外が飛ばずに空が返るケースがある**（yfinance は繋がらないとき
        例外ではなく空の dict を返すことがある）。それも再試行対象にする。
        これを入れないと、2026-08-08 の失敗はリトライされない。

    Returns
    -------
    (result, error)
        `error` が None なら成功。失敗時は最後の理由を文字列で返す。
        **呼び出し側はこの理由を握り潰さず、利用者に見せること。**
    """
    last_error: Optional[str] = None
    delay = backoff

    for attempt in range(1, max(1, attempts) + 1):
        try:
            result = fn()
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if not is_transient(exc):
                return None, last_error          # 待っても直らない
        else:
            if is_empty is None or not is_empty(result):
                return result, None
            last_error = "空の応答（取得できていない）"

        if attempt >= attempts:
            break
        if not quiet and attempt == 1 and label:
            print(f"[yahoo_client] {label}: 取得に失敗、再試行します（{last_error}）")
        # 通信が落ちているなら、待つより復帰を確認したほうが早い
        if not network_available(timeout=2.0):
            wait_for_network(max_wait=min(60.0, NETWORK_WAIT_SEC), quiet=quiet)
        time.sleep(min(delay, MAX_BACKOFF_SEC))
        delay *= 2

    if label and not quiet:
        print(f"⚠️  [yahoo_client] {label}: {attempts}回試して取得できませんでした"
              f"（{last_error}）。**これは『データが無い』ではありません。**")
    return None, last_error
