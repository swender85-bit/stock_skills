"""ブローカー抽象層のテスト (土曜設計書 提案1)。

核心は「取得失敗を保有ゼロに化けさせない」こと。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.data import brokers
from src.data.brokers import base as bb


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def test_snapshot_marks_unavailable_without_faking_zero():
    snap = bb.make_snapshot("moomoo", available=False, error="OpenD 未接続")
    assert snap["available"] is False
    assert snap["positions"] == []
    assert snap["as_of"] is None, "取得できていないのに時点を捏造しない"
    assert "取得できず" in bb.snapshot_summary(snap)


def test_snapshot_staleness_threshold():
    fresh = bb.make_snapshot("x", available=True, as_of=_iso(1), max_age_hours=24)
    old = bb.make_snapshot("x", available=True, as_of=_iso(100), max_age_hours=24)
    assert fresh["stale"] is False
    assert old["stale"] is True
    assert "古い" in bb.snapshot_summary(old)


def test_make_position_normalizes_blank_fields():
    p = bb.make_position("  aapl ", 10, name="  ", currency="usd")
    assert p["symbol"] == "aapl"
    assert p["name"] is None
    assert p["currency"] == "USD"
    assert p["shares"] == 10.0


def test_make_position_rejects_non_numeric_shares():
    assert bb.make_position("AAPL", "ten")["shares"] is None


def test_merged_scope_excludes_failed_sources():
    """失敗したソースの scope を数えると、その市場の保有が全部幽霊に化ける。"""
    ok = bb.make_snapshot("rakuten_csv", available=True, scope=["JP", "US"])
    ng = bb.make_snapshot("moomoo", available=False, scope=["US", "HK"],
                          error="未接続")
    assert brokers.merged_scope([ok, ng]) == {"JP", "US"}


def test_is_reconcilable():
    ok = bb.make_snapshot("a", available=True)
    ng = bb.make_snapshot("b", available=False, error="x")
    assert brokers.is_reconcilable([ok, ng]) is True
    assert brokers.is_reconcilable([ng]) is False
    assert brokers.is_reconcilable([]) is False


def test_collect_snapshots_isolates_source_failures(monkeypatch):
    """1ソースが例外を投げても他は返る。理由はスナップショットに残る。"""
    import src.data.brokers.rakuten_csv_broker as rcb

    monkeypatch.setattr(rcb, "fetch", lambda **kw: (_ for _ in ()).throw(
        RuntimeError("boom")))
    snaps = brokers.collect_snapshots(sources=["rakuten_csv"])
    assert len(snaps) == 1
    assert snaps[0]["available"] is False
    assert "boom" in (snaps[0]["error"] or "")


def test_collect_snapshots_reports_unknown_source():
    snaps = brokers.collect_snapshots(sources=["nonexistent"])
    assert snaps[0]["available"] is False
    assert "未知" in snaps[0]["error"]


# ---------------------------------------------------------------------------
# 楽天CSV
# ---------------------------------------------------------------------------


def test_rakuten_falls_back_to_yaml_with_degraded_flag(tmp_path, monkeypatch):
    """CSV原本が無いときは YAML に縮退するが、黙って正常扱いしない。"""
    import src.data.brokers.rakuten_csv_broker as rcb

    yaml_path = tmp_path / "holdings.yaml"
    yaml_path.write_text(
        "holdings:\n"
        "- name: トヨタ\n  quote_symbol: 7203.T\n  account: 特定\n"
        "  shares: 100\n  cost_price: 2800.0\n  currency: JPY\n"
        "cash:\n- name: 円\n  amount: 1000\n  currency: JPY\n"
        "source:\n  exported_at: '2026-01-01T00:00:00+00:00'\n  file: x.csv\n",
        encoding="utf-8")

    monkeypatch.setattr(rcb, "_try_read_csv", lambda *a, **k: None)
    snap = rcb.fetch(holdings_config_path=str(yaml_path))

    assert snap["available"] is True
    assert snap["detail"]["degraded"] is True
    assert "取り込み漏れを検出できません" in snap["detail"]["degraded_reason"]
    assert len(snap["positions"]) == 1
    assert snap["cash"][0]["amount"] == 1000


def test_rakuten_unavailable_when_nothing_readable(tmp_path, monkeypatch):
    import src.data.brokers.rakuten_csv_broker as rcb

    monkeypatch.setattr(rcb, "_try_read_csv", lambda *a, **k: None)
    snap = rcb.fetch(holdings_config_path=str(tmp_path / "missing.yaml"))
    assert snap["available"] is False


def test_rakuten_flags_circular_when_csv_is_the_import_source(tmp_path, monkeypatch):
    """模型を作った元CSVを読んだら、循環として明示する。"""
    import src.data.brokers.rakuten_csv_broker as rcb

    yaml_path = tmp_path / "holdings.yaml"
    yaml_path.write_text(
        "holdings:\n- name: A\n  quote_symbol: 1111.T\n  shares: 1\n"
        "source:\n  file: same.csv\n  exported_at: '2026-01-01T00:00:00+00:00'\n",
        encoding="utf-8")
    csv_file = tmp_path / "same.csv"
    csv_file.write_text("dummy", encoding="utf-8")

    monkeypatch.setattr(rcb, "_try_read_csv", lambda *a, **k: (
        [bb.make_position("1111.T", 1, name="A")], [], csv_file, None))

    snap = rcb.fetch(holdings_config_path=str(yaml_path))
    assert snap["detail"]["circular"] is True
    assert "新しいCSV" in snap["detail"]["circular_reason"]


def test_rakuten_not_circular_for_different_csv(tmp_path, monkeypatch):
    import src.data.brokers.rakuten_csv_broker as rcb

    yaml_path = tmp_path / "holdings.yaml"
    yaml_path.write_text(
        "holdings:\n- name: A\n  quote_symbol: 1111.T\n  shares: 1\n"
        "source:\n  file: old.csv\n", encoding="utf-8")
    csv_file = tmp_path / "new.csv"
    csv_file.write_text("dummy", encoding="utf-8")

    monkeypatch.setattr(rcb, "_try_read_csv", lambda *a, **k: (
        [bb.make_position("1111.T", 1, name="A")], [], csv_file, None))

    snap = rcb.fetch(holdings_config_path=str(yaml_path))
    assert snap["detail"]["circular"] is False


# ---------------------------------------------------------------------------
# moomoo
# ---------------------------------------------------------------------------


def test_moomoo_disabled_returns_unavailable(monkeypatch):
    monkeypatch.delenv("MOOMOO_ENABLED", raising=False)
    from src.data.brokers import moomoo_broker

    snap = moomoo_broker.fetch(autostart=False)
    assert snap["available"] is False
    assert snap["scope"] == ["US"]


def test_moomoo_scope_is_us_only():
    """日本株は US LV3 では取れない。scope を広げると幽霊が量産される。"""
    from src.data.brokers import moomoo_broker

    assert moomoo_broker.SCOPE == ["US"]


def test_moomoo_rows_raises_on_error_code():
    from src.data.brokers import moomoo_broker

    with pytest.raises(RuntimeError):
        moomoo_broker._rows((-1, "権限がありません"))


def test_moomoo_to_position_strips_market_prefix():
    from src.data.brokers import moomoo_broker

    p = moomoo_broker._to_position({"code": "US.AAPL", "qty": 10,
                                    "stock_name": "Apple", "cost_price": 150.0})
    assert p["symbol"] == "AAPL"
    assert p["market"] == "US"
    assert p["shares"] == 10.0
