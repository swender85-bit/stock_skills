"""物語量スナップショットのテスト (土曜設計書 提案7)。

この段階で守るべきは分析精度ではなく**記録の健全性**。
遡れないデータなので、ゴミを溜めると恒久的に汚れる。
"""

from __future__ import annotations

import json

import pytest

from src.core.research import narrative as nv


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """既定でネットワークを塞ぐ。明示的に返したいテストだけ上書きする。"""
    monkeypatch.setattr(nv, "_http_json", lambda url, retries=2: None)
    monkeypatch.setattr(nv, "_throttle", lambda: None)
    nv.reset_circuit()
    yield
    nv.reset_circuit()


def _timeline(values):
    return {"timeline": [{"data": [{"value": v} for v in values]}]}


# ---------------------------------------------------------------------------
# GDELT
# ---------------------------------------------------------------------------


def test_gdelt_sums_volume_timeline(monkeypatch):
    monkeypatch.setattr(nv, "_http_json",
                        lambda url, retries=2: _timeline([1, 2, 3])
                        if "volraw" in url else _timeline([0.5, 1.5]))
    r = nv.fetch_volume("Nvidia", "NVDA")
    assert r["available"] is True
    assert r["articles"] == 6.0
    assert r["avg_tone"] == 1.0


def test_gdelt_tone_only_is_not_available(monkeypatch):
    """記事量が取れなければ available にしない。

    ここを緩めると articles=None のスナップショットが溜まり、
    後から混雑度の分母に使えないゴミになる。
    """
    monkeypatch.setattr(nv, "_http_json",
                        lambda url, retries=2: None if "volraw" in url
                        else _timeline([0.5]))
    r = nv.fetch_volume("Nvidia", "NVDA")
    assert r["available"] is False
    assert "トーンのみ" in r["error"]


def test_gdelt_circuit_breaker_opens_after_failures():
    """429 が続く回線で、保有数×リトライで週次が何分も止まらないようにする。"""
    for _ in range(nv._GDELT_FAIL_LIMIT):
        nv.fetch_volume("Nvidia", "NVDA")
    assert nv.gdelt_circuit_open() is True
    r = nv.fetch_volume("Apple", "AAPL")
    assert "以後スキップ" in r["error"]


def test_gdelt_query_prefers_company_name():
    assert nv._gdelt_query("Ajinomoto", "2802.T") == '"Ajinomoto"'
    assert nv._gdelt_query(None, "NVDA") == '"NVDA"'
    assert nv._gdelt_query(None, "A") is None


# ---------------------------------------------------------------------------
# 多重ソース
# ---------------------------------------------------------------------------


def test_multi_falls_through_to_next_source(monkeypatch):
    monkeypatch.setattr(nv, "fetch_volume",
                        lambda n, s, timespan="1w": {"available": False,
                                                     "error": "429", "source": "gdelt"})
    monkeypatch.setattr(nv, "fetch_volume_finnhub",
                        lambda s, days=7: {"available": True, "source": "finnhub",
                                           "articles": 12.0})
    r = nv.fetch_volume_multi("Apple", "AAPL")
    assert r["source"] == "finnhub"
    assert [a["source"] for a in r["attempts"]] == ["gdelt", "finnhub"]


def test_multi_reports_all_failures(monkeypatch):
    for name in ("fetch_volume_finnhub", "fetch_volume_yahoo"):
        monkeypatch.setattr(nv, name,
                            lambda *a, **k: {"available": False, "error": "no"})
    r = nv.fetch_volume_multi("Apple", "AAPL")
    assert r["available"] is False
    assert len(r["attempts"]) == 3


def test_finnhub_zero_articles_is_not_success(monkeypatch):
    """0件は『材料なし』ではなく『非対応の可能性』。"""
    import src.data.finnhub_client as fc

    monkeypatch.setattr(fc, "is_available", lambda: True)
    monkeypatch.setattr(fc, "get_company_news", lambda s, days=7, limit=1: [])
    r = nv.fetch_volume_finnhub("AAPL")
    assert r["available"] is False


# ---------------------------------------------------------------------------
# 記録
# ---------------------------------------------------------------------------


def test_capture_appends_only(tmp_path, monkeypatch):
    monkeypatch.setattr(nv, "fetch_volume_multi",
                        lambda n, s: {"available": True, "source": "gdelt",
                                      "articles": 10.0, "avg_tone": 0.2})
    monkeypatch.setattr(nv, "fetch_coverage",
                        lambda s: {"available": True, "analyst_count": 5,
                                   "name": "Test"})
    d = str(tmp_path)
    nv.capture("AAPL", "Apple", occasion="thesis", base_dir=d)
    nv.capture("AAPL", "Apple", occasion="weekly", base_dir=d)

    snaps = nv.load_snapshots("AAPL", base_dir=d)
    assert len(snaps) == 2
    assert [s["occasion"] for s in snaps] == ["thesis", "weekly"]


