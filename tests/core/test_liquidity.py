"""流動性制約付きストレステストのテスト (土曜設計書 提案6-⑨ 受け入れ基準)。

1. 解消所要日数が保有株数と出来高から正しく計算される
2. ストレステストの推奨アクションに実行可能性判定が付く
3. 実行不能な推奨が明示的に警告される
4. 暴落時の買い推奨が現金余力と突合される
5. 口座区分別の税務非対称が表示される
"""

from __future__ import annotations

import pytest

from src.core.risk import liquidity as lq


def _holding(symbol="1111.T", shares=1000, weight=10.0, name=None, account="特定"):
    return {"symbol": symbol, "name": name or symbol, "shares": shares,
            "weight_pct": weight, "account": account,
            "value_jpy": weight * 100_000}


def _vol(symbol, adv):
    return {symbol: {"symbol": symbol, "available": True, "adv": adv}}


# ---------------------------------------------------------------------------
# 解消所要日数（受け入れ基準1）
# ---------------------------------------------------------------------------


def test_days_to_liquidate_uses_participation_rate():
    """出来高の10%までしか売らない前提。上げると『売れるはず』の楽観が入る。"""
    assert lq.days_to_liquidate(10_000, 10_000, 0.10) == pytest.approx(10.0)
    assert lq.days_to_liquidate(10_000, 10_000, 0.50) == pytest.approx(2.0)


@pytest.mark.parametrize("shares,adv", [
    (None, 1000), (1000, None), (1000, 0),
])
def test_days_to_liquidate_returns_none_on_missing_inputs(shares, adv):
    assert lq.days_to_liquidate(shares, adv) is None


def test_profile_tiers():
    fast = lq.liquidity_profile(_holding(shares=100), adv=1_000_000)
    slow = lq.liquidity_profile(_holding(shares=1_000_000), adv=100_000)
    assert fast["tier"] == "immediate"
    assert slow["tier"] == "trapped"


def test_profile_reports_stress_range_not_a_single_number():
    """ストレス時の出来高は事前に分からない。単一の値に決めない。"""
    p = lq.liquidity_profile(_holding(shares=100_000), adv=100_000)
    assert set(p["days_stress"]) == set(lq.STRESS_VOLUME_FACTORS)
    assert p["days_stress"]["conservative"] > p["days_stress"]["optimistic"]
    assert "事前に分かりません" in p["note"]


def test_profile_unknown_is_not_treated_as_liquid():
    """測れていないものを流動的と扱わない。"""
    p = lq.liquidity_profile(_holding(), adv=None)
    assert p["available"] is False
    assert p["tier"] == "unknown"
    assert "流動性が高いという意味ではありません" in p["reason"]


def test_average_volume_distinguishes_zero_from_missing():
    assert lq.average_volume(None) is None


class _FakeHistory:
    def __init__(self, volumes):
        self._v = volumes

    def __getitem__(self, key):
        assert key == "Volume"
        return self

    def dropna(self):
        return self

    def __iter__(self):
        return iter(self._v)


def test_average_volume_ignores_zero_volume_days():
    hist = _FakeHistory([0, 0, 100.0, 200.0])
    assert lq.average_volume(hist) == pytest.approx(150.0)


def test_average_volume_none_when_all_zero():
    assert lq.average_volume(_FakeHistory([0, 0])) is None


# ---------------------------------------------------------------------------
# PF 層別
# ---------------------------------------------------------------------------


def test_portfolio_splits_into_tiers():
    holdings = [_holding("A", shares=100, weight=60.0),
                _holding("B", shares=1_000_000, weight=40.0)]
    volumes = {**_vol("A", 1_000_000), **_vol("B", 100_000)}
    r = lq.portfolio_liquidity(holdings, volumes)
    assert r["tiers_pct"]["immediate"] == pytest.approx(60.0)
    assert r["tiers_pct"]["trapped"] == pytest.approx(40.0)
    assert "閉じ込め資本" in r["message"]


def test_unknown_tier_is_not_merged_into_immediate():
    holdings = [_holding("A", weight=50.0), _holding("B", weight=50.0)]
    r = lq.portfolio_liquidity(holdings, _vol("A", 1_000_000))
    assert r["tiers_pct"]["unknown"] == pytest.approx(50.0)
    assert "判定不能" in r["message"]


def test_symbol_less_holdings_are_reported_not_silently_dropped():
    """投信を黙って落とすと合計が100%にならず『残りは流動的』と誤読される。"""
    holdings = [_holding("A", weight=70.0),
                {"symbol": None, "name": "FANG+投信", "weight_pct": 30.0}]
    r = lq.portfolio_liquidity(holdings, _vol("A", 1_000_000))
    assert r["unmeasurable_pct"] == pytest.approx(30.0)
    assert "測れません" in r["message"]


@pytest.mark.parametrize("days,expected", [
    (None, "判定不能"), (0.01, "即日"), (0.5, "0.50日"), (12.4, "12.4日"),
])
def test_format_days(days, expected):
    assert lq.format_days(days) == expected


# ---------------------------------------------------------------------------
# 実行可能性（受け入れ基準2・3）
# ---------------------------------------------------------------------------


def _liq(symbol, shares, adv, weight=10.0):
    return lq.portfolio_liquidity([_holding(symbol, shares=shares, weight=weight)],
                                  _vol(symbol, adv))


