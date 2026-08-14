"""briefing_pack.py のテスト（ネットワーク非依存・モック）。"""

import json
from pathlib import Path

import pytest

from src.core.research import briefing_pack as bp


# --- week_over_week_delta ---------------------------------------------------


def test_wow_delta_computes_diffs():
    prior_index = {
        "QCOM": [
            {"date": "2026-07-01", "per": 20.0, "price": 150.0, "verdict": "適正"},
            {"date": "2026-06-01", "per": 22.0, "price": 140.0},
        ]
    }
    current = {"per": 18.0, "price": 170.0, "verdict": "やや割高"}
    d = bp.week_over_week_delta("QCOM", current, prior_index, today="2026-07-25")
    assert d["prior_date"] == "2026-07-01"  # today より前で最新
    assert d["fields"]["per"]["prior"] == 20.0
    assert d["fields"]["per"]["now"] == 18.0
    assert d["fields"]["per"]["diff"] == -2.0
    assert d["now_verdict"] == "やや割高"
    assert d["prior_verdict"] == "適正"


def test_wow_delta_none_when_no_prior():
    d = bp.week_over_week_delta("XXX", {"per": 1.0}, {}, today="2026-07-25")
    assert d is None


def test_wow_delta_ignores_future_snapshots():
    prior_index = {"AAA": [{"date": "2026-08-01", "per": 5.0}]}  # 未来
    d = bp.week_over_week_delta("AAA", {"per": 6.0}, prior_index, today="2026-07-25")
    assert d is None


# --- _prior_report_index (temp history dir) --------------------------------


def test_prior_report_index_indexes_by_symbol(monkeypatch):
    fake_snaps = [
        {"symbol": "QCOM", "date": "2026-07-20", "per": 18.0},
        {"symbol": "qcom", "date": "2026-07-10", "per": 19.0},  # 大小混在
        {"symbol": "SOXL", "date": "2026-07-20", "price": 130.0},
    ]
    import src.data.history.load as hload

    monkeypatch.setattr(hload, "load_history",
                        lambda category, days_back=None: fake_snaps)
    idx = bp._prior_report_index()
    assert set(idx) == {"QCOM", "SOXL"}
    assert len(idx["QCOM"]) == 2  # 大小同一キーに集約


# --- _forward_schedule ------------------------------------------------------


def test_forward_schedule_merges_calendars():
    moomoo = {
        "economic_events": [{"title": "CPI", "country": "US", "star": "HIGH"}],
        "earnings": [{"security": "QCOM", "date": "2026-07-30", "period": "Q3"}],
        "dividends": [{"security": "MDT", "ex_date": "2026-08-01"}],
        "fed_watch": {"next_meeting": "2026-09-17", "top_range": "425-450", "top_prob": 70.0},
    }
    sched = bp._forward_schedule(moomoo)
    kinds = {e["kind"] for e in sched}
    assert kinds == {"economic", "earnings", "dividend", "fomc"}


def test_forward_schedule_empty_on_empty_moomoo():
    assert bp._forward_schedule({}) == []


# --- _compact_technicals ----------------------------------------------------


def test_compact_technicals_subset():
    t = {
        "last": 100.0, "sma200": 90.0, "rsi14": 65.0,
        "bollinger": {"percent_b": 0.8},
        "range_52w": {"position": 0.7, "from_high_pct": -5.0},
        "heat": {"state": "overbought", "label": "買われすぎ", "signals": ["RSI>70"]},
    }
    c = bp._compact_technicals(t)
    assert c["rsi14"] == 65.0
    assert c["percent_b"] == 0.8
    assert c["heat_state"] == "overbought"


def test_compact_technicals_none():
    assert bp._compact_technicals(None) is None


# --- build_symbol_briefing (fully mocked) -----------------------------------


