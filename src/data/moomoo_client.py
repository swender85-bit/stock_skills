"""moomoo (OpenD) client — 気配・指数クオートの補完ソース。

Finnhub フリー枠は指数クオートと日本株ニュースに非対応で、yahoo も
取得に失敗することがある。moomoo は日本株・指数・気配を持つので、
それらの**フォールバック**として使う。

## 動作の前提（重要）

moomoo の Python SDK は **OpenD ゲートウェイ**（moomoo が配布するデスクトップ
アプリ）がローカルで起動しログイン済みであることを要求する。SDK は
`localhost:11111` の OpenD に TCP 接続するだけで、このモジュールが
証券口座の資格情報を保持することはない。

そのため次のいずれかが欠けていれば :func:`is_available` は ``False`` を返し、
すべての取得関数は空を返す（graceful degradation）:

1. SDK 未インストール（``pip install moomoo-api`` もしくは ``futu-api``）
2. OpenD が起動していない / ポートに到達できない
3. ``MOOMOO_ENABLED`` が有効化されていない

**既定は無効（opt-in）。** 有効化するには ``MOOMOO_ENABLED=on``。
OpenD が落ちている環境で毎回 TCP 接続を試みて分析を遅くしないための既定値。

## 環境変数

| 変数 | 既定 | 意味 |
|:---|:---|:---|
| ``MOOMOO_ENABLED`` | ``off`` | ``on``/``1``/``true`` で有効化 |
| ``MOOMOO_OPEND_HOST`` | ``127.0.0.1`` | OpenD ホスト |
| ``MOOMOO_OPEND_PORT`` | ``11111`` | OpenD ポート |
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 11111
_PROBE_TIMEOUT = 1.0   # OpenD 到達確認の秒数（分析を止めないため短く）
_PROBE_TTL = 60        # 到達確認結果のキャッシュ秒数
_DEFAULT_STARTUP_TIMEOUT = 90.0  # 自動起動時にログイン完了（ポート開）を待つ上限秒

# ---------------------------------------------------------------------------
# 状態
# ---------------------------------------------------------------------------
_error_state: dict = {
    "status": "disabled",  # disabled | no_sdk | opend_unreachable | ok | other_error
    "message": "",
}
# 到達確認キャッシュ: (timestamp, reachable)
_probe_cache: Optional[tuple[float, bool]] = None


def get_error_status() -> dict:
    """現在の moomoo 接続状態を返す。"""
    return dict(_error_state)


def reset_state() -> None:
    """状態と到達確認キャッシュをリセット（主にテスト用）。"""
    global _probe_cache
    _probe_cache = None
    _error_state["status"] = "disabled"
    _error_state["message"] = ""


def _record(status: str, message: str = "") -> None:
    _error_state["status"] = status
    _error_state["message"] = message[:200]


# ---------------------------------------------------------------------------
# 可用性判定
# ---------------------------------------------------------------------------

def is_enabled() -> bool:
    """``MOOMOO_ENABLED`` で明示的に有効化されているか。"""
    return os.environ.get("MOOMOO_ENABLED", "").strip().lower() in ("on", "1", "true", "yes")


def _get_endpoint() -> tuple[str, int]:
    host = os.environ.get("MOOMOO_OPEND_HOST", "").strip() or _DEFAULT_HOST
    raw_port = os.environ.get("MOOMOO_OPEND_PORT", "").strip()
    try:
        port = int(raw_port) if raw_port else _DEFAULT_PORT
    except ValueError:
        port = _DEFAULT_PORT
    return host, port


def _import_sdk():
    """moomoo / futu SDK を import する。無ければ None。"""
    try:
        import moomoo as sdk  # type: ignore
        return sdk
    except ImportError:
        pass
    try:
        import futu as sdk  # type: ignore
        return sdk
    except ImportError:
        return None


def _opend_reachable() -> bool:
    """OpenD に TCP 接続できるか（結果は60秒キャッシュ）。"""
    global _probe_cache
    now = time.time()
    if _probe_cache is not None and (now - _probe_cache[0]) < _PROBE_TTL:
        return _probe_cache[1]

    host, port = _get_endpoint()
    try:
        with socket.create_connection((host, port), timeout=_PROBE_TIMEOUT):
            reachable = True
    except OSError:
        reachable = False

    _probe_cache = (now, reachable)
    return reachable


def is_available() -> bool:
    """有効化済み + OpenD 到達可能 + SDK あり のときだけ True。

    判定順は**コストの安い順**。SDK の import は実測 1.2 秒かかるので、
    OpenD が起動していないときにその代金を払わないよう、到達確認
    （1秒タイムアウト・60秒キャッシュ）を先に済ませる。
    """
    if not is_enabled():
        _record("disabled", "MOOMOO_ENABLED 未設定（既定で無効）")
        return False
    if not _opend_reachable():
        host, port = _get_endpoint()
        _record("opend_unreachable", f"OpenD ({host}:{port}) に接続できません")
        return False
    if _import_sdk() is None:
        _record("no_sdk", "moomoo-api / futu-api が未インストール")
        return False
    _record("ok")
    return True


# ---------------------------------------------------------------------------
# 無人ライフサイクル: OpenD の自動起動→終了 (ensure_opend)
# ---------------------------------------------------------------------------
#
# 週次レポートなどを **無人** で回すために、OpenD が起動していなければこの場で
# 起動し、処理後に「自分が起動したものだけ」終了させる。既にユーザーが OpenD を
# 開いている場合はそれを尊重し、勝手に落とさない。
#
# ヘッドレス自動ログインには OpenD.xml に本人の login_account + login_pwd_md5 が
# 必要（`scripts/set_moomoo_login.py` で1回設定）。デバイス認証は GUI ログイン時に
# 済んでいれば Device.dat に保存され、再ログイン時の SMS は不要になる。


def _opend_exe_path() -> Optional[Path]:
    """OpenD.exe のパス。``MOOMOO_OPEND_PATH`` 優先、無ければ既定 Desktop パス。"""
    raw = os.environ.get("MOOMOO_OPEND_PATH", "").strip()
    if raw:
        p = Path(raw)
        return p if p.is_file() else None
    default = (
        Path.home() / "Desktop" / "moomoo_OpenD_10.9.6918_Windows"
        / "moomoo_OpenD_10.9.6918_Windows" / "OpenD.exe"
    )
    return default if default.is_file() else None


def _wait_reachable(timeout: float) -> bool:
    """ポートが listening になる（＝ログイン成功）まで待つ。"""
    host, port = _get_endpoint()
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(2.0)
    return False


def _launch_opend(exe: Path):
    """OpenD.exe を起動して Popen を返す。失敗時 None。"""
    try:
        creationflags = 0
        if os.name == "nt":
            # コンソールを開かず、親のシグナルから切り離す
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | \
                getattr(subprocess, "DETACHED_PROCESS", 0)
        return subprocess.Popen(
            [str(exe)], cwd=str(exe.parent),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, creationflags=creationflags,
        )
    except Exception as e:
        _record("other_error", f"OpenD 起動失敗: {type(e).__name__}: {e}")
        return None


def _terminate_opend(proc) -> None:
    """自分で起動した OpenD をプロセスツリーごと終了する。"""
    if proc is None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
        else:
            proc.terminate()
        proc.wait(timeout=15)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


@contextmanager
def ensure_opend(autostart: bool = True):
    """OpenD が使える状態を保証するコンテキストマネージャ。

    yield する値は「この with ブロック内で OpenD が到達可能か」の bool。

    - 既に到達可能 → そのまま利用（**終了させない**）
    - 未到達 + ``autostart`` + 有効化済み + exe あり → 自動起動して待機、
      ブロック終了時に**自分が起動したものだけ**終了させる
    - 無効化 / exe 無し / 起動失敗 → ``False`` を yield（呼び出し側は graceful に）

    ``autostart=False`` なら起動は試みず、現在の到達性だけを返す。
    """
    global _probe_cache

    if not is_enabled():
        _record("disabled", "MOOMOO_ENABLED 未設定（既定で無効）")
        yield False
        return

    if _opend_reachable():
        yield True  # ユーザーが開いている等。落とさない。
        return

    if not autostart:
        yield False
        return

    exe = _opend_exe_path()
    if exe is None:
        _record("opend_unreachable",
                 "OpenD 未起動で自動起動もできません（MOOMOO_OPEND_PATH 未設定/不在）")
        yield False
        return

    proc = _launch_opend(exe)
    if proc is None:
        yield False
        return

    startup_timeout = _DEFAULT_STARTUP_TIMEOUT
    raw = os.environ.get("MOOMOO_OPEND_STARTUP_TIMEOUT", "").strip()
    if raw:
        try:
            startup_timeout = float(raw)
        except ValueError:
            pass

    reachable = _wait_reachable(startup_timeout)
    _probe_cache = None  # 起動したので到達性キャッシュを無効化
    if reachable:
        # コールドスタート直後は FedWatch/ARK/ニュース等の参照データが
        # まだ同期されておらず空を返す。ポートが開いてから少し待って
        # OpenD に同期の猶予を与える（自動起動時のみ）。
        warmup = 12.0
        raw_w = os.environ.get("MOOMOO_OPEND_WARMUP", "").strip()
        if raw_w:
            try:
                warmup = float(raw_w)
            except ValueError:
                pass
        if warmup > 0:
            time.sleep(warmup)
    try:
        if not reachable:
            _record("opend_unreachable",
                     f"OpenD を起動したが {startup_timeout:.0f}s 以内にログインしませんでした"
                     "（OpenD.xml の資格情報を確認）")
        yield reachable
    finally:
        _terminate_opend(proc)
        _probe_cache = None


# ---------------------------------------------------------------------------
# シンボル変換
# ---------------------------------------------------------------------------

# yahoo サフィックス -> moomoo 市場プレフィックス
_SUFFIX_TO_MARKET = {
    ".T": "JP",
    ".JP": "JP",
    ".HK": "HK",
    ".SS": "SH",
    ".SZ": "SZ",
    ".SI": "SG",
}

# 指数は moomoo 側のコード体系が別なので明示マップ。
#
# ⚠️ このマップは **実 OpenD 接続での検証ができていない**（OpenD デスクトップアプリの
# 起動が必要なため）。株式コードの変換規則と SDK の API 形状（RET_OK / カラム名 /
# OpenQuoteContext のシグネチャ）は moomoo-api 10.9 に対して検証済みだが、
# 指数コードそのものが正しいかは未確認。誤っていれば該当指数が取れないだけで
# （空が返る）、yahoo の一次ソースには影響しない。
_INDEX_MAP = {
    "^GSPC": "US.SPX",
    "^NDX": "US.NDX",
    "^SOX": "US.SOX",
    "^DJI": "US.DJI",
    "^VIX": "US.VIX",
    "^N225": "JP.800000",
    "^HSI": "HK.800000",
}


def to_moomoo_symbol(symbol: str) -> Optional[str]:
    """yahoo 形式のティッカーを moomoo 形式に変換。変換不能なら None。

    ``7203.T`` -> ``JP.7203`` / ``AAPL`` -> ``US.AAPL`` / ``^GSPC`` -> ``US.SPX``
    """
    if not symbol:
        return None
    s = symbol.strip().upper()

    if s in _INDEX_MAP:
        return _INDEX_MAP[s]
    if s.startswith("^"):
        return None  # 未知の指数を推測で組み立てない

    if "." in s:
        base, _, suffix = s.rpartition(".")
        market = _SUFFIX_TO_MARKET.get(f".{suffix}")
        if market is None or not base:
            return None
        return f"{market}.{base}"

    # サフィックスなし = 米国株
    return f"US.{s}"


# ---------------------------------------------------------------------------
# 取得
# ---------------------------------------------------------------------------

def get_quote(symbol: str) -> Optional[dict]:
    """現在値クオートを取得。取得不能なら None。

    返り値のスキーマは :func:`src.data.finnhub_client.get_quote` と揃える:
    ``{"current", "change", "percent_change", "high", "low", "prev_close"}``
    """
    quotes = get_quotes([symbol]) if symbol else {}
    return quotes.get(symbol)


def get_quotes(symbols: list[str]) -> dict[str, Optional[dict]]:
    """複数シンボルのクオートを一括取得。

    返り値は **yahoo 形式のシンボルをキー**にした dict。
    取得できなかったシンボルはキーごと省略する（None を混ぜて
    「取得したが値がゼロ」と誤読させない）。
    """
    if not symbols or not is_available():
        return {}

    sdk = _import_sdk()
    if sdk is None:
        return {}

    # yahoo -> moomoo の対応表（変換できたものだけ）
    mapping = {}
    for sym in symbols:
        mm = to_moomoo_symbol(sym)
        if mm:
            mapping[mm] = sym
    if not mapping:
        return {}

    host, port = _get_endpoint()
    ctx = None
    try:
        ctx = sdk.OpenQuoteContext(host=host, port=port)
        ret, data = ctx.get_market_snapshot(list(mapping.keys()))
        if ret != getattr(sdk, "RET_OK", 0):
            _record("other_error", str(data)[:200])
            return {}
        return _parse_snapshot(data, mapping)
    except Exception as e:  # SDK は多様な例外を投げる
        _record("other_error", f"{type(e).__name__}: {e}")
        return {}
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass


def _parse_snapshot(data, mapping: dict[str, str]) -> dict[str, Optional[dict]]:
    """OpenQuoteContext.get_market_snapshot の DataFrame を共通スキーマに変換。"""
    out: dict[str, Optional[dict]] = {}
    try:
        rows = data.to_dict("records")
    except AttributeError:
        rows = list(data) if data is not None else []

    for row in rows:
        code = row.get("code")
        yahoo_symbol = mapping.get(code)
        if yahoo_symbol is None:
            continue
        current = row.get("last_price")
        prev_close = row.get("prev_close_price")
        if current in (None, 0) and prev_close in (None, 0):
            continue  # 全ゼロ = 無効シンボル
        change = None
        percent_change = None
        if current is not None and prev_close:
            change = current - prev_close
            percent_change = change / prev_close * 100.0
        out[yahoo_symbol] = {
            "current": current,
            "change": change,
            "percent_change": percent_change,
            "high": row.get("high_price"),
            "low": row.get("low_price"),
            "prev_close": prev_close,
        }
    return out
