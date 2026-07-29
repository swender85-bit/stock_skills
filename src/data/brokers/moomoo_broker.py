"""moomoo(OpenD) 由来の実在残高・約定履歴 (土曜設計書 提案1 / 提案5)。

## 権限と縮退について

実測で確定している制約（US LV3）: 日本株は権限なし、米指数・米オプションも不可。
したがって本モジュールの `scope` は **US のみ**である。照合エンジンは
scope 外（日本株・投信）について「口座に無い＝幽霊」と判定してはならない。

取引系 API (`OpenSecTradeContext`) は相場系と別物で、SDK未導入・OpenD未起動・
未ログイン・取引権限なしのいずれでも例外になる。**すべて握り潰して
`available=False` を返す**（設計書 第5章-7: 黙って古いデータを使わない）。

## 資格情報

取引パスワードは環境変数 `MOOMOO_TRADE_PWD_MD5` のみから読む。
ログ・レポート・Neo4j・コミットには一切出さない（設計書 第5章-6）。
ポジション照会は多くの環境で unlock 不要だが、必要な場合のみ使う。
"""

from __future__ import annotations

import os
from typing import Any, Optional

from src.data.brokers.base import make_position, make_snapshot

#: moomoo(US LV3) が実際にカバーする範囲。
SCOPE = ["US"]

_SOURCE = "moomoo"


def _trade_env(sdk):
    """本番/シミュレーションの選択。既定は本番（実在残高が目的のため）。"""
    raw = os.environ.get("MOOMOO_TRADE_ENV", "real").strip().lower()
    return sdk.TrdEnv.SIMULATE if raw in ("sim", "simulate", "paper") else sdk.TrdEnv.REAL


def _open_trade_ctx(sdk):
    """米国株の取引コンテキストを開く。失敗時は例外を投げる。"""
    from src.data.moomoo_client import _get_endpoint  # 内部だが同一パッケージの設定源

    host, port = _get_endpoint()
    ctx_cls = getattr(sdk, "OpenSecTradeContext", None)
    if ctx_cls is None:
        raise RuntimeError("SDK に OpenSecTradeContext がありません")
    try:
        return ctx_cls(filter_trdmarket=sdk.TrdMarket.US, host=host, port=port,
                       security_firm=getattr(sdk, "SecurityFirm").FUTUINC)
    except Exception:
        # 旧SDK / security_firm 不要な版へのフォールバック
        return ctx_cls(host=host, port=port)


def _unlock_if_needed(sdk, ctx) -> None:
    pwd_md5 = os.environ.get("MOOMOO_TRADE_PWD_MD5", "").strip()
    if not pwd_md5:
        return
    try:
        ctx.unlock_trade(password_md5=pwd_md5)
    except Exception:
        pass  # unlock 不要な口座もある。失敗しても照会は試す。


def _rows(ret_and_data) -> list[dict]:
    """SDK の (ret, data) タプルを list[dict] に均す。"""
    try:
        ret, data = ret_and_data
    except Exception:
        return []
    if ret != 0:
        raise RuntimeError(str(data)[:200])
    if hasattr(data, "to_dict"):
        return data.to_dict("records")
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    return []


def _to_position(r: dict) -> dict:
    code = str(r.get("code") or "")
    symbol = code.split(".")[-1] if code.startswith("US.") else code or None
    return make_position(
        symbol,
        r.get("qty"),
        name=r.get("stock_name"),
        account=None,  # moomoo は特定/NISA の日本の口座区分を持たない
        cost_price=r.get("cost_price") or r.get("average_cost"),
        currency=r.get("currency") or "USD",
        market_value=r.get("market_val"),
        market="US",
        raw={"pl_val": r.get("pl_val"), "pl_ratio": r.get("pl_ratio"),
             "can_sell_qty": r.get("can_sell_qty"), "code": code},
    )