def test_build_symbol_briefing_graceful(monkeypatch):
    import src.data.yahoo_client as yc

    monkeypatch.setattr(yc, "get_stock_info",
                        lambda s: {"name": "Toyota", "price": 2800.0, "per": 10.0})
    monkeypatch.setattr(yc, "get_stock_detail", lambda s: None)
    monkeypatch.setattr(yc, "get_price_history", lambda s, period="2y": None)
    # 外部依存は全て空に
    monkeypatch.setattr(bp, "_prior_report_index", lambda days_back=180: {})
    monkeypatch.setattr(bp, "_safe_competitors", lambda syms: {})
    monkeypatch.setattr(bp, "_safe_news_watch", lambda syms: {})
    monkeypatch.setattr(bp, "_safe_moomoo", lambda syms: {})
    monkeypatch.setattr(bp, "_safe_context", lambda q: "")
    # ⚠️ 判断層（ルックスルー・構成銘柄・前方カレンダー・レジーム・一次観測…）は
    # あとから足されたが、このテストは塞いでいなかった。**「fully mocked」と
    # 名乗りながら実際には yfinance を叩き、この1件だけで80秒かかっていた**。
    # 遅いだけでなく、ネットワークの調子で結果が変わる＝回帰テストにならない。
    for name, empty in (
        ("_safe_symbol_lookthrough", {}), ("_safe_constituent_intel", {}),
        ("_safe_forward_horizon", {}), ("_safe_leverage_sleeve", {}),
        ("_safe_regime", {}), ("_safe_primary_filings", {}),
        ("_safe_external_views", {}), ("_safe_falsification", {}),
        ("_safe_symbol_policy", {}), ("_safe_assumption_space_for", {}),
        ("_safe_narrative", {}),
    ):
        monkeypatch.setattr(bp, name, (lambda v: lambda *a, **k: v)(empty))
    monkeypatch.setattr(bp, "_closes_1y", lambda s: [])
    monkeypatch.setattr(
        "src.core.risk.forward_events.build_forward_section", lambda h: {})

    monkeypatch.setattr(bp, "_safe_composition_check", lambda: {})

    pack = bp.build_symbol_briefing("7203.T")
    assert pack["mode"] == "symbol"
    assert pack["meta"]["symbol"] == "7203.T"
    assert pack["pack_version"] == bp.PACK_VERSION
    assert pack["info"]["name"] == "Toyota"
    # 見通しの材料は、履歴が取れなくても必ずキーとして存在する
    assert "projection" in pack


def test_symbol_pack_provides_every_field_the_prompt_promises():
    """`stock_deep.md` の材料表に載っている材料が、実際にパックに入っていること。

    **表に書いてあるのに入っていない材料があった**（`composition_check`）。
    仕様が「これを読め」と書いていても、材料が来なければ指示は空振りする。
    """
    import inspect
    import re

    spec = Path(".claude/prompts/stock_deep.md").read_text(encoding="utf-8")
    promised = set(re.findall(r"^\| `([a-z_]+)` \|", spec, re.M))
    assert len(promised) >= 10, "材料表を読めていない（正規表現が壊れた）"

    src = inspect.getsource(bp.build_symbol_briefing)
    returned = set(re.findall(r'^\s+"([a-z_]+)":', src, re.M))
    missing = sorted(promised - returned)
    assert not missing, f"仕様が約束しているのにパックに無い材料: {missing}"


class TestSymbolComposition:
    def test_matches_fund_by_name_variant(self):
        """投信はティッカーを持たないことがあるので名前で照合する。"""
        assert bp._matches_fund("iFreeNEXT FANG+", None,
                                "iFreeNEXT FANG+インデックス")

    def test_unrelated_symbol_is_not_matched(self):
        assert not bp._matches_fund("iFreeNEXT FANG+", "7203.T", "トヨタ自動車")

    def test_non_fund_returns_empty(self, monkeypatch):
        monkeypatch.setattr(
            bp, "_safe_composition_check",
            lambda: {"iFreeNEXT FANG+": {"available": True}})
        assert bp._safe_symbol_composition("7203.T", "トヨタ自動車") == {}

    def test_fund_gets_only_its_own_check(self, monkeypatch):
        monkeypatch.setattr(
            bp, "_safe_composition_check",
            lambda: {"iFreeNEXT FANG+": {"available": True, "correlation": 0.92},
                     "他の投信": {"available": True}})
        got = bp._safe_symbol_composition(None, "iFreeNEXT FANG+インデックス")
        assert list(got) == ["iFreeNEXT FANG+"]

    def test_error_is_not_silently_empty(self, monkeypatch):
        """検証に失敗したことを「対象外」と同じ空で返さない。"""
        monkeypatch.setattr(
            bp, "_safe_composition_check", lambda: {"_error": "boom"})
        assert "_error" in bp._safe_symbol_composition("X", "X")


