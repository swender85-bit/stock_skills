"""outcome_tracker（学習ループ / upgrade v1.0 Phase 4）のユニットテスト。

株価取得は price_fn を差し替えてモックし、外部I/Oなしで検証する。
"""
from datetime import date

import pytest

from src.core.research import outcome_tracker as ot

pytestmark = pytest.mark.smoke


# --- verdict_direction ---

def test_verdict_buy():
    assert ot.verdict_direction("やや割安") == ot.BUY
    assert ot.verdict_direction("強い買い") == ot.BUY


def test_verdict_avoid():
    assert ot.verdict_direction("割高傾向") == ot.AVOID


def test_verdict_neutral():
    assert ot.verdict_direction("適正水準") == ot.NEUTRAL
    assert ot.verdict_direction("ETF") == ot.NEUTRAL
    assert ot.verdict_direction(None) == ot.NEUTRAL


def test_verdict_score_override():
    # 中立verdictでも高スコアなら買い寄り
    assert ot.verdict_direction("適正水準", value_score=75) == ot.BUY


# --- compute_outcome ---

def test_compute_outcome_gain():
    r = ot.compute_outcome(100, 120, "2026-01-01", today=date(2026, 7, 1))
    assert r["return_pct"] == pytest.approx(0.20)
    assert r["days_elapsed"] == 181


def test_compute_outcome_invalid_price():
    assert ot.compute_outcome(0, 120, "2026-01-01", today=date(2026, 7, 1)) is None
    assert ot.compute_outcome(100, None, "2026-01-01", today=date(2026, 7, 1)) is None


def test_compute_outcome_bad_date():
    assert ot.compute_outcome(100, 120, None, today=date(2026, 7, 1)) is None
    assert ot.compute_outcome(100, 120, "not-a-date", today=date(2026, 7, 1)) is None


def test_compute_outcome_future_date_rejected():
    assert ot.compute_outcome(100, 120, "2026-12-31", today=date(2026, 7, 1)) is None


# --- is_hit ---

def test_is_hit_buy():
    assert ot.is_hit(ot.BUY, 0.1) is True
    assert ot.is_hit(ot.BUY, -0.1) is False


def test_is_hit_avoid():
    assert ot.is_hit(ot.AVOID, -0.1) is True
    assert ot.is_hit(ot.AVOID, 0.1) is False


def test_is_hit_neutral_none():
    assert ot.is_hit(ot.NEUTRAL, 0.1) is None


# --- evaluate ---

def _make_judgments():
    return [
        {"source": "report", "symbol": "AAA", "entry_date": "2026-01-01",
         "entry_price": 100, "direction": ot.BUY, "label": "やや割安"},
        {"source": "report", "symbol": "BBB", "entry_date": "2026-01-01",
         "entry_price": 100, "direction": ot.AVOID, "label": "割高傾向"},
        {"source": "screen", "symbol": "CCC", "entry_date": "2026-06-20",
         "entry_price": 50, "direction": ot.BUY, "preset": "value"},
        {"source": "report", "symbol": "DDD", "entry_date": "2026-01-01",
         "entry_price": 100, "direction": ot.NEUTRAL, "label": "適正水準"},
    ]


def test_evaluate_aggregates():
    prices = {"AAA": 130, "BBB": 80, "CCC": 45, "DDD": 100}
    summary = ot.evaluate(_make_judgments(), lambda s: prices.get(s), today=date(2026, 7, 1))
    # NEUTRAL(DDD)は除外 → 3件評価
    assert summary["n_evaluated"] == 3
    assert summary["n_skipped"] == 1
    # AAA買い+30%命中, BBB回避-20%命中, CCC買い-10%外れ → 2/3
    assert summary["overall_hit_rate"] == pytest.approx(2 / 3)
    # 180日バケット: AAA,BBBのみ(181日) CCCは11日で除外
    assert summary["horizons"][180]["n"] == 2
    assert summary["horizons"][30]["n"] == 2  # AAA,BBB(181日) CCC(11日)は30未満で除外


