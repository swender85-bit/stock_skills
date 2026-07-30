"""現金・流入・注意予算のテスト (土曜設計書 提案9-⑨ 受け入れ基準)。

1. 入出金履歴から週次投資可能額が推定される
2. 売却を伴う全提案に、入金代替案が併記される
3. 「待つ」判断に期限と再評価日が必ず付く
4. 現金に目的が未割当の場合に警告が出る
5. 収入関連の絶対額がレポート・Neo4j に出力されない
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.core.portfolio import runway as rw


@pytest.fixture(autouse=True)
def _clean_cache():
    rw.reset_cache()
    yield
    rw.reset_cache()


def _cfg(**over):
    base = {
        "contributions": {"monthly_amount": 50000, "irregular": []},
        "estimation": {"lookback_weeks": 26, "percentile": 25, "runway_weeks": 12},
        "cash": {"purposes": []},
        "attention": {"weekly_review_minutes": 45, "min_minutes_per_holding": 4},
        "privacy": {"disclose_absolute_amounts": True},
    }
    for k, v in over.items():
        base[k] = {**base.get(k, {}), **v}
    return base


def _candidate(**over):
    base = {"symbol": "2802.T", "name": "味の素", "shares": 400, "price": 5102.0,
            "cost_price": 3906.03, "account": "特定", "currency": "JPY"}
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# 入金額の推定（受け入れ基準1）
# ---------------------------------------------------------------------------


def test_history_estimate_uses_conservative_percentile():
    """平均を使うと、賞与が入った週に引っ張られて待機見積もりが常に外れる。"""
    today = date.today()
    history = [{"date": (today - timedelta(weeks=w)).isoformat(),
                "amount_jpy": 500000 if w == 0 else 10000}
               for w in range(10)]
    r = rw.weekly_investable(history, _cfg())
    assert r["available"] is True
    assert r["basis"] == "history"
    assert r["weekly_jpy"] < 100000, "外れ値の賞与に引っ張られてはいけない"


def test_history_counts_weeks_without_deposits():
    """入金の無い週を落とすと『毎週入る』と誤認する。"""
    today = date.today()
    history = [{"date": (today - timedelta(weeks=8)).isoformat(), "amount_jpy": 80000},
               {"date": today.isoformat(), "amount_jpy": 80000}]
    r = rw.weekly_investable(history, _cfg())
    assert r["samples"] > 2
    assert r["weekly_jpy"] < 80000


def test_falls_back_to_config_monthly_amount():
    r = rw.weekly_investable(None, _cfg())
    assert r["basis"] == "config"
    assert r["weekly_jpy"] == pytest.approx(50000 * 12 / 52)


def test_unavailable_without_history_or_config():
    r = rw.weekly_investable(None, _cfg(contributions={"monthly_amount": 0}))
    assert r["available"] is False
    assert "cashflow.yaml" in r["note"]


def test_history_ignores_rows_outside_lookback():
    old = [{"date": (date.today() - timedelta(weeks=60)).isoformat(),
            "amount_jpy": 999999}]
    r = rw.weekly_investable(old, _cfg())
    assert r["basis"] == "config", "古すぎる実績は使わない"


def test_history_tolerates_malformed_rows():
    rows = ["not a dict", {"date": "bad", "amount_jpy": 1}, {"amount_jpy": None},
            {"date": date.today().isoformat(), "amount_jpy": 20000}]
    r = rw.weekly_investable(rows, _cfg())
    assert r["available"] is True


# ---------------------------------------------------------------------------
# ランウェイ
# ---------------------------------------------------------------------------


def test_runway_accumulates_cash_plus_contributions():
    r = rw.runway(10000.0, cash_jpy=50000.0, weeks=12, cfg=_cfg())
    assert r["cumulative_jpy"] == pytest.approx(50000 + 10000 * 12)
    assert len(r["schedule"]) == 12


def test_runway_warns_against_using_future_money_for_risk():
    """将来入金は取得可能額の計算にのみ使う。リスク拡大の根拠にしない。"""
    r = rw.runway(10000.0, cfg=_cfg())
    assert "リスクを増やしてはいけません" in r["caveat"]


def test_runway_unavailable_without_estimate():
    assert rw.runway(None, cfg=_cfg())["available"] is False


@pytest.mark.parametrize("target,cash,weekly,expected", [
    (100000, 0, 10000, 10),
    (100000, 100000, 10000, 0),
    (100000, 50000, 10000, 5),
    (100000, 0, 0, None),
    (100000, 0, None, None),
])
def test_weeks_until(target, cash, weekly, expected):
    assert rw.weeks_until(target, weekly, cash) == expected


# ---------------------------------------------------------------------------
# 資金調達の選択肢（受け入れ基準2・3）
# ---------------------------------------------------------------------------


def test_wait_is_recommended_when_reachable_in_horizon():
    """提案9の中核。『何もするな。N週後の入金で買え』が出ること。"""
    f = rw.funding_options(100000, cash_jpy=10000, weekly_jpy=11538,
                           sell_candidate=_candidate(), thesis_alive=True,
                           cfg=_cfg())
    assert f["recommended"] == "wait"
    wait = next(o for o in f["options"] if o["kind"] == "wait")
    assert wait["weeks"] == 8


def test_sell_option_always_accompanied_by_contribution_alternative():
    """受け入れ基準2: 売却案には必ず入金代替案が併記される。"""
    f = rw.funding_options(100000, cash_jpy=0, weekly_jpy=11538,
                           sell_candidate=_candidate(), cfg=_cfg())
    kinds = {o["kind"] for o in f["options"]}
    assert "sell" in kinds and "wait" in kinds


def test_wait_option_always_has_review_date():
    """受け入れ基準3: 無期限の保留を作らない。"""
    for target in (50000, 5_000_000):
        f = rw.funding_options(target, cash_jpy=0, weekly_jpy=11538, cfg=_cfg())
        wait = next(o for o in f["options"] if o["kind"] == "wait")
        assert wait["review_date"], "「待つ」に期限が無いと先延ばしの口実になる"


def test_unrealistic_wait_is_not_recommended():
    """『43週待て』は計画ではない。最善手として提示しない。"""
    f = rw.funding_options(500000, cash_jpy=10000, weekly_jpy=11538,
                           sell_candidate=_candidate(), thesis_alive=True,
                           cfg=_cfg())
    wait = next(o for o in f["options"] if o["kind"] == "wait")
    assert wait["realistic"] is False
    assert f["recommended"] != "wait"
    assert "買わない」と同義" in wait["detail"]


def test_live_thesis_sell_is_never_recommended():
    """『売る理由がない』と書きながら売却を推奨する矛盾を防ぐ。"""
    f = rw.funding_options(500000, cash_jpy=10000, weekly_jpy=11538,
                           sell_candidate=_candidate(), thesis_alive=True,
                           cfg=_cfg())
    assert f["recommended"] == "resize"
    sell = next(o for o in f["options"] if o["kind"] == "sell")
    assert "売る理由がありません" in sell["detail"]


def test_dead_thesis_sell_can_be_recommended():
    f = rw.funding_options(500000, cash_jpy=10000, weekly_jpy=11538,
                           sell_candidate=_candidate(), thesis_alive=False,
                           cfg=_cfg())
    assert f["recommended"] == "sell"


def test_cash_option_wins_when_cash_is_ample():
    f = rw.funding_options(100000, cash_jpy=5_000_000, weekly_jpy=100,
                           cfg=_cfg())
    assert f["recommended"] == "cash"


def test_cash_option_not_viable_when_short():
    f = rw.funding_options(100000, cash_jpy=1000, weekly_jpy=10000, cfg=_cfg())
    cash = next(o for o in f["options"] if o["kind"] == "cash")
    assert cash["viable"] is False
    assert "足りません" in cash["detail"]


def test_sell_option_reports_switching_hurdle():
    """受け入れ基準（提案3）: 売却案には損益分岐が付く。"""
    f = rw.funding_options(100000, sell_candidate=_candidate(), cfg=_cfg())
    sell = next(o for o in f["options"] if o["kind"] == "sell")
    assert sell["hurdle_pct"] is not None
    assert sell["friction_jpy"] > 0


def test_sell_option_unviable_on_incomplete_candidate():
    f = rw.funding_options(100000, sell_candidate={"symbol": "X"}, cfg=_cfg())
    sell = next(o for o in f["options"] if o["kind"] == "sell")
    assert sell["viable"] is False


def test_resize_not_offered_when_full_amount_is_reachable():
    f = rw.funding_options(50000, cash_jpy=0, weekly_jpy=11538, cfg=_cfg())
    resize = next(o for o in f["options"] if o["kind"] == "resize")
    assert resize["viable"] is False


def test_no_options_viable_without_estimate():
    f = rw.funding_options(100000, cash_jpy=0, weekly_jpy=None, cfg=_cfg())
    assert f["recommended"] is None


# ---------------------------------------------------------------------------
# 現金の目的（受け入れ基準4）
# ---------------------------------------------------------------------------


def test_unallocated_cash_warns():
    r = rw.cash_purpose_check(412000, 10_000_000, _cfg())
    assert r["unallocated_jpy"] == pytest.approx(412000)
    assert "目的がありません" in r["warning"]


def test_fully_allocated_cash_has_no_warning():
    cfg = _cfg(cash={"purposes": [{"label": "暴落用", "amount_jpy": 412000}]})
    r = rw.cash_purpose_check(412000, 10_000_000, cfg)
    assert r["warning"] is None


def test_cash_pct_is_computed():
    r = rw.cash_purpose_check(500000, 10_000_000, _cfg())
    assert r["cash_pct"] == pytest.approx(5.0)


def test_cash_check_handles_none():
    assert rw.cash_purpose_check(None, None, _cfg())["cash_jpy"] == 0.0


# ---------------------------------------------------------------------------
# 注意予算
# ---------------------------------------------------------------------------


def test_attention_budget_flags_too_many_holdings():
    r = rw.attention_budget(23, cfg=_cfg())
    assert r["minutes_per_holding"] < 4
    assert "監視されていません" in r["warning"]


def test_attention_budget_subtracts_orphans_from_monitored():
    """孤児はレビューの基準が無いので、実質監視されていない。"""
    r = rw.attention_budget(9, orphans_count=6, cfg=_cfg())
    assert r["effectively_monitored"] == 3
    assert "実質的に監視されている銘柄は3" in r["orphan_note"]


def test_attention_budget_suggests_max_holdings():
    r = rw.attention_budget(9, cfg=_cfg())
    assert r["suggested_max_holdings"] == 11
    assert "上限の目安" in r["guidance"]


def test_attention_budget_handles_zero_holdings():
    r = rw.attention_budget(0, cfg=_cfg())
    assert r["minutes_per_holding"] is None
    assert r["warning"] is None


# ---------------------------------------------------------------------------
# 収入情報の保護（受け入れ基準5）
# ---------------------------------------------------------------------------


def test_graph_payload_never_contains_absolute_amounts():
    """Neo4j に収入の絶対額を書かない。比率のみ。"""
    r = rw.runway(11538.0, cash_jpy=412000.0, weeks=12, cfg=_cfg())
    safe = rw.to_graph_safe(r, total_jpy=23_000_000)

    blob = repr(safe)
    for forbidden in ("11538", "412000", str(int(r["cumulative_jpy"]))):
        assert forbidden not in blob, f"絶対額 {forbidden} が漏れています"
    assert safe["weekly_pct_of_portfolio"] is not None


def test_graph_payload_without_total_omits_ratios():
    r = rw.runway(11538.0, cfg=_cfg())
    safe = rw.to_graph_safe(r, total_jpy=None)
    assert safe["weekly_pct_of_portfolio"] is None


def test_privacy_flag_suppresses_absolute_amounts_in_wait_option():
    cfg = _cfg(privacy={"disclose_absolute_amounts": False})
    f = rw.funding_options(100000, cash_jpy=0, weekly_jpy=11538, cfg=cfg)
    wait = next(o for o in f["options"] if o["kind"] == "wait")
    assert wait["amount_jpy"] is None
    assert "円" not in wait["detail"].replace("入金", "")