def test_capture_does_not_store_unavailable(tmp_path, monkeypatch):
    """取れなかったものを記録すると、後の分母がゼロ/Noneで汚れる。"""
    monkeypatch.setattr(nv, "fetch_volume_multi",
                        lambda n, s: {"available": False, "error": "x"})
    monkeypatch.setattr(nv, "fetch_coverage",
                        lambda s: {"available": False, "error": "y"})
    d = str(tmp_path)
    snap = nv.capture("AAPL", "Apple", base_dir=d)
    assert snap["available"] is False
    assert nv.load_snapshots("AAPL", base_dir=d) == []


def test_capture_disabled_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("NARRATIVE_ENABLED", "off")
    r = nv.capture("AAPL", "Apple", base_dir=str(tmp_path))
    assert r["available"] is False


def test_capture_many_survives_individual_failures(tmp_path, monkeypatch):
    calls = {"n": 0}

    def flaky(symbol, name=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return {"symbol": symbol, "available": True}

    monkeypatch.setattr(nv, "capture", flaky)
    r = nv.capture_many([{"symbol": "A"}, {"symbol": "B"}], base_dir=str(tmp_path))
    assert r["attempted"] == 2
    assert r["captured"] == 1


def test_capture_many_skips_rows_without_identity(tmp_path):
    r = nv.capture_many([{"symbol": None, "name": None}], base_dir=str(tmp_path))
    assert r["attempted"] == 0


def test_load_snapshots_tolerates_corrupt_lines(tmp_path):
    p = tmp_path / "AAPL.jsonl"
    p.write_text(json.dumps({"symbol": "AAPL", "captured_at": "2026-01-01"})
                 + "\nnot json\n", encoding="utf-8")
    assert len(nv.load_snapshots("AAPL", base_dir=str(tmp_path))) == 1


# ---------------------------------------------------------------------------
# 混雑度
# ---------------------------------------------------------------------------


def _write(tmp_path, rows):
    p = tmp_path / "AAPL.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")


def test_crowding_needs_a_baseline(tmp_path):
    _write(tmp_path, [{"symbol": "AAPL", "captured_at": "2026-01-01",
                       "articles": 10, "volume_source": "gdelt"}])
    r = nv.crowding("AAPL", base_dir=str(tmp_path))
    assert r["available"] is False
    assert "蓄積中" in r["reason"]


def test_crowding_uses_thesis_snapshot_as_denominator(tmp_path):
    _write(tmp_path, [
        {"symbol": "AAPL", "captured_at": "2026-01-01", "articles": 10,
         "occasion": "weekly", "volume_source": "gdelt"},
        {"symbol": "AAPL", "captured_at": "2026-02-01", "articles": 20,
         "occasion": "thesis", "volume_source": "gdelt"},
        {"symbol": "AAPL", "captured_at": "2026-03-01", "articles": 60,
         "occasion": "weekly", "volume_source": "gdelt"},
    ])
    r = nv.crowding("AAPL", base_dir=str(tmp_path))
    assert r["baseline_kind"] == "thesis"
    assert r["ratio"] == pytest.approx(3.0)


def test_crowding_never_mixes_sources(tmp_path):
    """GDELT の全世界記事数と Finnhub の件数は母集団が違う。比にしてはいけない。"""
    _write(tmp_path, [
        {"symbol": "AAPL", "captured_at": "2026-01-01", "articles": 1000,
         "volume_source": "gdelt"},
        {"symbol": "AAPL", "captured_at": "2026-03-01", "articles": 8,
         "volume_source": "finnhub"},
    ])
    r = nv.crowding("AAPL", base_dir=str(tmp_path))
    assert r["available"] is False
    assert r["volume_source"] == "finnhub"


def test_crowding_carries_caveat_against_sell_signal(tmp_path):
    _write(tmp_path, [
        {"symbol": "AAPL", "captured_at": "2026-01-01", "articles": 10,
         "volume_source": "gdelt"},
        {"symbol": "AAPL", "captured_at": "2026-03-01", "articles": 100,
         "volume_source": "gdelt"},
    ])
    r = nv.crowding("AAPL", base_dir=str(tmp_path))
    assert r["ratio"] == pytest.approx(10.0)
    assert "売り推奨を作りません" in r["caveat"]