def test_evaluate_skips_unpriced():
    summary = ot.evaluate(_make_judgments(), lambda s: None, today=date(2026, 7, 1))
    assert summary["n_evaluated"] == 0
    assert summary["overall_hit_rate"] is None


def test_evaluate_price_fn_exception_graceful():
    def boom(s):
        raise RuntimeError("network")
    summary = ot.evaluate(_make_judgments(), boom, today=date(2026, 7, 1))
    assert summary["n_evaluated"] == 0


def test_preset_stats():
    prices = {"CCC": 60, "EEE": 40}
    js = [
        {"source": "screen", "symbol": "CCC", "entry_date": "2026-01-01",
         "entry_price": 50, "direction": ot.BUY, "preset": "value"},
        {"source": "screen", "symbol": "EEE", "entry_date": "2026-01-01",
         "entry_price": 50, "direction": ot.BUY, "preset": "value"},
    ]
    summary = ot.evaluate(js, lambda s: prices.get(s), today=date(2026, 7, 1))
    assert summary["presets"]["value"]["n"] == 2
    assert summary["presets"]["value"]["hits"] == 1  # CCC+20%命中, EEE-20%外れ


# --- find_big_misses ---

def test_find_big_misses():
    prices = {"AAA": 70, "CCC": 95}  # AAA -30%(181日), CCC -5%(181日)
    js = [
        {"source": "screen", "symbol": "AAA", "entry_date": "2026-01-01",
         "entry_price": 100, "direction": ot.BUY, "preset": "value"},
        {"source": "screen", "symbol": "CCC", "entry_date": "2026-01-01",
         "entry_price": 100, "direction": ot.BUY, "preset": "value"},
    ]
    summary = ot.evaluate(js, lambda s: prices.get(s), today=date(2026, 7, 1))
    misses = ot.find_big_misses(summary, threshold_pct=-0.20, min_days=90)
    assert len(misses) == 1
    assert misses[0]["symbol"] == "AAA"


def test_find_big_misses_respects_min_days():
    prices = {"AAA": 70}
    js = [{"source": "screen", "symbol": "AAA", "entry_date": "2026-06-25",
           "entry_price": 100, "direction": ot.BUY, "preset": "value"}]
    summary = ot.evaluate(js, lambda s: prices.get(s), today=date(2026, 7, 1))
    # 6日しか経っていない → min_days=90 で除外
    assert ot.find_big_misses(summary, min_days=90) == []


# --- render_markdown ---

def test_render_markdown_has_frontmatter_and_tables():
    prices = {"AAA": 130}
    js = [{"source": "report", "symbol": "AAA", "entry_date": "2026-01-01",
           "entry_price": 100, "direction": ot.BUY, "label": "やや割安"}]
    summary = ot.evaluate(js, lambda s: prices.get(s), today=date(2026, 7, 1))
    md = ot.render_markdown(summary)
    assert md.startswith("---")
    assert "created: 2026-07-01" in md
    assert "総合命中率" in md
    assert "| 経過 |" in md


# --- collect_judgments (I/O, tmp history) ---

def test_collect_judgments_from_history(tmp_path):
    import json
    hist = tmp_path / "history"
    (hist / "report").mkdir(parents=True)
    (hist / "screen").mkdir(parents=True)
    (hist / "report" / "2026-01-01_AAA.json").write_text(json.dumps({
        "symbol": "AAA", "date": "2026-01-01", "price": 100,
        "verdict": "やや割安", "value_score": 60,
    }), encoding="utf-8")
    (hist / "screen" / "2026-01-02_us_value.json").write_text(json.dumps({
        "date": "2026-01-02", "preset": "value", "region": "us",
        "results": [{"symbol": "CCC", "price": 50, "name": "C Corp"}],
    }), encoding="utf-8")

    js = ot.collect_judgments(base_dir=str(hist))
    by_sym = {j["symbol"]: j for j in js}
    assert by_sym["AAA"]["direction"] == ot.BUY
    assert by_sym["AAA"]["source"] == "report"
    assert by_sym["CCC"]["direction"] == ot.BUY
    assert by_sym["CCC"]["preset"] == "value"
