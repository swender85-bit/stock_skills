"""取得の一時失敗を確定にしない -- 2026-08-08 の週次全滅の再発防止.

## 何が起きたか

週次レポートが**10銘柄中9銘柄の価格 null** で生成・保存された。
全節が「取得できず」で埋まった状態で vault に届いた。

原因はレート制限でも yfinance の仕様変更でもなく、
**PC がスタンバイから起こされた直後（WakeToRun）にネットワークが
繋がっていなかった**こと。パックの実測タイミングが証拠:

    prices_and_holdings   17.4s  → 価格 全滅・FX もフォールバック(157.81)
    news                   1.1s  → 「取得できませんでした」
    narrative            135.9s  → **成功**（finnhub 記事数が取れている）

**前半が全滅し、後半が成功している。** 通信は後から復旧していた。
`get_stock_info()` にリトライが1回も無かったため、その一瞬の失敗が
「今週の価格は取得不能」として確定した。

## ここで縛っていること

1. **一時失敗（通信断・タイムアウト・レート制限）はリトライする**
2. **空の応答もリトライする** — yfinance は通信断で例外ではなく空を返す。
   例外だけ見ていると 8/8 の失敗は捕まらない
3. **恒久的失敗（上場廃止等）はリトライしない** — 待っても直らない
4. **取得を始める前にネットワーク復帰を待つ**
5. **価格が取れていないパックからレポートを書かせない**
"""

from __future__ import annotations

import socket

import pytest

