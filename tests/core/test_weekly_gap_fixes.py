"""2026-08-01 の週次レポートが自ら挙げた穴のうち、外部依存で塞いだ分の検証。

いずれも「moomoo が無効だと丸ごと欠ける」という単一依存が原因だった:

- 月曜寄付の織り込み … moomoo からしか先物を見ておらず、US LV3 に日本指数の
  権限が無いため毎週「取得できず」になっていた
- 投信のテクニカル … ティッカーが無く基準価額が取れず「判定不能」。
  PF の 6.9%（FANG+）が過熱判定の対象外だった
- vault の同期 … 書いた直後の検証は通るのに、翌日には消えていた
"""

from __future__ import annotations

import os

from src.core.risk import forward_events as fe


# ---------------------------------------------------------------------------
# 月曜寄付（日経先物）
# ---------------------------------------------------------------------------


def test_futures_prefers_moomoo_when_available(monkeypatch):
    monkeypatch.setattr(fe, "NIKKEI_FUTURES_TICKERS", ())
    r = fe._nikkei_futures({"nikkei_futures": {"price": 63000.0}})
    assert r["source"] == "moomoo"


def test_futures_falls_back_to_yfinance_without_moomoo(monkeypatch):
    """moomoo 無効でも先物は取れる。ここが単一依存だった。"""
    class _T:
        def __init__(self, t):
            self.t = t

        def history(self, period="5d"):
            import pandas as pd

            return pd.DataFrame({"Close": [63100.0, 63190.0]},
                                index=pd.to_datetime(["2026-07-30", "2026-07-31"]))

    import yfinance as yf

    monkeypatch.setattr(yf, "Ticker", _T)
    r = fe._nikkei_futures(None)
    assert r["price"] == 63190.0
    assert r["source"].startswith("yfinance:")


def test_futures_returns_none_when_every_source_fails(monkeypatch):
    class _Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("network")

    import yfinance as yf

    monkeypatch.setattr(yf, "Ticker", _Boom)
    assert fe._nikkei_futures({}) is None


def test_monday_message_says_below_when_futures_are_lower(monkeypatch):
    """下落時に「-1.82% 上回って」と書かれる文言バグの回帰テスト。"""
    monkeypatch.setattr(fe, "_nikkei_futures",
                        lambda m: {"price": 63190.0, "source": "yfinance:NIY=F"})
    r = fe.monday_outlook([{"symbol": "^N225", "price": 64362.0}], None)
    assert r["available"] is True
    assert "下回って" in r["message"]
    assert "上回って" not in r["message"]
    assert "-1.82% 上" not in r["message"]


def test_monday_outlook_reports_unavailable_rather_than_calm(monkeypatch):
    monkeypatch.setattr(fe, "_nikkei_futures", lambda m: None)
    r = fe.monday_outlook([{"symbol": "^N225", "price": 64362.0}], None)
    assert r["available"] is False
    assert "取得できず" in r["message"]


# ---------------------------------------------------------------------------
# 投信のテクニカル（代理指数）
# ---------------------------------------------------------------------------


def test_technical_proxy_resolves_fund_by_name():
    from src.core.risk.etf_lookthrough import resolve_technical_proxy

    r = resolve_technical_proxy(None, "iFreeNEXT FANG+インデックス")
    assert r is not None
    assert r["proxy"] == "^NYFANG"
    assert r["approximate"] is True


def test_technical_proxy_not_used_for_symbols_with_own_prices():
    from src.core.risk.etf_lookthrough import resolve_technical_proxy

    assert resolve_technical_proxy("AAPL", "Apple") is None


def test_proxy_technicals_are_labelled_as_proxy(monkeypatch):
    """代理値を基準価額そのものとして書かせないため、必ず印を付ける。"""
    import pandas as pd

    from src.core.research import briefing_pack as bp
    from src.data import yahoo_client as yc

    closes = [100.0 + i * 0.5 for i in range(300)]
    monkeypatch.setattr(yc, "get_price_history",
                        lambda *a, **k: pd.DataFrame({"Close": closes}))
    t = bp._proxy_technicals(None, "iFreeNEXT FANG+インデックス")
    assert t["is_proxy"] is True
    assert t["proxy_symbol"] == "^NYFANG"
    assert t["rsi14"] is not None


def test_proxy_technicals_return_none_for_unmapped(monkeypatch):
    from src.core.research import briefing_pack as bp

    assert bp._proxy_technicals(None, "名も無き投信") is None


# ---------------------------------------------------------------------------
# vault の再同期
# ---------------------------------------------------------------------------


def test_resync_restores_report_that_vanished_from_vault(tmp_path):
    from src.output.sync import resync_missing

    out = tmp_path / "output"
    vault = tmp_path / "vault"
    out.mkdir()
    vault.mkdir()
    (out / "週次PF分析_20260801.md").write_text("本文", encoding="utf-8")
    (out / "週次PF分析_20260730.md").write_text("本文", encoding="utf-8")
    (vault / "週次PF分析_20260730.md").write_text("本文", encoding="utf-8")

    r = resync_missing(output_dir=str(out), vault_path=str(vault))
    assert r["restored"] == ["週次PF分析_20260801.md"]
    assert os.path.exists(vault / "週次PF分析_20260801.md")


def test_resync_is_a_noop_when_everything_is_present(tmp_path):
    from src.output.sync import resync_missing

    out = tmp_path / "output"
    vault = tmp_path / "vault"
    out.mkdir()
    vault.mkdir()
    (out / "週次PF分析_20260801.md").write_text("本文", encoding="utf-8")
    (vault / "週次PF分析_20260801.md").write_text("本文", encoding="utf-8")

    r = resync_missing(output_dir=str(out), vault_path=str(vault))
    assert r["restored"] == []
    assert r["checked"] == 1


def test_resync_skips_gracefully_without_vault(tmp_path):
    from src.output.sync import resync_missing

    out = tmp_path / "output"
    out.mkdir()
    r = resync_missing(output_dir=str(out), vault_path=str(tmp_path / "missing"))
    assert r["available"] is False
    assert r["restored"] == []
