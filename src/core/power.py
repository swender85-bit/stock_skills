"""実行中にスリープへ戻らせない -- 蓋を閉じたスタンバイ運用の前提条件.

## 名指しする問題

「PC はスタンバイで蓋を閉じたまま、週次レポートが自動で出る」という前提で
`WakeToRun=True` のタスクを組んだ。**だが実行中にスリープへ戻らない保証を
一切していなかった。**

システムログの実測（2026-08-09）:

    15:13:59  システムがスリープ状態になります
    15:14:03  システムがスリープ状態から再開されました   ← 4秒
    12:13:58  スリープ →  12:14:02  再開
     9:14:04  再開

3時間ごとの `WeeklyDeepResume` が起こしている。Resume は即終了するので
4秒で妥当だが、**本番の週次は5〜10分かかる**。

Windows は「ユーザー不在で起床したタスク」の完了後すぐスリープへ戻る。
実行中でも、プロセスが **system-required を宣言していなければ**
アイドル判定でスリープに戻りうる。ネットワーク復帰を180秒待っている最中に
スリープへ戻れば、待った意味がない。

さらに悪いことに、**スリープに戻った瞬間ネットワークが切れる**ので、
2026-08-08 に観測した「前半全滅・後半成功」と同じ形が再現する。

## 何をするか

`SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)` を宣言し、
処理が終わるまでシステムスリープを抑止する。**画面は点けない**
（`ES_DISPLAY_REQUIRED` を付けない）ので、蓋を閉じたまま暗いまま動く。

    with keep_awake("週次レポート"):
        ...          # この間スリープに戻らない
    # 抜けると抑止解除。Windows は通常どおりスリープへ戻れる

## 非Windows / 失敗時

何もしない（`active=False` を返す）。**抑止できなかったことを黙って
「抑止した」と扱わない。** 呼び出し側はログに残す。
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from typing import Optional

#: SetThreadExecutionState のフラグ
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
ES_AWAYMODE_REQUIRED = 0x00000040


def is_windows() -> bool:
    return os.name == "nt"


def _set_state(flags: int) -> bool:
    """SetThreadExecutionState を呼ぶ。成功なら True。"""
    if not is_windows():
        return False
    try:
        import ctypes

        result = ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(flags))
        return result != 0
    except Exception:
        return False


def prevent_sleep(display: bool = False, away_mode: bool = False) -> dict:
    """処理が終わるまでシステムスリープを抑止する。

    Parameters
    ----------
    display : bool
        画面も点けたままにするか。**既定は False。**
        蓋を閉じた運用では画面を点ける必要がなく、点けると発熱とバッテリー消費が増える。
    away_mode : bool
        アウェイモード（見た目はスリープだが処理は続く）。
        対応していないシステムでは無視される。

    Returns
    -------
    dict
        {"active", "flags", "reason"}
        **`active=False` を「抑止できた」と扱わない。**
    """
    if not is_windows():
        return {"active": False, "flags": 0,
                "reason": "Windows ではないためスリープ抑止をスキップしました"}

    flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    if display:
        flags |= ES_DISPLAY_REQUIRED
    if away_mode:
        flags |= ES_AWAYMODE_REQUIRED

    if _set_state(flags):
        return {"active": True, "flags": flags,
                "reason": "スリープ抑止を有効化しました（画面は点けません）"}

    # アウェイモード非対応で失敗することがあるので、無しで再試行する
    if away_mode:
        fallback = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | (
            ES_DISPLAY_REQUIRED if display else 0)
        if _set_state(fallback):
            return {"active": True, "flags": fallback,
                    "reason": "アウェイモード非対応のため通常の抑止で有効化しました"}

    return {"active": False, "flags": 0,
            "reason": "SetThreadExecutionState に失敗しました。"
                      "**実行中にスリープへ戻る可能性があります。**"}


def allow_sleep() -> bool:
    """抑止を解除し、通常のスリープ動作に戻す。

    **必ず呼ぶこと。** 解除し忘れるとPCがスリープしなくなる。
    """
    return _set_state(ES_CONTINUOUS)


@contextmanager
def keep_awake(label: str = "", display: bool = False, quiet: bool = False):
    """処理中スリープに戻らせないコンテキスト。

        with keep_awake("週次レポート"):
            ...

    例外が出ても必ず解除する。**解除漏れでPCが眠れなくなる方が実害が大きい。**
    """
    state = prevent_sleep(display=display)
    if not quiet:
        mark = "🔒" if state["active"] else "⚠️"
        prefix = f"[power] {label}: " if label else "[power] "
        print(f"{mark} {prefix}{state['reason']}", file=sys.stderr, flush=True)
    try:
        yield state
    finally:
        allow_sleep()
        if not quiet and state["active"]:
            print(f"[power] {label or '処理'}: スリープ抑止を解除しました",
                  file=sys.stderr, flush=True)


def power_status() -> dict:
    """いまスリープ抑止が効くかを診断する（無人実行前の自己点検用）。"""
    if not is_windows():
        return {"supported": False, "reason": "Windows ではありません"}
    probe = prevent_sleep()
    allow_sleep()
    return {
        "supported": probe["active"],
        "reason": probe["reason"],
        "note": ("蓋を閉じたスタンバイ運用では、これが有効でないと"
                 "実行の途中でスリープへ戻り、ネットワークが切れます。"),
    }