from src.data.yahoo_client import _net


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    """テストを待たせない。"""
    monkeypatch.setattr(_net.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(_net, "network_available", lambda *a, **k: True)


# ---------------------------------------------------------------------------
# 一時失敗 / 恒久失敗の区別
# ---------------------------------------------------------------------------


class TestTransientClassification:
    @pytest.mark.parametrize("exc", [
        TimeoutError("timed out"),
        socket.timeout("timed out"),
        ConnectionError("connection reset by peer"),
        OSError("getaddrinfo failed"),
        RuntimeError("HTTP 429 Too Many Requests"),
        RuntimeError("Max retries exceeded"),
        RuntimeError("503 Service Unavailable"),
    ])
    def test_transient_failures_are_detected(self, exc):
        assert _net.is_transient(exc) is True

    @pytest.mark.parametrize("exc", [
        ValueError("symbol may be delisted"),
        KeyError("regularMarketPrice"),
        TypeError("bad argument"),
    ])
    def test_permanent_failures_are_not_retried(self, exc):
        assert _net.is_transient(exc) is False


# ---------------------------------------------------------------------------
# リトライ
# ---------------------------------------------------------------------------


class TestWithRetry:
    def test_transient_failure_is_retried_until_success(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise TimeoutError("timed out")
            return {"ok": True}

        result, error = _net.with_retry(flaky, attempts=4, backoff=0)
        assert result == {"ok": True}
        assert error is None
        assert calls["n"] == 3

    def test_empty_response_is_retried(self):
        """**8/8 の症状そのもの。** 通信断では例外ではなく空が返る。

        例外だけを再試行対象にしていると、この失敗は捕まらない。
        """
        calls = {"n": 0}

        def empty_then_ok():
            calls["n"] += 1
            return {} if calls["n"] < 2 else {"regularMarketPrice": 100}

        result, error = _net.with_retry(
            empty_then_ok, attempts=3, backoff=0,
            is_empty=lambda d: not d or d.get("regularMarketPrice") is None)
        assert result == {"regularMarketPrice": 100}
        assert calls["n"] == 2

    def test_permanent_failure_gives_up_immediately(self):
        calls = {"n": 0}

        def permanent():
            calls["n"] += 1
            raise ValueError("symbol may be delisted")

        result, error = _net.with_retry(permanent, attempts=5, backoff=0)
        assert result is None
        assert "delisted" in error
        assert calls["n"] == 1, "待っても直らない失敗を再試行して時間を無駄にしている"

    def test_exhausted_retries_return_the_reason(self):
        result, error = _net.with_retry(
            lambda: (_ for _ in ()).throw(TimeoutError("timed out")),
            attempts=3, backoff=0)
        assert result is None
        # 理由を握り潰さない（「データが無い」と誤読させないため）
        assert "timed out" in error.lower()

    def test_persistent_empty_returns_reason(self):
        result, error = _net.with_retry(
            lambda: {}, attempts=2, backoff=0, is_empty=lambda d: not d)
        assert result is None
        assert "空の応答" in error


# ---------------------------------------------------------------------------
# ネットワーク待ち
# ---------------------------------------------------------------------------


class TestWaitForNetwork:
    def test_returns_ready_when_connected(self, monkeypatch):
        monkeypatch.setattr(_net, "network_available", lambda *a, **k: True)
        result = _net.wait_for_network(max_wait=1)
        assert result["ready"] is True

    def test_waits_then_succeeds(self, monkeypatch):
        calls = {"n": 0}

        def flaky(*_a, **_k):
            calls["n"] += 1
            return calls["n"] >= 3

        monkeypatch.setattr(_net, "network_available", flaky)
        result = _net.wait_for_network(max_wait=30, quiet=True)
        assert result["ready"] is True
        assert calls["n"] >= 3

    def test_timeout_says_it_could_not_reach_not_no_data(self, monkeypatch):
        monkeypatch.setattr(_net, "network_available", lambda *a, **k: False)
        result = _net.wait_for_network(max_wait=0.01, quiet=True)
        assert result["ready"] is False
        # 「データが無い」ではなく「取りに行けなかった」と書けること
        assert "取りに行けなかった" in result["message"]


# ---------------------------------------------------------------------------
# 価格カバレッジのゲート
# ---------------------------------------------------------------------------


class TestDataQualityGate:
    def _rows(self, priced: int, missing: int):
        rows = [{"symbol": f"OK{i}", "price": 100.0} for i in range(priced)]
        rows += [{"symbol": f"NG{i}", "price": None} for i in range(missing)]
        return rows

    def test_full_coverage_is_usable(self):
        from src.core.research.briefing_pack import _data_quality

        q = _data_quality(self._rows(9, 0))
        assert q["usable"] is True
        assert q["price_coverage"] == 1.0

    def test_the_2026_08_08_state_is_rejected(self):
        """10銘柄中9銘柄が null。**この状態でレポートを書かせない。**"""
        from src.core.research.briefing_pack import _data_quality

        q = _data_quality(self._rows(1, 9))
        assert q["usable"] is False
        assert q["price_coverage"] == pytest.approx(0.1)
        assert "レポートを書ける状態ではありません" in q["verdict"]
        # 取得できなかった銘柄を名指しする
        assert len(q["missing"]) == 9

    def test_verdict_says_not_fetched_rather_than_no_movement(self):
        from src.core.research.briefing_pack import _data_quality

        q = _data_quality(self._rows(0, 5))
        assert "値動きが無かった" in q["verdict"]   # 「ではなく」と否定している
        assert "取りに行けなかった" in q["verdict"]

    def test_network_failure_is_included_in_the_verdict(self):
        from src.core.research.briefing_pack import _data_quality

        q = _data_quality(self._rows(0, 3),
                          {"ready": False, "message": "ネットワークに繋がりませんでした"})
        assert "ネットワーク" in q["verdict"]

    def test_borderline_coverage(self):
        from src.core.research.briefing_pack import MIN_PRICE_COVERAGE, _data_quality

        assert MIN_PRICE_COVERAGE == 0.7
        assert _data_quality(self._rows(7, 3))["usable"] is True
        assert _data_quality(self._rows(6, 4))["usable"] is False

    def test_empty_holdings_do_not_crash(self):
        from src.core.research.briefing_pack import _data_quality

        q = _data_quality([])
        assert q["total"] == 0
        assert q["usable"] is False


# ---------------------------------------------------------------------------
# 失敗理由の記録
# ---------------------------------------------------------------------------


def test_fetch_error_is_recorded_and_readable():
    """price=None / error=None という 8/8 の状態を繰り返さない。

    理由が残らないと、レポートは「取得できず」としか書けず、
    原因（ネットワーク断なのか、上場廃止なのか）を利用者に伝えられない。
    """
    from src.data.yahoo_client.detail import (
        _record_fetch_error,
        clear_fetch_errors,
        last_fetch_error,
    )

    clear_fetch_errors()
    assert last_fetch_error("QCOM") is None
    _record_fetch_error("QCOM", "TimeoutError: timed out")
    assert "timed out" in last_fetch_error("QCOM")
    clear_fetch_errors()
    assert last_fetch_error("QCOM") is None