def test_infeasible_sell_recommendation_is_flagged():
    liq = _liq("RRRR", shares=2_000_000, adv=100_000)
    r = lq.check_recommendation_feasibility(
        [{"symbol": "RRRR", "action": "SELL"}], liq)
    row = r["checked"][0]
    assert row["feasible"] is False
    assert "実行不能です" in row["reason"]
    assert len(row["alternatives"]) == 3
    assert "実行不能な推奨" in r["message"]


def test_feasible_sell_passes():
    liq = _liq("AAAA", shares=100, adv=1_000_000)
    r = lq.check_recommendation_feasibility(
        [{"symbol": "AAAA", "action": "SELL"}], liq)
    assert r["checked"][0]["feasible"] is True
    assert r["message"] is None


def test_non_sell_recommendations_are_not_constrained():
    liq = _liq("AAAA", shares=100, adv=1_000_000)
    r = lq.check_recommendation_feasibility(
        [{"symbol": "AAAA", "action": "ADD"}], liq)
    assert r["checked"][0]["feasible"] is True
    assert "売却を伴わない" in r["checked"][0]["reason"]


def test_unmeasurable_sell_is_unknown_not_feasible():
    """出来高が取れないことを『実行できる』と読ませない。"""
    liq = lq.portfolio_liquidity([_holding("X")], {})
    r = lq.check_recommendation_feasibility(
        [{"symbol": "X", "action": "SELL"}], liq)
    assert r["checked"][0]["feasible"] is None
    assert "実行できるという意味ではありません" in r["checked"][0]["reason"]
    assert len(r["unknown"]) == 1


def test_japanese_sell_action_is_recognized():
    liq = _liq("RRRR", shares=2_000_000, adv=100_000)
    r = lq.check_recommendation_feasibility(
        [{"symbol": "RRRR", "action": "売却"}], liq)
    assert r["checked"][0]["feasible"] is False


def test_stress_case_choice_changes_verdict():
    liq = _liq("MID", shares=300_000, adv=100_000)
    optimistic = lq.check_recommendation_feasibility(
        [{"symbol": "MID", "action": "SELL"}], liq, stress_case="optimistic")
    conservative = lq.check_recommendation_feasibility(
        [{"symbol": "MID", "action": "SELL"}], liq, stress_case="conservative")
    assert (conservative["checked"][0]["days_stress"]
            > optimistic["checked"][0]["days_stress"])


# ---------------------------------------------------------------------------
# 暴落時の資金余力（受け入れ基準4）
# ---------------------------------------------------------------------------


def test_buy_recommendations_are_reconciled_with_cash():
    r = lq.crash_buying_power(cash_jpy=412_000, total_jpy=10_000_000,
                              buy_candidates=["A"] * 7)
    assert r["affordable_count"] == 0
    assert "実際に買えるのは" in r["message"]


def test_low_cash_alone_triggers_a_warning():
    r = lq.crash_buying_power(cash_jpy=10_000, total_jpy=10_000_000,
                              buy_candidates=[])
    assert "暴落前に現金を用意して初めて計画" in r["message"]


def test_ample_cash_produces_no_warning():
    r = lq.crash_buying_power(cash_jpy=5_000_000, total_jpy=10_000_000,
                              buy_candidates=["A"])
    assert r["message"] is None


def test_buying_power_notes_the_timing_trap():
    r = lq.crash_buying_power(1000, 100_000, [])
    assert "暴落直前" in r["note"]


# ---------------------------------------------------------------------------
# 口座区分の非対称（受け入れ基準5）
# ---------------------------------------------------------------------------


def test_nisa_downside_asymmetry_is_reported():
    holdings = [_holding("A", weight=69.0, account="特定"),
                _holding("B", weight=31.0, account="NISA成長")]
    r = lq.account_asymmetry(holdings)
    assert r["available"] is True
    assert r["tax_free_pct"] == pytest.approx(31.0)
    assert "損益通算できません" in r["message"]


def test_no_nisa_means_no_asymmetry_message():
    r = lq.account_asymmetry([_holding("A", weight=100.0, account="特定")])
    assert r["message"] is None


def test_unknown_account_is_tracked_separately():
    holdings = [{"symbol": "A", "weight_pct": 50.0},
                _holding("B", weight=50.0, account="特定")]
    r = lq.account_asymmetry(holdings)
    assert r["unknown_pct"] == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# まとめ
# ---------------------------------------------------------------------------


def test_build_section_survives_missing_volumes():
    r = lq.build_liquidity_section([_holding("A")], volumes={},
                                   cash_jpy=0, total_jpy=1_000_000)
    assert "liquidity" in r
    assert r["liquidity"]["tiers_pct"]["unknown"] > 0
    assert r["errors"] == []


def test_build_section_includes_all_subsections():
    r = lq.build_liquidity_section(
        [_holding("A", shares=100)], volumes=_vol("A", 1_000_000),
        cash_jpy=100_000, total_jpy=1_000_000,
        recommendations=[{"symbol": "A", "action": "SELL"}],
        buy_candidates=["X"])
    for key in ("liquidity", "feasibility", "buying_power", "account_asymmetry"):
        assert r[key] is not None, key


def test_participation_rate_is_conservative_by_default():
    assert lq.DEFAULT_PARTICIPATION_RATE <= 0.10
