"""欠測週の正しい振る舞い（診断後修理 Phase C-3）と、欠落節の告知（H4）。"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "weekly_deep_driver", REPO / "scripts" / "weekly_deep_driver.py")
driver = importlib.util.module_from_spec(spec)
sys.modules["weekly_deep_driver"] = driver
spec.loader.exec_module(driver)


LOW_QUALITY = {
    "price_coverage": 0.1, "priced": 1, "total": 10,
    "missing": ["2737.T", "2802.T", "9843.T"],
    "reasons": {"2737.T": "possibly delisted"},
    "usable": False, "min_required": 0.7,
    "verdict": "🔴 価格カバレッジ 10%（1/10）。",
}

PACK = {
    "meta": {"as_of": "2026-08-08",
             "network": {"ready": False, "message": "未接続のまま取得を開始"},
             "timings_sec": {"prices_and_holdings": 17.4, "narrative": 135.9}},
    "portfolio": {"price_failures": [{"id": "QCOM", "reason": "possibly delisted"}]},
}


class TestAbortReport:
    def test_says_it_did_not_analyze(self):
        text = driver.build_abort_report(PACK, LOW_QUALITY, "20260808")
        assert "分析は行っていません" in text
        assert "一切行っていません" in text

    def test_never_calls_unavailable_data_absent(self):
        text = driver.build_abort_report(PACK, LOW_QUALITY, "20260808")
        assert "取りに行けなかった" in text
        assert "材料が無かった" not in text.replace("でも「材料が無かった」でもありません", "")

    def test_names_what_was_missing_and_where_it_fell(self):
        text = driver.build_abort_report(PACK, LOW_QUALITY, "20260808")
        assert "2737.T" in text
        assert "possibly delisted" in text
        assert "ready=False" in text

    def test_gives_the_next_command(self):
        text = driver.build_abort_report(PACK, LOW_QUALITY, "20260808")
        assert "--resume-only" in text

    def test_stays_short(self):
        """770行の欠測報告を出さない。上限は設定ファイル側の値に従う。"""
        from src.core._thresholds import th

        limit = th("data_quality", "abort_report_max_lines", 80)
        text = driver.build_abort_report(PACK, LOW_QUALITY, "20260808")
        assert len(text.splitlines()) <= limit

    def test_threshold_is_not_hardcoded(self):
        from src.core._thresholds import get_thresholds

        assert "data_quality" in get_thresholds()
        assert "min_price_coverage" in get_thresholds()["data_quality"]


class TestMissingSections:
    def _state(self, tmp_path):
        (tmp_path / "s1.md").write_text("節1の本文", encoding="utf-8")
        return {"sections": [
            {"order": 1, "heading": "## 1. 照合", "status": "done", "file": "s1.md"},
            {"order": 2, "heading": "## 7. 監査", "status": "failed",
             "attempts": 2, "error": "usage limit"},
            {"order": 3, "heading": "## 8. 限界", "status": "pending", "attempts": 0},
        ]}

    def test_failed_sections_are_listed_not_dropped(self, tmp_path):
        missing = driver.missing_sections(self._state(tmp_path), tmp_path)
        heads = [m["heading"] for m in missing]
        assert "7. 監査" in heads and "8. 限界" in heads
        assert "1. 照合" not in heads

    def test_block_states_they_were_not_written(self, tmp_path):
        block = driver.build_missing_block(
            driver.missing_sections(self._state(tmp_path), tmp_path))
        assert "「該当なし」ではありません" in block
        assert "usage limit" in block

    def test_assembled_report_carries_the_warning(self, tmp_path):
        report = driver.assemble(self._state(tmp_path), tmp_path,
                                 {"meta": {"as_of": "2026-08-08"}, "portfolio": {}})
        assert "このレポートには欠落があります" in report
        assert "節1の本文" in report

    def test_complete_report_has_no_warning(self, tmp_path):
        (tmp_path / "s1.md").write_text("本文", encoding="utf-8")
        state = {"sections": [{"order": 1, "heading": "## 1. 照合",
                               "status": "done", "file": "s1.md"}]}
        report = driver.assemble(state, tmp_path,
                                 {"meta": {"as_of": "2026-08-08"}, "portfolio": {}})
        assert "欠落があります" not in report


class TestHeaderDisclosure:
    def test_partial_total_is_annotated(self):
        pack = {"meta": {"as_of": "2026-08-08"},
                "portfolio": {"total_jpy": 1386357.9, "cash_jpy": 9953.0,
                              "total_pl_jpy": 1000.0, "pl_pct": None,
                              "pl_pct_suppressed": True,
                              "pl_pct_suppressed_reason": "分子と分母の母集団が一致しません",
                              "coverage": {"total": 10, "observed": 1, "complete": False,
                                           "note": "（10件中 1件のみ評価・残り 9件は取得不可）"}}}
        header = driver.build_header(pack)
        assert "10件中 1件のみ評価" in header
        assert "損益率は非表示" in header

    def test_complete_total_is_not_annotated(self):
        pack = {"meta": {"as_of": "2026-08-09"},
                "portfolio": {"total_jpy": 22157221.8, "cash_jpy": 9953.0,
                              "total_pl_jpy": 9000000.0, "pl_pct": 75.7,
                              "pl_pct_suppressed": False,
                              "coverage": {"total": 9, "observed": 9, "complete": True,
                                           "note": ""}}}
        header = driver.build_header(pack)
        assert "のみ評価" not in header
        assert "75.7%" in header
        assert "損益率は非表示" not in header


class _Args:
    """`_run` に渡す引数。argparse の既定値と同じ形にする。"""

    def __init__(self, out_dir, **kw):
        self.pack = None
        self.out_dir = str(out_dir)
        self.model = "opus"
        self.timeout = 900
        self.max_sections = 0
        self.max_attempts = 2
        self.restart = False
        self.resume_within_days = 3
        self.resume_only = False
        self.dry_run = True
        self.no_moomoo = True
        self.no_critics = True
        self.critic_days = 7
        self.force_low_quality = False
        for k, v in kw.items():
            setattr(self, k, v)


class TestAbortLeavesResumableState:
    """H5 — 打ち切ったまま誰も気づかない、を再発させない。"""

    def _low_quality_pack(self, tmp_path):
        pack = {
            "meta": {"as_of": "2026-08-08",
                     "network": {"ready": False, "message": "未接続"},
                     "data_quality": dict(LOW_QUALITY)},
            "portfolio": {"price_failures": []},
            "holdings": [],
        }
        p = tmp_path / "PF_low.json"
        p.write_text(__import__("json").dumps(pack, ensure_ascii=False), encoding="utf-8")
        return p

    def test_state_is_saved_before_returning(self, tmp_path, monkeypatch):
        pack_path = self._low_quality_pack(tmp_path)
        monkeypatch.setattr(driver, "ensure_pack", lambda *a, **k: pack_path)

        code = driver._run(_Args(tmp_path))

        assert code == driver.EXIT_INTERRUPTED
        states = list(tmp_path.glob("state_*.json"))
        assert states, "打ち切ったのに state が残っていない（再開タスクが拾えない）"
        saved = __import__("json").loads(states[0].read_text(encoding="utf-8"))
        assert saved["aborted_low_quality"] is True
        assert saved["finished"] is False

    def test_resume_only_picks_up_the_abort(self, tmp_path, monkeypatch):
        """--resume-only が『何もしません』で終わらないこと。"""
        pack_path = self._low_quality_pack(tmp_path)
        monkeypatch.setattr(driver, "ensure_pack", lambda *a, **k: pack_path)
        driver._run(_Args(tmp_path))

        calls = []

        def _ensure(*a, **k):
            calls.append(1)
            return pack_path

        monkeypatch.setattr(driver, "ensure_pack", _ensure)
        code = driver._run(_Args(tmp_path, resume_only=True))

        assert calls, "--resume-only がパックを作り直していない（打ち切りが放置される）"
        assert code == driver.EXIT_INTERRUPTED
