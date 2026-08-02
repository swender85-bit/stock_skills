"""損益計算書からの成長率導出（yfinance の比率欠損の補完）。

2026-08-01 の週次レポートは味の素について「earnings_growth 欠落。増収が利益に
落ちているか不明」と留保したが、同じ銘柄の income_stmt には
純利益 70.3B → 134.7B（**+91.6%**）と明記されていた。
比率フィールドが空だっただけで、原資料は取れていた。

守るべき性質:
- 導出値であることを必ず示す（他社の比率と同じ顔をさせない）
- 前期が赤字・ゼロなら成長率を返さない（符号の意味が壊れる）
- 既にある値を上書きしない
- 取れなければ None。推測しない
"""

from __future__ import annotations

import pytest

from src.data.yahoo_client import financials as fin


class _Stmt:
    """income_stmt の最小スタブ（DataFrame 風）。"""

    def __init__(self, rows: dict, periods=("2026-03-31", "2025-03-31")):
        self._rows = rows
        self.columns = list(periods)
        self.empty = not rows

    @property
    def index(self):
        return list(self._rows)

    @property
    def loc(self):
        rows = self._rows

        class _L:
            def __getitem__(self, k):
                class _S:
                    def tolist(_self):
                        return rows[k]
                return _S()
        return _L()


class _Ticker:
    def __init__(self, stmt):
        self.income_stmt = stmt


AJINOMOTO = _Stmt({
    "Net Income": [134675000000.0, 70272000000.0],
    "Operating Income": [191301000000.0, 107656000000.0],
    "Total Revenue": [1583719000000.0, 1530556000000.0],
    "Diluted EPS": [138.36, 34.885],
})


# ---------------------------------------------------------------------------
# 導出
# ---------------------------------------------------------------------------


def test_derives_the_growth_the_report_could_not_see():
    r = fin.derive_growth("2802.T", ticker=_Ticker(AJINOMOTO))
    assert r["available"] is True
    assert r["earnings_growth"] == pytest.approx(0.916, abs=0.01)
    assert r["operating_income_growth"] == pytest.approx(0.777, abs=0.01)
    assert r["revenue_growth"] == pytest.approx(0.035, abs=0.01)


def test_derivation_is_labelled_as_derived_with_its_periods():
    """他社の yfinance 比率と同じ顔をさせない。"""
    r = fin.derive_growth("2802.T", ticker=_Ticker(AJINOMOTO))
    assert r["source"] == "income_stmt"
    assert r["periods"] == ["2026-03-31", "2025-03-31"]
    assert "導出" in r["note"]


def test_alternate_row_names_are_accepted():
    stmt = _Stmt({"Net Income Common Stockholders": [200.0, 100.0],
                  "Operating Revenue": [50.0, 40.0]})
    r = fin.derive_growth("X", ticker=_Ticker(stmt))
    assert r["earnings_growth"] == pytest.approx(1.0)
    assert r["revenue_growth"] == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# 符号が壊れるケースを避ける
# ---------------------------------------------------------------------------


def test_growth_is_not_reported_when_the_prior_period_was_a_loss():
    """-100 → +50 を「+150%成長」と書くと意味が反転する。"""
    assert fin.growth_from_series([50.0, -100.0]) is None


def test_zero_prior_period_yields_none():
    assert fin.growth_from_series([50.0, 0.0]) is None


def test_turning_profitable_is_recorded_as_a_fact_not_a_rate():
    stmt = _Stmt({"Net Income": [50.0, -100.0]})
    r = fin.derive_growth("X", ticker=_Ticker(stmt))
    assert r.get("earnings_growth") is None
    assert r.get("turned_profitable") is True
    assert r["available"] is True


def test_nan_values_are_ignored():
    assert fin.growth_from_series([float("nan"), 100.0]) is None


def test_single_period_cannot_produce_growth():
    assert fin.growth_from_series([100.0]) is None


# ---------------------------------------------------------------------------
# 取れないとき
# ---------------------------------------------------------------------------


def test_empty_statement_reports_error_not_zero():
    r = fin.derive_growth("X", ticker=_Ticker(_Stmt({})))
    assert r["available"] is False
    assert "取得できません" in r["error"]


def test_statement_without_known_rows_reports_error():
    r = fin.derive_growth("X", ticker=_Ticker(_Stmt({"Weird Row": [1.0, 2.0]})))
    assert r["available"] is False


def test_exception_is_captured_not_raised():
    class _Boom:
        @property
        def income_stmt(self):
            raise RuntimeError("network")

    r = fin.derive_growth("X", ticker=_Boom())
    assert r["available"] is False
    assert "RuntimeError" in r["error"]


# ---------------------------------------------------------------------------
# detail への充填
# ---------------------------------------------------------------------------


def test_fill_only_touches_missing_fields():
    detail = {"symbol": "2802.T", "earnings_growth": None, "revenue_growth": 0.105}
    fin.fill_missing_growth(detail, ticker=_Ticker(AJINOMOTO))
    assert detail["earnings_growth"] == pytest.approx(0.916, abs=0.01)
    assert detail["revenue_growth"] == 0.105, "既存値を上書きしない"
    assert detail["growth_derived"]["fields"] == ["earnings_growth"]


def test_fill_is_a_noop_when_nothing_is_missing():
    detail = {"symbol": "X", "earnings_growth": 0.2, "revenue_growth": 0.1}
    fin.fill_missing_growth(detail, ticker=_Ticker(AJINOMOTO))
    assert "growth_derived" not in detail


def test_fill_records_why_it_could_not_derive():
    detail = {"symbol": "X", "earnings_growth": None, "revenue_growth": None}
    fin.fill_missing_growth(detail, ticker=_Ticker(_Stmt({})))
    assert detail["earnings_growth"] is None
    assert detail.get("growth_derivation_error")


def test_fill_survives_a_broken_ticker():
    class _Boom:
        @property
        def income_stmt(self):
            raise RuntimeError("x")

    detail = {"symbol": "X", "earnings_growth": None, "revenue_growth": None}
    fin.fill_missing_growth(detail, ticker=_Boom())
    assert detail["earnings_growth"] is None
