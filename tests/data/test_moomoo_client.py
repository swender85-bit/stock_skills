"""Tests for src/data/moomoo_client.py (OpenD 経由の気配・指数補完)."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.data import moomoo_client as mc


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    mc.reset_state()
    for var in ("MOOMOO_ENABLED", "MOOMOO_OPEND_HOST", "MOOMOO_OPEND_PORT"):
        monkeypatch.delenv(var, raising=False)
    # 実ソケットを一切開かせない（実 OpenD の有無でテストがブレないように）。
    # _opend_reachable ではなく socket 層を塞ぐので、到達判定自体もテストできる。
    def _refuse(addr, timeout=None):
        raise OSError("blocked in tests")

    monkeypatch.setattr(mc.socket, "create_connection", _refuse)
    yield
    mc.reset_state()


class FakeSDK:
    """moomoo SDK の最小スタブ。"""

    RET_OK = 0

    def __init__(self, ret=0, rows=None, raise_on_init=None):
        self._ret = ret
        self._rows = rows if rows is not None else []
        self._raise_on_init = raise_on_init
        self.closed = False
        self.requested_codes = None

    # SDK の OpenQuoteContext(host=..., port=...) を模す
    def OpenQuoteContext(self, host=None, port=None):
        if self._raise_on_init:
            raise self._raise_on_init
        self.host, self.port = host, port
        return self

    def get_market_snapshot(self, codes):
        self.requested_codes = list(codes)
        if self._ret != self.RET_OK:
            return self._ret, "snapshot failed"
        return self.RET_OK, _FakeFrame(self._rows)

    def close(self):
        self.closed = True


class _FakeFrame:
    def __init__(self, rows):
        self._rows = rows

    def to_dict(self, orient):
        assert orient == "records"
        return self._rows


def _row(code, last=100.0, prev=98.0, high=101.0, low=97.0):
    return {
        "code": code, "last_price": last, "prev_close_price": prev,
        "high_price": high, "low_price": low,
    }


# ---------------------------------------------------------------------------
# is_enabled / is_available
# ---------------------------------------------------------------------------

class TestIsEnabled:
    @pytest.mark.parametrize("value", ["on", "1", "true", "TRUE", "yes"])
    def test_truthy_values(self, monkeypatch, value):
        monkeypatch.setenv("MOOMOO_ENABLED", value)
        assert mc.is_enabled() is True

    @pytest.mark.parametrize("value", ["", "off", "0", "false", "no"])
    def test_falsy_values(self, monkeypatch, value):
        monkeypatch.setenv("MOOMOO_ENABLED", value)
        assert mc.is_enabled() is False

    def test_default_is_disabled(self):
        """既定は opt-in。OpenD が落ちた環境で毎回接続を試みないため。"""
        assert mc.is_enabled() is False


class TestIsAvailable:
    def test_disabled_short_circuits(self, monkeypatch):
        called = []
        monkeypatch.setattr(mc, "_import_sdk", lambda: called.append(1))
        assert mc.is_available() is False
        assert mc.get_error_status()["status"] == "disabled"
        assert called == []  # SDK import すら試みない

    def test_no_sdk(self, monkeypatch):
        monkeypatch.setenv("MOOMOO_ENABLED", "on")
        monkeypatch.setattr(mc, "_import_sdk", lambda: None)
        assert mc.is_available() is False
        assert mc.get_error_status()["status"] == "no_sdk"

    def test_opend_unreachable(self, monkeypatch):
        monkeypatch.setenv("MOOMOO_ENABLED", "on")
        monkeypatch.setattr(mc, "_import_sdk", lambda: FakeSDK())
        monkeypatch.setattr(mc, "_opend_reachable", lambda: False)
        assert mc.is_available() is False
        st = mc.get_error_status()
        assert st["status"] == "opend_unreachable"
        assert "11111" in st["message"]

    def test_all_conditions_met(self, monkeypatch):
        monkeypatch.setenv("MOOMOO_ENABLED", "on")
        monkeypatch.setattr(mc, "_import_sdk", lambda: FakeSDK())
        monkeypatch.setattr(mc, "_opend_reachable", lambda: True)
        assert mc.is_available() is True
        assert mc.get_error_status()["status"] == "ok"


# ---------------------------------------------------------------------------
# エンドポイント
# ---------------------------------------------------------------------------

class TestGetEndpoint:
    def test_defaults(self):
        assert mc._get_endpoint() == ("127.0.0.1", 11111)

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("MOOMOO_OPEND_HOST", "192.168.1.5")
        monkeypatch.setenv("MOOMOO_OPEND_PORT", "22222")
        assert mc._get_endpoint() == ("192.168.1.5", 22222)

    def test_invalid_port_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("MOOMOO_OPEND_PORT", "not-a-port")
        assert mc._get_endpoint()[1] == 11111


class TestOpendReachable:
    def test_caches_probe_result(self, monkeypatch):
        calls = []

        def fake_connect(addr, timeout=None):
            calls.append(addr)
            raise OSError("refused")

        monkeypatch.setattr(mc.socket, "create_connection", fake_connect)
        assert mc._opend_reachable() is False
        assert mc._opend_reachable() is False
        assert len(calls) == 1  # 2回目はキャッシュ

    def test_probes_configured_endpoint(self, monkeypatch):
        monkeypatch.setenv("MOOMOO_OPEND_HOST", "10.0.0.9")
        monkeypatch.setenv("MOOMOO_OPEND_PORT", "12345")
        seen = []
        monkeypatch.setattr(
            mc.socket, "create_connection",
            lambda addr, timeout=None: seen.append(addr) or MagicMock(),
        )
        mc._opend_reachable()
        assert seen == [("10.0.0.9", 12345)]

    def test_successful_connection(self, monkeypatch):
        monkeypatch.setattr(mc.socket, "create_connection",
                            lambda addr, timeout=None: MagicMock())
        assert mc._opend_reachable() is True


# ---------------------------------------------------------------------------
# シンボル変換
# ---------------------------------------------------------------------------

class TestToMoomooSymbol:
    @pytest.mark.parametrize("yahoo,expected", [
        ("AAPL", "US.AAPL"),
        ("soxl", "US.SOXL"),
        ("7203.T", "JP.7203"),
        ("2802.t", "JP.2802"),
        ("0700.HK", "HK.0700"),
        ("D05.SI", "SG.D05"),
        ("600519.SS", "SH.600519"),
        ("^GSPC", "US.SPX"),
        ("^N225", "JP.800000"),
        ("^VIX", "US.VIX"),
    ])
    def test_conversions(self, yahoo, expected):
        assert mc.to_moomoo_symbol(yahoo) == expected

    def test_unknown_index_returns_none(self):
        """未知の指数は推測で組み立てず None にする（誤コードで引かないため）。"""
        assert mc.to_moomoo_symbol("^UNKNOWN") is None

    def test_unknown_suffix_returns_none(self):
        assert mc.to_moomoo_symbol("ABC.XYZ") is None

    def test_empty_returns_none(self):
        assert mc.to_moomoo_symbol("") is None
        assert mc.to_moomoo_symbol(None) is None


# ---------------------------------------------------------------------------
# get_quotes
# ---------------------------------------------------------------------------

def _enable(monkeypatch, sdk):
    monkeypatch.setenv("MOOMOO_ENABLED", "on")
    monkeypatch.setattr(mc, "_import_sdk", lambda: sdk)
    monkeypatch.setattr(mc, "_opend_reachable", lambda: True)


class TestGetQuotes:
    def test_returns_empty_when_unavailable(self, monkeypatch):
        assert mc.get_quotes(["AAPL"]) == {}

    def test_empty_symbols(self, monkeypatch):
        _enable(monkeypatch, FakeSDK())
        assert mc.get_quotes([]) == {}

    def test_maps_back_to_yahoo_symbols(self, monkeypatch):
        sdk = FakeSDK(rows=[_row("JP.7203", last=2900.0, prev=2850.0)])
        _enable(monkeypatch, sdk)
        out = mc.get_quotes(["7203.T"])
        assert set(out) == {"7203.T"}
        q = out["7203.T"]
        assert q["current"] == 2900.0
        assert q["prev_close"] == 2850.0
        assert q["change"] == pytest.approx(50.0)
        assert q["percent_change"] == pytest.approx(50 / 2850 * 100)

    def test_requests_converted_codes(self, monkeypatch):
        sdk = FakeSDK(rows=[])
        _enable(monkeypatch, sdk)
        mc.get_quotes(["AAPL", "7203.T"])
        assert sorted(sdk.requested_codes) == ["JP.7203", "US.AAPL"]

    def test_unconvertible_symbols_are_dropped(self, monkeypatch):
        sdk = FakeSDK(rows=[])
        _enable(monkeypatch, sdk)
        assert mc.get_quotes(["^UNKNOWN", "ABC.XYZ"]) == {}
        assert sdk.requested_codes is None  # SDK を呼ぶ前に打ち切る

    def test_all_zero_row_is_omitted(self, monkeypatch):
        """無効シンボルは全ゼロで返る。0 を現在値として返さない。"""
        sdk = FakeSDK(rows=[_row("US.AAPL", last=0, prev=0)])
        _enable(monkeypatch, sdk)
        assert mc.get_quotes(["AAPL"]) == {}

    def test_unknown_code_in_response_is_ignored(self, monkeypatch):
        sdk = FakeSDK(rows=[_row("US.MSFT")])
        _enable(monkeypatch, sdk)
        assert mc.get_quotes(["AAPL"]) == {}

    def test_ret_not_ok_returns_empty(self, monkeypatch):
        sdk = FakeSDK(ret=-1)
        _enable(monkeypatch, sdk)
        assert mc.get_quotes(["AAPL"]) == {}
        assert mc.get_error_status()["status"] == "other_error"

    def test_sdk_exception_is_swallowed(self, monkeypatch):
        sdk = FakeSDK(raise_on_init=RuntimeError("OpenD died"))
        _enable(monkeypatch, sdk)
        assert mc.get_quotes(["AAPL"]) == {}
        assert mc.get_error_status()["status"] == "other_error"
        assert "OpenD died" in mc.get_error_status()["message"]

    def test_context_is_closed(self, monkeypatch):
        sdk = FakeSDK(rows=[_row("US.AAPL")])
        _enable(monkeypatch, sdk)
        mc.get_quotes(["AAPL"])
        assert sdk.closed is True

    def test_missing_prev_close_leaves_change_none(self, monkeypatch):
        """騰落率が出せないときに 0% と返すと「動いていない」と誤読される。"""
        sdk = FakeSDK(rows=[_row("US.AAPL", last=100.0, prev=None)])
        _enable(monkeypatch, sdk)
        q = mc.get_quotes(["AAPL"])["AAPL"]
        assert q["current"] == 100.0
        assert q["change"] is None
        assert q["percent_change"] is None


class TestGetQuote:
    def test_single_symbol(self, monkeypatch):
        sdk = FakeSDK(rows=[_row("US.AAPL", last=250.0, prev=245.0)])
        _enable(monkeypatch, sdk)
        assert mc.get_quote("AAPL")["current"] == 250.0

    def test_unavailable_returns_none(self):
        assert mc.get_quote("AAPL") is None

    def test_empty_symbol_returns_none(self, monkeypatch):
        _enable(monkeypatch, FakeSDK())
        assert mc.get_quote("") is None
