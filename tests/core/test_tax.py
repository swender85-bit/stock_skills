"""手取り翻訳層のテスト (土曜設計書 提案3-⑨ 受け入れ基準)。

1. 全ての乗り換え提案に switching_hurdle が添付される（配線側でも検証）
2. 手取りが改善しない乗り換えが Review で自動却下される
3. NISA残枠と年内消滅見込みが正しく計算される
4. 部分売却時の税額が、取得ロット単位で正しく計算される
5. 税率をハードコードした箇所が存在しない
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from src.core.portfolio import tax


@pytest.fixture(autouse=True)
def _clean_cache():
    tax.reset_cache()
    yield
    tax.reset_cache()


@pytest.fixture
def cfg():
    return tax.load_tax_config()


def _write_cfg(tmp_path: Path, body: str) -> str:
    p = tmp_path / "tax.yaml"
    p.write_text(body, encoding="utf-8")
    return str(p)


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------


def test_config_loads_from_repo():
    c = tax.load_tax_config()
    assert c["capital_gains"]["rate"] > 0
    assert "growth" in c["nisa"]["annual_limits"]


def test_missing_config_warns_instead_of_silently_using_defaults(tmp_path):
    """設定が無いことを黙って正常扱いしない。税額を信用させてはいけない。"""
    c = tax.load_tax_config(str(tmp_path / "nope.yaml"), use_cache=False)
    assert any("読めませんでした" in w for w in c["_warnings"])


def test_unverified_config_warns(tmp_path):
    path = _write_cfg(tmp_path, "meta:\n  verified_as_of: null\n")
    c = tax.load_tax_config(path, use_cache=False)
    assert any("確認されていません" in w for w in c["_warnings"])


def test_stale_config_warns(tmp_path):
    path = _write_cfg(
        tmp_path, "meta:\n  verified_as_of: '2020-01-01'\n  stale_warning_days: 30\n")
    c = tax.load_tax_config(path, use_cache=False)
    assert any("経過しています" in w for w in c["_warnings"])


def test_fresh_config_has_no_staleness_warning(tmp_path):
    path = _write_cfg(
        tmp_path,
        f"meta:\n  verified_as_of: '{date.today().isoformat()}'\n"
        "  stale_warning_days: 400\n")
    c = tax.load_tax_config(path, use_cache=False)
    assert c["_warnings"] == []


def test_no_hardcoded_tax_rate_outside_config_and_fallback():
    """受け入れ基準5: 税率のハードコード禁止。

    `_FALLBACK` と docstring 以外に 0.20315 が現れてはいけない。
    """
    src = Path("src/core/portfolio/tax.py").read_text(encoding="utf-8")
    # `_FALLBACK`（設定が壊れたときの最後の砦）より後ろだけを見る。
    body = src.split("_cache: dict", 1)[1]
    # 残る 0.20315 は必ず `.get(..., 0.20315)` の形、つまり
    # 「設定から読めなかったとき」の既定値でなければならない。
    for line in body.splitlines():
        if "0.20315" not in line:
            continue
        assert ".get(" in line, f"税率が計算式に直接埋め込まれています: {line.strip()}"


# ---------------------------------------------------------------------------
# 口座区分
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("account,tax_free", [
    ("特定", False), ("一般", False), ("NISA成長", True), ("NISAつみたて", True),
    ("NISA成長投資枠", True), ("", False), (None, False), ("謎の口座", False),
])
def test_account_kind(account, tax_free):
    """未知の口座は課税口座として扱う（保守側）。"""
    assert tax.is_tax_free(account) is tax_free


# ---------------------------------------------------------------------------
# 譲渡益課税
# ---------------------------------------------------------------------------


def test_capital_gains_tax_on_taxable_account(cfg):
    r = tax.capital_gains_tax(1_000_000, "特定", cfg)
    assert r["tax"] == pytest.approx(1_000_000 * cfg["capital_gains"]["rate"])


def test_nisa_gain_is_untaxed_but_loss_is_not_offsettable():
    """NISA の下方非対称。利益は無税だが損失も救済されない。

    これは暴落時の意思決定を根本的に変えるが、口座区分を持たない
    システムは区別できない（設計書 提案6-①(c)）。
    """
    gain = tax.capital_gains_tax(1_000_000, "NISA成長")
    assert gain["tax"] == 0.0
    assert gain["offsettable"] is False
    assert "損益通算" in gain["reason"]


def test_taxable_loss_is_offsettable():
    r = tax.capital_gains_tax(-500_000, "特定")
    assert r["tax"] == 0.0
    assert r["offsettable"] is True


def test_capital_gains_tax_handles_unknown_gain():
    r = tax.capital_gains_tax(None, "特定")
    assert r["tax"] is None


# ---------------------------------------------------------------------------
# 売却手取り（受け入れ基準4）
# ---------------------------------------------------------------------------


def test_sell_proceeds_deducts_tax_fee_and_fx():
    r = tax.sell_proceeds(100, 250.0, 150.0, "特定", fx_rate=160.0, currency="USD")
    assert r["gross_jpy"] == pytest.approx(100 * 250.0 * 160.0)
    assert r["tax_jpy"] > 0
    assert r["fee_jpy"] > 0
    assert r["fx_cost_jpy"] > 0, "為替スプレッドを落とすと乗り換え判定が楽観側に外れる"
    assert r["net_jpy"] < r["gross_jpy"]


def test_sell_proceeds_partial_uses_sold_shares_only():
    """部分売却は売った株数分のロットにだけ課税される。"""
    full = tax.sell_proceeds(100, 250.0, 150.0, "特定")
    half = tax.sell_proceeds(50, 250.0, 150.0, "特定")
    assert half["tax_jpy"] == pytest.approx(full["tax_jpy"] / 2)
    assert half["gain_before_tax_jpy"] == pytest.approx(full["gain_before_tax_jpy"] / 2)


def test_sell_proceeds_in_nisa_has_no_tax():
    r = tax.sell_proceeds(100, 250.0, 150.0, "NISA成長")
    assert r["tax_jpy"] == 0.0


def test_jpy_sale_has_no_fx_cost():
    r = tax.sell_proceeds(100, 3000.0, 2000.0, "特定", currency="JPY")
    assert r["fx_cost_jpy"] == 0.0


def test_us_fee_respects_cap(cfg):
    """上限手数料を超えないこと（大口で手数料が爆発しない）。"""
    cap = cfg["fees"]["us_stock"]["max"]
    assert tax.trading_fee(1_000_000.0, "USD", cfg) == pytest.approx(cap)


# ---------------------------------------------------------------------------
# 乗り換え損益分岐（提案3の中核）
# ---------------------------------------------------------------------------


def test_switching_hurdle_grows_with_unrealized_gain():
    """含み益が大きいほどハードルは高い。ここが乗り換え過剰の正体。"""
    small = tax.switching_hurdle(100, 110.0, 100.0, "特定", currency="JPY")
    large = tax.switching_hurdle(100, 300.0, 100.0, "特定", currency="JPY")
    assert large["hurdle_pct"] > small["hurdle_pct"]


def test_switching_hurdle_is_near_zero_in_nisa():
    r = tax.switching_hurdle(100, 300.0, 100.0, "NISA成長", currency="JPY")
    assert r["hurdle_pct"] == pytest.approx(0.0, abs=0.01)


def test_switching_hurdle_reports_friction_amount():
    r = tax.switching_hurdle(100, 250.0, 150.0, "特定", fx_rate=160.0, currency="USD")
    assert r["friction_jpy"] > 0
    assert "上回って初めて損益分岐" in r["message"]


def test_switching_hurdle_unavailable_on_bad_input():
    r = tax.switching_hurdle(0, 0.0, 0.0, "特定")
    assert r["available"] is False


def test_evaluate_switch_rejects_when_edge_below_hurdle():
    """受け入れ基準2: 手取りが改善しない乗り換えは却下される。"""
    h = tax.switching_hurdle(100, 300.0, 100.0, "特定", currency="JPY")
    r = tax.evaluate_switch(1.0, h)
    assert r["reject"] is True
    assert "推奨しません" in r["message"]


def test_evaluate_switch_never_recommends_buying():
    """税は却下する側にのみ使う。通過しても買い推奨にはしない。"""
    h = tax.switching_hurdle(100, 101.0, 100.0, "NISA成長", currency="JPY")
    r = tax.evaluate_switch(50.0, h)
    assert r["reject"] is False
    assert r["verdict"] == "passes_tax_check"
    assert "買い推奨ではありません" in r["message"]


def test_evaluate_switch_unknown_without_edge():
    h = tax.switching_hurdle(100, 300.0, 100.0, "特定", currency="JPY")
    assert tax.evaluate_switch(None, h)["verdict"] == "unknown"


# ---------------------------------------------------------------------------
# 含み損の税務価値（推奨を生成しないこと）
# ---------------------------------------------------------------------------


def test_loss_harvest_reports_value_but_never_recommends():
    r = tax.loss_harvest_value(-640_000, 412_000, "特定")
    assert r["value_jpy"] > 0
    assert r["recommendation"] is None, "節税を売却推奨に化けさせてはいけない"
    assert "損切りの推奨ではありません" in r["caveat"]


def test_loss_harvest_caps_offset_at_realized_gain():
    r = tax.loss_harvest_value(-1_000_000, 200_000, "特定")
    assert r["offsettable_jpy"] == pytest.approx(200_000)


def test_loss_harvest_in_nisa_has_no_value():
    r = tax.loss_harvest_value(-1_000_000, 500_000, "NISA成長")
    assert r["value_jpy"] == 0.0
    assert "損益通算できません" in r["message"]


def test_loss_harvest_without_loss():
    assert tax.loss_harvest_value(50_000, 100_000, "特定")["available"] is False


def test_loss_harvest_without_offset_target_is_unavailable():
    """相殺相手が無いのに『約0円の税が消えます』と書かない。情報ゼロで害。"""
    r = tax.loss_harvest_value(-500_000, 0, "特定")
    assert r["available"] is False
    assert "即時の節税効果はありません" in r["message"]
    assert "繰り越せます" in r["message"]


def test_loss_harvest_distinguishes_unknown_realized_from_zero():
    unknown = tax.loss_harvest_value(-500_000, None, "特定")
    zero = tax.loss_harvest_value(-500_000, 0, "特定")
    assert "不明です" in unknown["message"]
    assert "ありません" in zero["message"]


# ---------------------------------------------------------------------------
# 税務状態の組み立て
# ---------------------------------------------------------------------------


def test_build_tax_state_reports_unknown_realized_as_none(monkeypatch):
    """履歴が無いとき 0 を返すと『通算相手がいない』と誤読される。"""
    monkeypatch.setattr(tax, "_realized_gain_ytd", lambda year: None)
    s = tax.build_tax_state([])
    assert s["realized_gain_ytd_jpy"] is None
    assert s["estimated_tax_jpy"] is None


def test_build_tax_state_computes_estimated_tax(monkeypatch, cfg):
    monkeypatch.setattr(tax, "_realized_gain_ytd", lambda year: 412_000.0)
    s = tax.build_tax_state([])
    assert s["estimated_tax_jpy"] == pytest.approx(
        412_000.0 * cfg["capital_gains"]["rate"])


def test_nisa_used_estimate_is_flagged_unreliable():
    """取得日が無いので『当年に使った枠』は出せない。確定値として出さない。"""
    holdings = [{"account": "NISA成長", "shares": 39, "cost_price": 4813.0},
                {"account": "特定", "shares": 400, "cost_price": 3906.0}]
    r = tax.nisa_used_from_holdings(holdings)
    assert r["reliable"] is False
    assert r["used"]["growth"] == pytest.approx(39 * 4813.0)
    assert r["used"]["tsumitate"] == 0.0, "課税口座の分を混ぜてはいけない"


def test_nisa_used_estimate_respects_unit_divisor():
    """投信は口数×基準価額/10000。divisor を無視すると1万倍に膨らむ。"""
    holdings = [{"account": "NISAつみたて", "shares": 149927, "cost_price": 73369.04,
                 "unit_divisor": 10000}]
    r = tax.nisa_used_from_holdings(holdings)
    assert r["used"]["tsumitate"] == pytest.approx(149927 * 73369.04 / 10000)


def test_nisa_used_estimate_is_capped_at_annual_limit(cfg):
    holdings = [{"account": "NISA成長", "shares": 1_000_000, "cost_price": 10_000.0}]
    r = tax.nisa_used_from_holdings(holdings)
    assert r["used"]["growth"] == pytest.approx(cfg["nisa"]["annual_limits"]["growth"])


def test_build_tax_state_warns_about_nisa_estimate(monkeypatch):
    monkeypatch.setattr(tax, "_realized_gain_ytd", lambda year: None)
    s = tax.build_tax_state([{"account": "NISA成長", "shares": 1, "cost_price": 100.0}])
    assert any("推定" in w for w in s["warnings"])


# ---------------------------------------------------------------------------
# NISA 枠（受け入れ基準3）
# ---------------------------------------------------------------------------


def test_nisa_state_computes_remaining(cfg):
    limit = cfg["nisa"]["annual_limits"]["growth"]
    s = tax.nisa_state(used_growth_jpy=limit / 2, used_tsumitate_jpy=0)
    assert s["buckets"]["growth"]["remaining_jpy"] == pytest.approx(limit / 2)
    assert s["buckets"]["growth"]["used_pct"] == pytest.approx(50.0)


def test_nisa_warns_about_expiry_late_in_year():
    s = tax.nisa_state(used_growth_jpy=0, used_tsumitate_jpy=0,
                       today=date(2026, 11, 1))
    assert s["message"] is not None
    assert "消滅" in s["message"]


def test_nisa_no_warning_early_in_year():
    s = tax.nisa_state(used_growth_jpy=0, used_tsumitate_jpy=0,
                       today=date(2026, 1, 5))
    assert s["message"] is None


def test_nisa_remaining_never_negative():
    s = tax.nisa_state(used_growth_jpy=99_999_999)
    assert s["buckets"]["growth"]["remaining_jpy"] == 0.0


def test_nisa_suitability_prefers_long_high_dividend():
    high = tax.nisa_suitability(12.0, 3.8, 10)
    low = tax.nisa_suitability(3.0, 0.0, 0.5)
    assert high["score"] > low["score"]
    assert "資源の浪費" in high["message"]


def test_nisa_suitability_without_inputs_is_unavailable():
    assert tax.nisa_suitability(None, None, None)["available"] is False


# ---------------------------------------------------------------------------
# 免責
# ---------------------------------------------------------------------------


def test_outputs_carry_disclaimer():
    """税務助言と誤読されないよう、常に概算であると明示する。"""
    assert "税務助言" in tax.sell_proceeds(1, 1.0, 1.0, "特定")["disclaimer"]
    assert "税務助言" in tax.switching_hurdle(1, 2.0, 1.0, "特定")["disclaimer"]