def fetch(autostart: bool = True) -> dict:
    """moomoo の米国株実在残高を返す。取れなければ available=False。"""
    from src.data import moomoo_client as mc

    if not mc.is_enabled():
        return make_snapshot(_SOURCE, available=False, scope=SCOPE,
                             error="MOOMOO_ENABLED 未設定（既定で無効）")

    try:
        # ensure_opend は contextmanager。ブロックを抜けると
        # 「自分が起動した OpenD だけ」が終了する。
        with mc.ensure_opend(autostart=autostart) as reachable:
            if not reachable:
                return make_snapshot(
                    _SOURCE, available=False, scope=SCOPE,
                    error="OpenD に接続できません（未起動/未ログイン）")
            sdk = mc._import_sdk()
            if sdk is None:
                return make_snapshot(_SOURCE, available=False, scope=SCOPE,
                                     error="moomoo-api / futu-api が未インストール")
            return _fetch_with_sdk(sdk)
    except Exception as e:
        return make_snapshot(_SOURCE, available=False, scope=SCOPE,
                             error=f"{type(e).__name__}: {str(e)[:160]}")


def _fetch_with_sdk(sdk) -> dict:
    ctx = None
    try:
        ctx = _open_trade_ctx(sdk)
        _unlock_if_needed(sdk, ctx)
        env = _trade_env(sdk)
        positions = [_to_position(r)
                     for r in _rows(ctx.position_list_query(trd_env=env))]
        cash = _fetch_cash(ctx, env)
        return make_snapshot(
            _SOURCE, available=True, positions=positions, cash=cash,
            scope=SCOPE, max_age_hours=24.0, detail={"trade_env": str(env)},
        )
    except Exception as e:
        return make_snapshot(_SOURCE, available=False, scope=SCOPE,
                             error=f"取引照会に失敗: {type(e).__name__}: {str(e)[:140]}")
    finally:
        try:
            if ctx is not None:
                ctx.close()
        except Exception:
            pass


def _fetch_cash(ctx, env) -> list[dict]:
    try:
        rows = _rows(ctx.accinfo_query(trd_env=env, currency=None))
    except Exception:
        return []
    out = []
    for r in rows:
        amount = r.get("cash") if isinstance(r.get("cash"), (int, float)) else r.get("power")
        if amount is None:
            continue
        out.append({"currency": (r.get("currency") or "USD"), "amount": float(amount),
                    "name": "moomoo 現金", "purpose": None})
    return out


def fetch_executions(days: int = 90) -> dict:
    """約定履歴（提案5 執行監査の入力）。取れなければ available=False。

    OpenD の履歴照会は口座権限に依存するため、失敗を正常系として扱う。
    """
    from datetime import date, timedelta

    from src.data import moomoo_client as mc

    if not mc.is_enabled():
        return {"available": False, "source": _SOURCE, "executions": [],
                "error": "MOOMOO_ENABLED 未設定"}

    start = (date.today() - timedelta(days=days)).isoformat()
    end = date.today().isoformat()

    try:
        with mc.ensure_opend(autostart=True) as reachable:
            if not reachable:
                return {"available": False, "source": _SOURCE, "executions": [],
                        "error": "OpenD 未接続"}
            sdk = mc._import_sdk()
            if sdk is None:
                return {"available": False, "source": _SOURCE, "executions": [],
                        "error": "SDK 未導入"}
            ctx = None
            try:
                ctx = _open_trade_ctx(sdk)
                _unlock_if_needed(sdk, ctx)
                env = _trade_env(sdk)
                rows = _rows(ctx.history_deal_list_query(
                    start=start, end=end, trd_env=env))
                execs = [{
                    "symbol": str(r.get("code") or "").split(".")[-1] or None,
                    "side": str(r.get("trd_side") or "").upper(),
                    "shares": r.get("qty"),
                    "price": r.get("price"),
                    "executed_at": r.get("create_time"),
                    "source": _SOURCE,
                } for r in rows]
                return {"available": True, "source": _SOURCE, "executions": execs,
                        "window_days": days, "error": None}
            finally:
                try:
                    if ctx is not None:
                        ctx.close()
                except Exception:
                    pass
    except Exception as e:
        return {"available": False, "source": _SOURCE, "executions": [],
                "error": f"{type(e).__name__}: {str(e)[:160]}"}
