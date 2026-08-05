"""縮退まわりのカオス: moomoo落ち / 全項目取得失敗 / 決算空 / vault消失 (改善7).

| テスト | 壊し方 | 期待される検出 |
|:---|:---|:---|
| TestMoomooDown | `futu-api` 未導入を再現 | マクロが**退避キャッシュ**に切替、`cached_age_hours` を明示 |
| TestAllUnavailable | 全項目 `available=false` | 「問題なし」ではなく**「判定不能」** |
| TestEarningsNone | 決算日が空リスト | `no_earnings` / `unavailable` を区別 |
| TestVaultDeleted | 同期後に vault から消す | `resync_missing()` が翌回に検出 |

**8/2 に直した穴9件のうち6件が「単一の取得元に依存していて、その取得元が実態と
食い違っていた」という同じ形だった。** 再発したらここで捕まる。
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone

import pytest

from src.core.risk import forward_events as FE
from src.output.sync import resync_missing


# ---------------------------------------------------------------------------
# moomoo が落ちる → 退避キャッシュに切り替わり、鮮度を明示する
# ---------------------------------------------------------------------------


@pytest.fixture
def macro_cache(tmp_path, monkeypatch):
    """マクロ退避キャッシュを tmp に隔離する。実データを壊さない。"""
    path = tmp_path / "macro_cache.json"
    monkeypatch.setattr(FE, "_macro_cache_path", lambda: str(path))
    return path


class TestMoomooDown:
    """moomoo が落ちた週に FOMC が黙って消えるのを防げているか。

    以前は moomoo だけを見ていたため、**落ちた週は FOMC がカレンダーから
    黙って消えていた**。マクロは保有全体に効くので影響が大きい。
    """

    def _live_moomoo(self, meeting: str) -> dict:
        return {
            "fed_watch": {"next_meeting": meeting, "top_range": "3.75-4.00",
                          "top_prob": 0.82},
            "economic_events": [
                {"date": meeting, "title": "米雇用統計", "country": "US", "star": 3},
            ],
        }

    def test_macro_survives_when_moomoo_dies(self, macro_cache):
        start = date(2026, 8, 3)
        end = date(2026, 8, 9)
        meeting = "2026-08-05"

        # 1週目: moomoo が生きている → 退避される
        live = FE._macro_events(self._live_moomoo(meeting), start, end)
        assert live, "生きている週にマクロが取れていない"
        assert macro_cache.exists(), "取得できた週に退避していない"

        # 2週目: futu-api 未導入で moomoo が空 → 退避から復元されるべき
        dead = FE._macro_events({}, start, end)
        assert dead, "moomoo が落ちた週に FOMC が黙って消えた（8/1 の再発）"
        kinds = {e["kind"] for e in dead}
        assert "fomc" in kinds

    def test_cached_events_disclose_their_age(self, macro_cache):
        start, end = date(2026, 8, 3), date(2026, 8, 9)
        FE._macro_events(self._live_moomoo("2026-08-05"), start, end)

        dead = FE._macro_events({}, start, end)
        for event in dead:
            assert "cached" in str(event["source"]), \
                "退避を使ったのに source が最新のふりをしている"
            assert event["cached_age_hours"] is not None, \
                "鮮度を伏せている（最新であるかのように読まれる）"

    def test_live_events_are_not_marked_cached(self, macro_cache):
        start, end = date(2026, 8, 3), date(2026, 8, 9)
        live = FE._macro_events(self._live_moomoo("2026-08-05"), start, end)
        for event in live:
            assert "cached" not in str(event["source"])
            assert event["cached_age_hours"] is None

    def test_stale_cache_is_dropped_not_served_as_fresh(self, macro_cache):
        """古すぎる退避は使わない。先週の終値のつもりで先々週を見る事故を防ぐ。"""
        old = datetime.now(timezone.utc) - timedelta(
            hours=FE.MACRO_CACHE_MAX_AGE_HOURS + 24)
        macro_cache.write_text(json.dumps({
            "fetched_at": old.isoformat(),
            "economic_events": [{"date": "2026-08-05", "title": "米雇用統計"}],
            "fed_watch": {"next_meeting": "2026-08-05"},
        }), encoding="utf-8")

        assert FE.load_macro_cache() is None
        assert FE._macro_events({}, date(2026, 8, 3), date(2026, 8, 9)) == []

    def test_empty_moomoo_does_not_overwrite_a_good_cache(self, macro_cache):
        start, end = date(2026, 8, 3), date(2026, 8, 9)
        FE._macro_events(self._live_moomoo("2026-08-05"), start, end)
        before = macro_cache.read_text(encoding="utf-8")

        FE._macro_events({}, start, end)          # 落ちた週
        FE._macro_events(None, start, end)        # moomoo 無効の週

        assert macro_cache.read_text(encoding="utf-8") == before, \
            "落ちた週の空データで退避を潰している"


# ---------------------------------------------------------------------------
# 全項目 available=false → 「問題なし」ではなく「判定不能」
# ---------------------------------------------------------------------------


class TestAllUnavailable:
    """取得できなかったものを結果として書かないか (§16-1)。

    ここは `tests/synthesis/assertions.py` の検査そのものを、
    **全項目が落ちた極限**で走らせる。
    """

    def _pack(self) -> dict:
        root = os.path.join(os.path.dirname(__file__), "..", "synthesis",
                            "fixtures", "pack_unavailable.json")
        with open(os.path.abspath(root), encoding="utf-8") as f:
            return json.load(f)

    def test_no_problem_wording_is_rejected(self):
        from tests.synthesis import assertions as A

        pack = self._pack()
        text = ("## 7. 監査\n\n"
                "- 決定生存率: 執行率0%。\n"
                "- 模型健全性: 問題なしです。\n"
                "- トリガー: 接近・成立ともになし。\n")
        res = A.check_no_unavailable_as_zero(text, pack)
        assert res["status"] == A.FAIL
        assert res["evidence"]

    def test_undeterminable_wording_is_accepted(self):
        from tests.synthesis import assertions as A

        pack = self._pack()
        text = ("## 7. 監査\n\n"
                "- 決定生存率: **取得できませんでした**（執行率0%ではなく測れていない）。\n"
                "- 模型健全性: データ蓄積中（4/26週）。\n"
                "- トリガー: 株価が取れず**判定不能**。\n")
        assert A.check_no_unavailable_as_zero(text, pack)["status"] == A.PASS

    def test_every_unavailable_subject_is_nameable(self):
        """取得失敗の項目に本文での呼び名が付いているか。

        呼び名が無い項目は検査対象から漏れる＝守られていない。
        極限ケースで漏れが増えていないかを見る。
        """
        from tests.synthesis import assertions as A

        subjects = A.unavailable_subjects(self._pack())
        assert len(subjects) >= 6, "取得失敗の検出が減っている（検査が空振りする）"
        assert all(s["aliases"] for s in subjects)


# ---------------------------------------------------------------------------
# 決算日が空 → no_earnings と unavailable を区別する
# ---------------------------------------------------------------------------


class TestEarningsNone:
    """空リストは状態ではない。ETFの「決算が無い」と「取得できなかった」は別。"""

    HOLDINGS = [
        {"symbol": "2802.T", "name": "味の素", "weight_pct": 10.9},
        {"symbol": "SOXL", "name": "Direxion デイリー 半導体株 ブル 3倍 ETF",
         "weight_pct": 24.9, "leverage": 3},
    ]

    def test_etf_with_no_earnings_is_not_unavailable(self):
        events = {
            "2802.T": {"available": True, "source": "yfinance", "earnings_dates": []},
            "SOXL": {"available": True, "source": "yfinance", "earnings_dates": []},
        }
        status = FE.symbol_schedule_status(
            self.HOLDINGS, as_of=date(2026, 8, 1), events_by_symbol=events)

        assert status["SOXL"]["status"] == "no_earnings", \
            "ETF の『決算が無い』を『取得できなかった』と書いている"
        assert status["2802.T"]["status"] == "none_upcoming", \
            "取得成功で予定なしを、取得失敗と混同している"

    def test_fetch_failure_is_marked_unavailable(self):
        events = {
            "2802.T": {"available": False, "error": "yfinance 応答なし"},
            "SOXL": {"available": False, "error": "yfinance 応答なし"},
        }
        status = FE.symbol_schedule_status(
            self.HOLDINGS, as_of=date(2026, 8, 1), events_by_symbol=events)
        # ETF は「決算が無い」が性質なので、取得失敗でも no_earnings で正しい
        assert status["2802.T"]["status"] == "unavailable"
        assert "取得" in (status["2802.T"]["label"] or "")

    def test_all_four_states_are_distinguishable(self):
        assert set(FE.SCHEDULE_STATES) == {
            "scheduled", "none_upcoming", "no_earnings", "unavailable"}


# ---------------------------------------------------------------------------
# vault からファイルが消える → 翌回に検出して戻す
# ---------------------------------------------------------------------------


class TestVaultDeleted:
    """8/1 のレポートは同期ログ上「成功」だったのに翌日には vault から消えていた。

    書いた直後の検証だけでは足りない。**過去に届けたはずのものが今もあるか**を
    毎回見る必要がある。
    """

    def _setup(self, tmp_path):
        out = tmp_path / "output"
        vault = tmp_path / "vault"
        out.mkdir()
        vault.mkdir()
        name = "週次PF分析_20260801.md"
        (out / name).write_text("---\ntitle: t\n---\n\n# 週次\n", encoding="utf-8")
        (vault / name).write_text("---\ntitle: t\n---\n\n# 週次\n", encoding="utf-8")
        return out, vault, name

    def test_deleted_report_is_restored_next_run(self, tmp_path):
        out, vault, name = self._setup(tmp_path)
        (vault / name).unlink()          # iCloud が消した、を再現

        result = resync_missing(output_dir=str(out), vault_path=str(vault))

        assert result["available"] is True
        assert name in result["restored"], "vault から消えたレポートを検出していない"
        assert (vault / name).exists()
        assert any("復元" in m for m in result["messages"])

    def test_intact_vault_restores_nothing(self, tmp_path):
        out, vault, _name = self._setup(tmp_path)
        result = resync_missing(output_dir=str(out), vault_path=str(vault))
        assert result["restored"] == []
        assert result["checked"] == 1

    def test_missing_vault_is_reported_not_silently_ok(self, tmp_path):
        out, _vault, _name = self._setup(tmp_path)
        result = resync_missing(output_dir=str(out),
                                vault_path=str(tmp_path / "does_not_exist"))
        # vault が無いことを「同期済み」と読ませない
        assert result["available"] is False
        assert result["messages"]
