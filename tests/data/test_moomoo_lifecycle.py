"""Tests for moomoo_client.ensure_opend (無人 OpenD ライフサイクル).

実際に OpenD.exe を起動しないよう、起動・待機・終了はすべてモックする。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.data import moomoo_client as mc


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    mc.reset_state()
    for var in ("MOOMOO_ENABLED", "MOOMOO_OPEND_PATH", "MOOMOO_OPEND_STARTUP_TIMEOUT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MOOMOO_OPEND_WARMUP", "0")  # テストでは実スリープを避ける
    yield
    mc.reset_state()


def _enable(monkeypatch):
    monkeypatch.setenv("MOOMOO_ENABLED", "on")


def test_disabled_yields_false_no_launch(monkeypatch):
    launched = []
    monkeypatch.setattr(mc, "_launch_opend", lambda exe: launched.append(exe))
    with mc.ensure_opend() as up:
        assert up is False
    assert launched == []


def test_already_reachable_does_not_launch_or_kill(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(mc, "_opend_reachable", lambda: True)
    launched, killed = [], []
    monkeypatch.setattr(mc, "_launch_opend", lambda exe: launched.append(exe))
    monkeypatch.setattr(mc, "_terminate_opend", lambda p: killed.append(p))
    with mc.ensure_opend() as up:
        assert up is True
    assert launched == [] and killed == []  # ユーザーの OpenD を尊重


def test_autostart_launches_waits_and_terminates(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(mc, "_opend_reachable", lambda: False)
    monkeypatch.setattr(mc, "_opend_exe_path", lambda: Path("OpenD.exe"))
    fake_proc = object()
    monkeypatch.setattr(mc, "_launch_opend", lambda exe: fake_proc)
    monkeypatch.setattr(mc, "_wait_reachable", lambda t: True)
    killed = []
    monkeypatch.setattr(mc, "_terminate_opend", lambda p: killed.append(p))
    with mc.ensure_opend() as up:
        assert up is True
        assert killed == []  # ブロック中はまだ落とさない
    assert killed == [fake_proc]  # 抜けたら自分が起動したものを落とす


def test_autostart_missing_exe_yields_false(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(mc, "_opend_reachable", lambda: False)
    monkeypatch.setattr(mc, "_opend_exe_path", lambda: None)
    launched = []
    monkeypatch.setattr(mc, "_launch_opend", lambda exe: launched.append(exe))
    with mc.ensure_opend() as up:
        assert up is False
    assert launched == []


def test_autostart_login_timeout_yields_false_still_terminates(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(mc, "_opend_reachable", lambda: False)
    monkeypatch.setattr(mc, "_opend_exe_path", lambda: Path("OpenD.exe"))
    fake_proc = object()
    monkeypatch.setattr(mc, "_launch_opend", lambda exe: fake_proc)
    monkeypatch.setattr(mc, "_wait_reachable", lambda t: False)  # ログイン失敗
    killed = []
    monkeypatch.setattr(mc, "_terminate_opend", lambda p: killed.append(p))
    with mc.ensure_opend() as up:
        assert up is False
    assert killed == [fake_proc]  # 起動した以上、必ず後始末する


def test_autostart_false_never_launches(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(mc, "_opend_reachable", lambda: False)
    launched = []
    monkeypatch.setattr(mc, "_launch_opend", lambda exe: launched.append(exe))
    with mc.ensure_opend(autostart=False) as up:
        assert up is False
    assert launched == []


def test_launch_failure_yields_false(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(mc, "_opend_reachable", lambda: False)
    monkeypatch.setattr(mc, "_opend_exe_path", lambda: Path("OpenD.exe"))
    monkeypatch.setattr(mc, "_launch_opend", lambda exe: None)  # 起動失敗
    with mc.ensure_opend() as up:
        assert up is False


def test_exe_path_env_override(monkeypatch, tmp_path):
    exe = tmp_path / "OpenD.exe"
    exe.write_text("x")
    monkeypatch.setenv("MOOMOO_OPEND_PATH", str(exe))
    assert mc._opend_exe_path() == exe


def test_exe_path_env_missing_file_returns_none(monkeypatch):
    monkeypatch.setenv("MOOMOO_OPEND_PATH", "C:/nope/OpenD.exe")
    assert mc._opend_exe_path() is None