# --- _safe_symbol_projection -------------------------------------------------
#
# 「これから」を書く節の材料。**点推定ではなくレンジ**であることと、
# 取得できなかったときに「変動しない」と読ませないことを縛る。


class TestSymbolProjection:
    @pytest.fixture(autouse=True)
    def _no_network(self, monkeypatch):
        """履歴取得を閉じる。**較正の有無ではなくレンジの形を試す**テスト群なので、
        ここで実際に yfinance を叩くとテストが数十秒かかり、しかも
        取得結果によって結果が変わる（＝回帰テストにならない）。"""
        monkeypatch.setattr(bp, "_closes_1y", lambda s: [])

    def test_returns_all_horizons_in_order(self):
        got = bp._safe_symbol_projection("QCOM", {"leverage": 1}, None)
        assert got["available"] is True
        assert [h["key"] for h in got["horizons"]] == [
            "short", "quarter", "mid", "long"]

    def test_basis_is_index_not_money(self):
        """金額で返すと『いくら儲かる』と読まれる。非保有銘柄にも答えるので倍率で語る。"""
        got = bp._safe_symbol_projection("QCOM", {}, None)
        assert "100" in got["basis"]
        for h in got["horizons"]:
            assert h["low_pct"] < h["high_pct"]

    def test_leverage_widens_the_range_and_adds_drag(self):
        one = bp._safe_symbol_projection("SOXL", {"leverage": 1}, None)
        three = bp._safe_symbol_projection("SOXL", {"leverage": 3}, None)
        assert three["drag_pct"] > one["drag_pct"]
        w1 = one["horizons"][0]["high_pct"] - one["horizons"][0]["low_pct"]
        w3 = three["horizons"][0]["high_pct"] - three["horizons"][0]["low_pct"]
        assert w3 > w1

    def test_vol_source_is_disclosed(self):
        """前提σを使ったのか較正済みσを使ったのかを黙らない。"""
        got = bp._safe_symbol_projection("QCOM", {}, None)
        assert got["vol_source"]

    def test_failure_does_not_read_as_no_movement(self, monkeypatch):
        monkeypatch.setattr(
            bp, "_closes_1y",
            lambda s: (_ for _ in ()).throw(RuntimeError("boom")))
        got = bp._safe_symbol_projection("QCOM", {}, None)
        assert got["available"] is False
        assert "変動しない" in got["note"]  # 誤読を明示的に否定している


class TestCloses1y:
    def test_series_truthiness_is_not_used(self, monkeypatch):
        """⚠️ `hist.get("Close") or []` は pandas で ValueError になる。

        そこで黙って握り潰されると実測σが**前提σへ静かに退化**する。
        実際に起きた事故なので、DataFrame を渡して値が返ることを固定する。
        """
        pd = pytest.importorskip("pandas")
        import src.data.yahoo_client as yc

        df = pd.DataFrame({"Close": [100.0, 101.0, 99.5]})
        monkeypatch.setattr(yc, "get_price_history", lambda s, period="1y": df)
        assert bp._closes_1y("QCOM") == [100.0, 101.0, 99.5]

    def test_drops_non_positive_and_nan(self, monkeypatch):
        pd = pytest.importorskip("pandas")
        import src.data.yahoo_client as yc

        df = pd.DataFrame({"Close": [100.0, float("nan"), 0.0, -5.0, 102.0]})
        monkeypatch.setattr(yc, "get_price_history", lambda s, period="1y": df)
        assert bp._closes_1y("QCOM") == [100.0, 102.0]

    def test_missing_history_is_empty_not_error(self, monkeypatch):
        import src.data.yahoo_client as yc

        monkeypatch.setattr(yc, "get_price_history", lambda s, period="1y": None)
        assert bp._closes_1y("QCOM") == []
