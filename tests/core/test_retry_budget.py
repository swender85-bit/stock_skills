"""再試行の予算は「回数」ではなく「時刻」である（H6）。

2026-08-13 の実測: `You've hit your session limit · resets 1:10am (Asia/Tokyo)` が
`LIMIT_MARKERS` のどれにも一致せず「中断」と判定されなかったため、
**残り12節を1つずつ即失敗で消費して打ち切った**。

待てば必ず成功する失敗を、節ごとの再試行回数で焼き切っていた。
"""
import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "weekly_deep_driver", REPO / "scripts" / "weekly_deep_driver.py")
driver = importlib.util.module_from_spec(spec)
sys.modules["weekly_deep_driver"] = driver
spec.loader.exec_module(driver)

REAL_MESSAGE = "You've hit your session limit · resets 1:10am (Asia/Tokyo)"


class TestLimitDetection:
    def test_the_actual_message_is_recognised(self):
        """この1件が漏れていたために12節が失われた。"""
        assert driver.looks_like_limit(REAL_MESSAGE) is True

    @pytest.mark.parametrize("msg", [
        "usage limit reached",
        "You've hit your session limit",
        "rate limit exceeded",
        "429 too many requests",
        "使用量の上限に達しました",
        "credit balance is too low",
    ])
    def test_recoverable_failures(self, msg):
        assert driver.looks_like_limit(msg) is True

    @pytest.mark.parametrize("msg", [
        "SyntaxError: invalid syntax",
        "claude CLI が見つかりません",
        "空の応答",
    ])
    def test_permanent_failures_are_not_limits(self, msg):
        assert driver.looks_like_limit(msg) is False


class TestResetParsing:
    def test_parses_am_time(self):
        now = datetime(2026, 8, 13, 22, 0).astimezone()
        got = driver.parse_reset_at(REAL_MESSAGE, now=now)
        assert got is not None
        assert (got.hour, got.minute) == (1, 10)
        assert got.date() == (now + timedelta(days=1)).date()   # 深夜は翌日

    def test_parses_pm_time(self):
        now = datetime(2026, 8, 13, 9, 0).astimezone()
        got = driver.parse_reset_at("resets 12:10pm", now=now)
        assert (got.hour, got.minute) == (12, 10)
        assert got.date() == now.date()

    def test_parses_24h_time(self):
        now = datetime(2026, 8, 13, 9, 0).astimezone()
        got = driver.parse_reset_at("resets at 13:05", now=now)
        assert (got.hour, got.minute) == (13, 5)

    def test_returns_none_when_absent(self):
        assert driver.parse_reset_at("SyntaxError") is None

    def test_rejects_nonsense_hours(self):
        assert driver.parse_reset_at("resets 99:99") is None

    def test_the_2026_08_08_case(self):
        """07:12 起動・12:10 解除。2回の再試行で焼き切ってはならない。"""
        now = datetime(2026, 8, 8, 7, 12).astimezone()
        got = driver.parse_reset_at("resets 12:10pm (Asia/Tokyo)", now=now)
        assert got.hour == 12 and got.minute == 10
        assert got > now


class TestBudgetSemantics:
    """上限失敗は attempts を消費しない、という不変条件をコード上で縛る。"""

    def test_limit_failure_keeps_section_pending(self):
        src = (REPO / "scripts" / "weekly_deep_driver.py").read_text(encoding="utf-8")
        # interrupted 分岐で status を pending に戻し、attempts を触らない
        block = src[src.find('if res.get("interrupted"):'):]
        block = block[:block.find("# 待っても直らない失敗")]
        assert 's["status"] = "pending"' in block
        assert "attempts" not in block

    def test_attempts_counted_only_for_permanent_failures(self):
        src = (REPO / "scripts" / "weekly_deep_driver.py").read_text(encoding="utf-8")
        assert '# 待っても直らない失敗だけを回数で数える' in src
        idx = src.find("# 待っても直らない失敗だけを回数で数える")
        after = src[idx:idx + 300]
        assert 's["attempts"] = s.get("attempts", 0) + 1' in after

    def test_retry_after_is_recorded_in_state(self):
        src = (REPO / "scripts" / "weekly_deep_driver.py").read_text(encoding="utf-8")
        assert 'state["retry_after"] = reset_at.isoformat()' in src

    def test_startup_waits_until_reset(self):
        src = (REPO / "scripts" / "weekly_deep_driver.py").read_text(encoding="utf-8")
        assert 'state.get("retry_after")' in src
        assert "今回は書きません" in src
