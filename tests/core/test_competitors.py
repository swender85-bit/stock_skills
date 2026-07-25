"""competitors.py のテスト（ネットワーク非依存・モック）。"""

from src.core.research import competitors as C


class _FakeYahoo:
    def __init__(self, infos, closes):
        self._infos = infos
        self._closes = closes

    def get_stock_info(self, symbol):
        return self._infos.get(symbol)

    def get_price_history(self, symbol, period="1mo"):
        import pandas as pd

        data = self._closes.get(symbol)
        if data is None:
            raise RuntimeError("no data")
        return pd.DataFrame({"Close": data})


def test_peers_for_explicit_from_yaml():
    peers = C.peers_for("SOXL")
    assert "NVDA" in peers
    assert "SOXL" not in peers  # 自分自身は除外


def test_peers_for_unknown_returns_empty():
    assert C.peers_for("ZZZZ_UNKNOWN") == []


def test_peer_note_present_and_absent():
    assert C.peer_note("SOXL")  # notes に定義あり
    assert C.peer_note("ZZZZ_UNKNOWN") is None


def test_fetch_peer_snapshot_computes_weekly_change():
    client = _FakeYahoo(
        infos={"NVDA": {"name": "NVIDIA", "price": 110.0, "per": 40.0,
                        "revenue_growth": 0.5}},
        closes={"NVDA": [100, 102, 104, 106, 108, 110]},  # 6本前(=[-6])=100 → +10%
    )
    snap = C.fetch_peer_snapshot("NVDA", client=client)
    assert snap["symbol"] == "NVDA"
    assert snap["name"] == "NVIDIA"
    assert round(snap["week_change_pct"], 1) == 10.0


def test_fetch_peer_snapshot_none_when_no_info():
    client = _FakeYahoo(infos={}, closes={})
    assert C.fetch_peer_snapshot("NVDA", client=client) is None


def test_build_peer_context_groups_and_dedups():
    infos = {s: {"name": s, "price": 10.0} for s in ("NVDA", "AVGO", "AMD", "TSM", "MU")}
    closes = {s: [10, 10, 10, 10, 10, 10, 11] for s in infos}
    client = _FakeYahoo(infos=infos, closes=closes)
    ctx = C.build_peer_context(["SOXL"], client=client, max_peers=3)
    assert "SOXL" in ctx
    assert ctx["SOXL"]["note"]
    assert len(ctx["SOXL"]["peers"]) == 3  # max_peers で制限


def test_build_peer_context_skips_holding_without_peers():
    client = _FakeYahoo(infos={}, closes={})
    ctx = C.build_peer_context(["ZZZZ_UNKNOWN"], client=client)
    assert ctx == {}
