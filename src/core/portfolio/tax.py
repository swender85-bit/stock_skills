"""手取り翻訳層 — 判断単位を税引後に変える (土曜設計書 提案3)。

## 名指しする問題

Stock Skills の全ての判断は税引前・手数料前・為替スプレッド前で行われている。
日本の個人投資家にとって、これは算術的に誤った土俵である。

> **「Aを売ってBに乗り換える」という提案は、含み益がある限り、必ず
> 20.315%のハンデを背負う。**

含み益100万円のポジションを売れば約20万円が消え、乗り換え先は残り80万円で走る。
B は A を **25%以上**上回って初めて損益分岐する。現行システムはこの計算を
していないので、**乗り換え提案が構造的に過剰**になっている。

これは個人投資家の最大のリターン毀損要因（過剰売買）への直接の対策であり、
本設計書の中で金額インパクトが最も大きい可能性が高い。

## 二つの巨大な盲点

**(a) 含み損は資産である。** 税制上、含み損は将来の利益と相殺できる権利であり
金銭的価値を持つ。ただし本モジュールはそれを**売却推奨には使わない**（下記）。

**(b) NISA枠は希少資源の配分問題である。** 年間の非課税枠は有限で、
使い残しは繰り越せない。

## 税を使う方向の非対称（重要な設計上の制約）

設計書 提案3-⑧:

> 税は**判断を却下する側にのみ使い、推奨する側には使わない**。

つまり「税引後で損だから、この乗り換えはやめる」は言ってよい。
「節税になるから売る」は**言ってはならない**（テールワグズドッグ）。
`loss_harvest_value()` は情報提供専用であり、売却推奨を生成しない。

## ハードコード禁止

税率・NISA枠・手数料は全て `config/tax.yaml` から読む。
grep で税率のハードコードが無いことを検証する（受け入れ基準5）。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

DEFAULT_CONFIG_PATH = "config/tax.yaml"

#: 設定ファイルが読めないときの最終手段。**ここも「唯一の真実」ではない**。
#: 設定が無いことを黙って正常扱いしないため、必ず warning を返す。
_FALLBACK = {
    "meta": {"verified_as_of": None, "stale_warning_days": 400},
    "capital_gains": {"rate": 0.20315, "loss_carryforward_years": 3},
    "dividends": {"domestic_rate": 0.20315, "us_withholding_rate": 0.10,
                  "assume_foreign_tax_credit": False},
    "accounts": {"_default": {"kind": "taxable", "label": "不明"}},
    "nisa": {"annual_limits": {"growth": 2400000, "tsumitate": 1200000},
             "expiry_warning_weeks": 26},
    "fees": {},
}

_cache: dict[str, Any] = {}


def load_tax_config(path: str = DEFAULT_CONFIG_PATH, use_cache: bool = True) -> dict:
    """税制設定を読む。読めなければフォールバックし、必ず警告を添える。"""
    if use_cache and path in _cache:
        return _cache[path]

    warnings: list[str] = []
    try:
        import yaml

        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(path)
        cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as e:
        cfg = dict(_FALLBACK)
        warnings.append(
            f"税制設定 {path} を読めませんでした（{type(e).__name__}）。"
            "内蔵の暫定値を使っています。税額は信用しないでください。")

    warnings.extend(_config_warnings(cfg))
    cfg = {**cfg, "_warnings": warnings}
    if use_cache:
        _cache[path] = cfg
    return cfg


def _config_warnings(cfg: dict) -> list[str]:
    out: list[str] = []
    meta = cfg.get("meta") or {}
    verified = meta.get("verified_as_of")
    if not verified:
        out.append(
            "税制設定がまだ人の目で確認されていません（config/tax.yaml の "
            "meta.verified_as_of が空）。税率・NISA枠・手数料を確認して日付を入れてください。")
        return out
    try:
        d = datetime.strptime(str(verified)[:10], "%Y-%m-%d").date()
    except ValueError:
        out.append(f"meta.verified_as_of の日付を解釈できません: {verified!r}")
        return out
    age = (date.today() - d).days
    limit = int(meta.get("stale_warning_days") or 400)
    if age > limit:
        out.append(f"税制設定の確認から {age}日 経過しています。"
                   "税制は毎年変わります。config/tax.yaml を見直してください。")
    return out


def reset_cache() -> None:
    _cache.clear()


# ---------------------------------------------------------------------------
# 口座区分
# ---------------------------------------------------------------------------


def account_kind(account: Optional[str], cfg: Optional[dict] = None) -> dict:
    """口座名から課税区分を引く。未知の口座は**課税口座として扱う**（保守側）。"""
    cfg = cfg or load_tax_config()
    accounts = cfg.get("accounts") or {}
    raw = (account or "").strip()
    entry = accounts.get(raw)
    if entry is None:
        # 「NISA成長投資枠」のような表記揺れを拾う
        for key, val in accounts.items():
            if key.startswith("_"):
                continue
            if raw and (raw.startswith(key) or key.startswith(raw)):
                entry = val
                break
    if entry is None:
        entry = accounts.get("_default") or {"kind": "taxable", "label": "不明"}
    return {"kind": entry.get("kind", "taxable"),
            "label": entry.get("label", raw or "不明"),
            "nisa_bucket": entry.get("nisa_bucket"),
            "tax_free": entry.get("kind") == "tax_free",
            "account": raw or None}


def is_tax_free(account: Optional[str], cfg: Optional[dict] = None) -> bool:
    return account_kind(account, cfg)["tax_free"]


# ---------------------------------------------------------------------------
# 譲渡益課税
# ---------------------------------------------------------------------------


def capital_gains_tax(gain: Optional[float], account: Optional[str] = None,
                      cfg: Optional[dict] = None) -> dict:
    """譲渡益にかかる税額。

    NISA は非課税。ただし**損失も通算できない**ので、損失側では
    `offsettable=False` を返す（暴落時の下方非対称・設計書 提案6-①(c)）。
    """
    cfg = cfg or load_tax_config()
    rate = float((cfg.get("capital_gains") or {}).get("rate", 0.20315))
    kind = account_kind(account, cfg)

    if gain is None:
        return {"tax": None, "rate": rate, "account": kind, "offsettable": None,
                "reason": "譲渡損益が不明で税額を計算できません"}

    if kind["tax_free"]:
        return {
            "tax": 0.0, "rate": 0.0, "account": kind,
            # ここが NISA の本質的な非対称。利益は無税だが損失は救済されない。
            "offsettable": False,
            "reason": ("非課税口座のため譲渡益に課税されません。"
                       "ただし損失が出ても損益通算・繰越控除はできません。"),
        }

    if gain <= 0:
        return {"tax": 0.0, "rate": rate, "account": kind, "offsettable": True,
                "reason": "譲渡損のため課税されません（同年の利益と相殺できます）"}

    return {"tax": gain * rate, "rate": rate, "account": kind, "offsettable": True,
            "reason": f"譲渡益 {gain:,.0f}円 × {rate:.5%}"}


# ---------------------------------------------------------------------------
# 取得ロット（部分売却の税額を正確にする）
# ---------------------------------------------------------------------------


def sell_proceeds(
    shares_sold: float,
    price: float,
    cost_price: float,
    account: Optional[str] = None,
    *,
    fx_rate: float = 1.0,
    currency: str = "JPY",
    cfg: Optional[dict] = None,
) -> dict:
    """売却の手取りを計算する（税・手数料・為替スプレッド控除後）。

    全ての金額は円建てで返す。
    """
    cfg = cfg or load_tax_config()
    gross_local = shares_sold * price
    cost_local = shares_sold * cost_price
    gain_local = gross_local - cost_local

    fee_local = trading_fee(gross_local, currency, cfg)
    fx_cost = fx_spread_cost(gross_local, currency, cfg) if currency != "JPY" else 0.0

    gross_jpy = gross_local * fx_rate
    gain_jpy = gain_local * fx_rate
    fee_jpy = fee_local * fx_rate
    fx_cost_jpy = fx_cost * fx_rate

    tax = capital_gains_tax(gain_jpy - fee_jpy - fx_cost_jpy, account, cfg)
    tax_jpy = tax["tax"] or 0.0

    return {
        "gross_jpy": gross_jpy,
        "cost_jpy": cost_local * fx_rate,
        "gain_before_tax_jpy": gain_jpy,
        "fee_jpy": fee_jpy,
        "fx_cost_jpy": fx_cost_jpy,
        "tax_jpy": tax_jpy,
        "net_jpy": gross_jpy - fee_jpy - fx_cost_jpy - tax_jpy,
        "account": tax["account"],
        "tax_detail": tax,
        "disclaimer": "概算です。税務助言ではありません。",
    }


def trading_fee(gross_local: float, currency: str, cfg: Optional[dict] = None) -> float:
    cfg = cfg or load_tax_config()
    fees = cfg.get("fees") or {}
    table = fees.get("us_stock" if (currency or "JPY").upper() != "JPY"
                     else "jp_stock") or {}
    fee = gross_local * float(table.get("rate", 0.0)) + float(table.get("fixed", 0.0))
    if table.get("min") is not None:
        fee = max(fee, float(table["min"]))
    if table.get("max") is not None:
        fee = min(fee, float(table["max"]))
    return fee


def fx_spread_cost(gross_local: float, currency: str,
                   cfg: Optional[dict] = None) -> float:
    """為替スプレッドの実額（現地通貨建て）。片道分。

    往復コストは売買手数料より大きいことが多く、これを落とすと
    乗り換え判定はほぼ必ず楽観側に外れる。
    """
    cfg = cfg or load_tax_config()
    if (currency or "JPY").upper() == "JPY":
        return 0.0
    spread = float(((cfg.get("fees") or {}).get("fx") or {})
                   .get("usdjpy_spread_per_usd", 0.0))
    # 円建てのスプレッド額を現地通貨に戻すため、片道分を USD 単位で掛ける
    return 0.0 if spread <= 0 else gross_local * (spread / 150.0)


# ---------------------------------------------------------------------------
# 乗り換え損益分岐（提案3の中核）
# ---------------------------------------------------------------------------


def switching_hurdle(
    shares: float,
    price: float,
    cost_price: float,
    account: Optional[str] = None,
    *,
    fx_rate: float = 1.0,
    currency: str = "JPY",
    buy_fee_rate: Optional[float] = None,
    cfg: Optional[dict] = None,
) -> dict:
    """乗り換え先が上回らなければならない率（%）。

    A を売って B を買うとき、税・手数料で目減りした資金で B が走る。
    B が A と同じ額に戻るには、その目減り分を取り返す必要がある。

        hurdle = 売却総額 / 再投資可能額 − 1

    含み益が大きいほどハードルは高くなる。**これを添えない乗り換え提案は
    構造的に過剰である。**
    """
    cfg = cfg or load_tax_config()
    proceeds = sell_proceeds(shares, price, cost_price, account,
                             fx_rate=fx_rate, currency=currency, cfg=cfg)

    gross = proceeds["gross_jpy"]
    net = proceeds["net_jpy"]

    # 買い直しの手数料も、B が取り返さなければならない
    if buy_fee_rate is None:
        table = (cfg.get("fees") or {}).get(
            "us_stock" if (currency or "JPY").upper() != "JPY" else "jp_stock") or {}
        buy_fee_rate = float(table.get("rate", 0.0))
    reinvestable = net * (1.0 - buy_fee_rate)

    if gross <= 0 or reinvestable <= 0:
        return {"available": False, "hurdle_pct": None,
                "reason": "売却額が計算できません", **proceeds}

    hurdle = (gross / reinvestable - 1.0) * 100.0
    return {
        "available": True,
        "hurdle_pct": round(hurdle, 2),
        "gross_jpy": gross,
        "net_jpy": net,
        "reinvestable_jpy": reinvestable,
        "friction_jpy": gross - reinvestable,
        "tax_jpy": proceeds["tax_jpy"],
        "fee_jpy": proceeds["fee_jpy"],
        "fx_cost_jpy": proceeds["fx_cost_jpy"],
        "account": proceeds["account"],
        "message": (
            f"この乗り換えで {gross - reinvestable:,.0f}円 が税・手数料・為替で消えます。"
            f"乗り換え先は現保有を **{hurdle:.1f}%** 上回って初めて損益分岐します。"),
        "disclaimer": "概算です。税務助言ではありません。",
    }


def evaluate_switch(expected_edge_pct: Optional[float], hurdle: dict) -> dict:
    """期待優位とハードルを突き合わせ、**却下すべきかどうか**を返す。

    税は判断を却下する側にのみ使う。ここで `recommend=True` は返さない。
    """
    if not hurdle.get("available") or expected_edge_pct is None:
        return {"verdict": "unknown", "reject": False,
                "message": "期待優位または損益分岐が計算できず、税引後の判定ができません。"}

    h = hurdle["hurdle_pct"]
    if expected_edge_pct < h:
        return {
            "verdict": "reject", "reject": True,
            "expected_edge_pct": expected_edge_pct, "hurdle_pct": h,
            "message": (f"⚠️ 期待優位 {expected_edge_pct:.1f}% < 損益分岐 {h:.1f}%。"
                        "税引後で改善しないため、この乗り換えは推奨しません。"),
        }
    return {
        "verdict": "passes_tax_check", "reject": False,
        "expected_edge_pct": expected_edge_pct, "hurdle_pct": h,
        "message": (f"期待優位 {expected_edge_pct:.1f}% は損益分岐 {h:.1f}% を上回ります。"
                    "税務上の障害はありませんが、これは買い推奨ではありません。"),
    }


# ---------------------------------------------------------------------------
# 含み損の税務価値（情報提供のみ）
# ---------------------------------------------------------------------------


def loss_harvest_value(unrealized_loss_jpy: Optional[float],
                       realized_gain_ytd_jpy: Optional[float],
                       account: Optional[str] = None,
                       cfg: Optional[dict] = None) -> dict:
    """含み損を実現した場合の節税額。**売却推奨は生成しない。**

    設計書 提案3-⑧: 税は判断を却下する側にのみ使い、推奨する側には使わない。
    ここは常に「情報提供」であり、`recommendation` は返さない。
    """
    cfg = cfg or load_tax_config()
    kind = account_kind(account, cfg)

    if kind["tax_free"]:
        return {
            "available": False, "value_jpy": 0.0, "account": kind,
            "message": ("非課税口座の損失は損益通算できません。"
                        "この含み損には税務上の救済がありません。"),
        }

    if not unrealized_loss_jpy or unrealized_loss_jpy >= 0:
        return {"available": False, "value_jpy": 0.0, "account": kind,
                "message": "含み損がありません。"}

    rate = float((cfg.get("capital_gains") or {}).get("rate", 0.20315))
    years = int((cfg.get("capital_gains") or {}).get("loss_carryforward_years", 3))
    offset = min(abs(unrealized_loss_jpy), max(realized_gain_ytd_jpy or 0.0, 0.0))

    return {
        "available": True,
        "unrealized_loss_jpy": unrealized_loss_jpy,
        "offsettable_jpy": offset,
        "value_jpy": offset * rate,
        "carryforward_years": years,
        "account": kind,
        "message": (
            f"当年の実現益 {realized_gain_ytd_jpy or 0:,.0f}円と相殺した場合、"
            f"約 {offset * rate:,.0f}円 の税が消えます"
            f"（相殺しきれない分は{years}年繰り越せます）。"),
        # この一文を消してはいけない。消すと節税が売却推奨に化ける。
        "caveat": ("これは損切りの推奨ではありません。保有し続ける理由があるなら、"
                   "税効果だけを理由に売るべきではありません。"),
        "recommendation": None,
    }


# ---------------------------------------------------------------------------
# NISA 枠
# ---------------------------------------------------------------------------


def nisa_state(used_growth_jpy: float = 0.0, used_tsumitate_jpy: float = 0.0,
               today: Optional[date] = None, cfg: Optional[dict] = None) -> dict:
    """NISA の残枠と、年内に消滅する見込み。

    使い残しは繰り越せない。つまり毎年、静かに損失が発生している。
    """
    cfg = cfg or load_tax_config()
    nisa = cfg.get("nisa") or {}
    limits = nisa.get("annual_limits") or {}
    today = today or date.today()

    weeks_left = max(0, (date(today.year, 12, 31) - today).days) // 7
    buckets = {}
    for key, used in (("growth", used_growth_jpy), ("tsumitate", used_tsumitate_jpy)):
        limit = float(limits.get(key) or 0.0)
        remaining = max(0.0, limit - float(used or 0.0))
        buckets[key] = {
            "limit_jpy": limit,
            "used_jpy": float(used or 0.0),
            "remaining_jpy": remaining,
            "used_pct": round(float(used or 0.0) / limit * 100.0, 1) if limit else None,
        }

    total_remaining = sum(b["remaining_jpy"] for b in buckets.values())
    warn_weeks = int(nisa.get("expiry_warning_weeks") or 26)

    message = None
    if total_remaining > 0 and weeks_left <= warn_weeks:
        message = (f"年内残り{weeks_left}週。現ペースだと約 {total_remaining:,.0f}円 の"
                   "非課税枠が未使用のまま消滅します（枠は翌年に繰り越せません）。")

    return {
        "buckets": buckets,
        "total_remaining_jpy": total_remaining,
        "weeks_left_in_year": weeks_left,
        "message": message,
        "disclaimer": "概算です。実際の使用額は証券会社の記録を確認してください。",
    }


def nisa_suitability(expected_return_pct: Optional[float],
                     dividend_yield_pct: Optional[float],
                     holding_years: Optional[float],
                     cfg: Optional[dict] = None) -> dict:
    """NISA枠を与える価値の高さ。

    期待リターンが高く、保有期間が長く、配当が多いほど非課税の恩恵が大きい。
    逆に、短期売買予定の銘柄に枠を使うのは資源の浪費である。
    """
    parts: list[str] = []
    score = 0.0

    if isinstance(expected_return_pct, (int, float)):
        score += max(0.0, min(expected_return_pct, 30.0)) / 30.0 * 40.0
        parts.append(f"期待リターン {expected_return_pct:.1f}%")
    if isinstance(dividend_yield_pct, (int, float)):
        score += max(0.0, min(dividend_yield_pct, 6.0)) / 6.0 * 30.0
        parts.append(f"配当利回り {dividend_yield_pct:.1f}%")
    if isinstance(holding_years, (int, float)):
        score += max(0.0, min(holding_years, 10.0)) / 10.0 * 30.0
        parts.append(f"想定保有 {holding_years:.0f}年")

    if not parts:
        return {"available": False, "score": None,
                "message": "NISA枠適性を判定する材料（期待リターン・配当・保有期間）がありません。"}

    label = "高" if score >= 60 else ("中" if score >= 35 else "低")
    return {
        "available": True, "score": round(score, 1), "label": label,
        "basis": parts,
        "message": (f"NISA枠適性: {label}（{score:.0f}点） — " + " / ".join(parts)
                    + "。短期売買予定の銘柄に枠を使うのは資源の浪費です。"),
    }
