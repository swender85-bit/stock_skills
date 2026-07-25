"""scripts/common.py の OS 非依存タイムアウトのテスト。

SIGALRM 版は Windows で「時間制限なし」に退化しており、Neo4j/TEI が落ちている
環境でスキルスクリプトが無限に固まっていた。その回帰を防ぐ。
"""

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.common import _run_with_timeout  # noqa: E402


class TestRunWithTimeout:
    def test_returns_value_when_fast(self):
        assert _run_with_timeout(lambda: "ok", 5) == "ok"

    def test_returns_default_when_slow(self):
        """遅い処理は待たずに諦める（呼び出し側は止まらない）。"""
        started = time.time()
        result = _run_with_timeout(lambda: time.sleep(10) or "遅い", 1,
                                   default="諦めた")
        assert result == "諦めた"
        assert time.time() - started < 5

    def test_slow_worker_is_daemon_so_process_can_exit(self):
        """超過したワーカーが残ってもプロセス終了を妨げないこと。"""
        before = {id(t) for t in threading.enumerate()}
        _run_with_timeout(lambda: time.sleep(5), 1)
        leaked = [t for t in threading.enumerate()
                  if id(t) not in before and t.name == "skill-timeout"]
        assert leaked and all(t.daemon for t in leaked)

    def test_exception_is_propagated(self):
        def boom():
            raise ValueError("失敗")

        with pytest.raises(ValueError, match="失敗"):
            _run_with_timeout(boom, 5)

    def test_none_result_is_kept_distinct_from_default(self):
        assert _run_with_timeout(lambda: None, 5, default="既定") is None

    def test_works_without_sigalrm(self, monkeypatch):
        """Windows（SIGALRM なし）でも時間制限が効くこと。"""
        import signal

        monkeypatch.delattr(signal, "SIGALRM", raising=False)
        assert _run_with_timeout(lambda: time.sleep(5), 1, default="停止") == "停止"
