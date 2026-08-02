"""マクロイベントの退避 — moomoo が落ちた週に FOMC を消さない。

`_macro_events()` は moomoo だけを見ていたため、moomoo が一時的に落ちた週は
FOMC も経済指標も**黙ってカレンダーから消えていた**。ユーザーの手動起動でも
実際に `Login failed, Network error` は発生している。

「イベントが無い」と「取得できなかった」の混同そのもので、しかもマクロは
保有全体に効くので影響が大きい。

守るべき性質:
- 取得できた週に退避し、落ちた週はそれを使う
- 使ったことを隠さない（source に `(cached)`、経過時間も残す）
- 古すぎるキャッシュは使わない
- 退避が壊れていても本体は動く
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from src.core.risk import forward_events as fe

START = date(2026, 8, 3)
END = date(2026, 8, 7)

LIVE = {
    "economic_events": [
        {"date": "2026-08-05", "title": "ISM非製造業", "country": "US", "star": 3},
    ],
    "fed_watch": {"next_meeting": "2026-08-06", "top_range": "4.00-4.25",
                  "top_prob": "72%"},
}


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(fe, "_macro_cache_path",
                        lambda: str(tmp_path / "macro_events.json"))
    yield


# ---------------------------------------------------------------------------
# 通常時
# ---------------------------------------------------------------------------


def test_live_events_are_used_and_saved():
    out = fe._macro_events(LIVE, START, END)
    kinds = {e["kind"] for e in out}
    assert kinds == {"economic", "fomc"}
    assert all(e["source"] == "moomoo" for e in out)
    assert fe.load_macro_cache() is not None, "取れた週に退避しておく"


def test_events_outside_the_window_are_dropped():
    payload = {"economic_events": [{"date": "2026-09-01", "title": "遠い"}],
               "fed_watch": {"next_meeting": "2026-09-16"}}
    assert fe._macro_events(payload, START, END) == []


# ---------------------------------------------------------------------------
# moomoo が落ちた週
# ---------------------------------------------------------------------------


def test_fomc_survives_a_moomoo_outage():
    """これが本丸。落ちた週に FOMC が黙って消えていた。"""
    fe._macro_events(LIVE, START, END)          # 取れた週に退避
    out = fe._macro_events({}, START, END)      # 落ちた週
    assert any(e["kind"] == "fomc" for e in out)


def test_cached_events_are_labelled_as_cached():
    """鮮度を伏せたまま最新であるかのように見せない。"""
    fe._macro_events(LIVE, START, END)
    out = fe._macro_events(None, START, END)
    assert out and all(e["source"] == "moomoo(cached)" for e in out)
    assert all(e["cached_age_hours"] is not None for e in out)


def test_no_cache_and_no_moomoo_yields_nothing():
    assert fe._macro_events({}, START, END) == []


def test_stale_cache_is_not_used(monkeypatch, tmp_path):
    old = datetime.now(timezone.utc) - timedelta(
        hours=fe.MACRO_CACHE_MAX_AGE_HOURS + 1)
    (tmp_path / "macro_events.json").write_text(json.dumps({
        "fetched_at": old.isoformat(), **LIVE}), encoding="utf-8")
    assert fe.load_macro_cache() is None
    assert fe._macro_events({}, START, END) == []


def test_live_fetch_overwrites_the_cache():
    fe._macro_events(LIVE, START, END)
    newer = {"economic_events": [{"date": "2026-08-04", "title": "雇用統計"}],
             "fed_watch": {}}
    fe._macro_events(newer, START, END)
    cached = fe.load_macro_cache()
    assert cached["economic_events"][0]["title"] == "雇用統計"


def test_empty_payload_never_overwrites_a_good_cache():
    """落ちた週に空で上書きすると、翌週まで材料が消える。"""
    fe._macro_events(LIVE, START, END)
    fe._macro_events({}, START, END)
    assert fe.load_macro_cache()["fed_watch"]["next_meeting"] == "2026-08-06"


# ---------------------------------------------------------------------------
# 壊れていても本体を止めない
# ---------------------------------------------------------------------------


def test_corrupt_cache_is_ignored(tmp_path):
    (tmp_path / "macro_events.json").write_text("{ broken", encoding="utf-8")
    assert fe.load_macro_cache() is None
    assert fe._macro_events(LIVE, START, END), "壊れた退避があっても取得分は返す"


def test_unwritable_cache_does_not_raise(monkeypatch):
    monkeypatch.setattr(fe, "_macro_cache_path",
                        lambda: "/nonexistent\x00/macro.json")
    assert fe.save_macro_cache(LIVE) is False
    assert fe._macro_events(LIVE, START, END), "退避に失敗しても本体は動く"


def test_save_refuses_empty_payload():
    assert fe.save_macro_cache({}) is False
    assert fe.save_macro_cache(None) is False
