"""共通基盤(タイムスタンプ規律・封印ハッシュ)のテスト -- Fable5 第2弾."""

from datetime import datetime, timedelta, timezone

import pytest

from src.core.temporal import (
    apply_seal,
    canonical_json,
    compare_instants,
    market_of,
    market_offset,
    parse_instant,
    seal,
    stamp,
    stamp_for_symbol,
    verify_seal,
)


class TestMarketResolution:
    def test_japanese_suffix_maps_to_jp(self):
        assert market_of("7203.T") == "JP"

    def test_bare_ticker_defaults_to_us(self):
        assert market_of("AAPL") == "US"

    def test_hong_kong_and_singapore(self):
        assert market_of("0700.HK") == "HK"
        assert market_of("D05.SI") == "SG"

    def test_empty_symbol_is_default(self):
        assert market_of("") == "US"

    def test_us_offset_shifts_with_dst(self):
        winter = datetime(2026, 1, 15, tzinfo=timezone.utc)
        summer = datetime(2026, 7, 15, tzinfo=timezone.utc)
        assert market_offset("US", winter) == -5
        assert market_offset("US", summer) == -4

    def test_tokyo_offset_is_constant(self):
        assert market_offset("JP", datetime(2026, 1, 15, tzinfo=timezone.utc)) == 9
        assert market_offset("JP", datetime(2026, 7, 15, tzinfo=timezone.utc)) == 9


class TestStamp:
    def test_stamp_carries_utc_and_market_local(self):
        at = datetime(2026, 7, 18, 0, 30, tzinfo=timezone.utc)
        s = stamp("JP", at=at)
        assert s["utc"].startswith("2026-07-18T00:30")
        assert s["market"] == "JP"
        assert s["market_tz"] == "Asia/Tokyo"
        assert s["offset_hours"] == 9
        # UTC 00:30 は東京の同日 09:30
        assert s["market_local"].startswith("2026-07-18T09:30")

    def test_stamp_for_symbol_infers_market(self):
        s = stamp_for_symbol("7203.T", at=datetime(2026, 7, 18, tzinfo=timezone.utc))
        assert s["market"] == "JP"

    def test_naive_datetime_is_treated_as_utc(self):
        s = stamp("JP", at=datetime(2026, 7, 18, 0, 0))
        assert s["utc"].startswith("2026-07-18T00:00")


class TestInstantComparison:
    def test_disclosure_before_decision(self):
        assert compare_instants("2026-07-15T09:00:00+09:00", "2026-07-18T09:00:00+09:00") == -1

    def test_disclosure_after_decision(self):
        assert compare_instants("2026-07-20T09:00:00+09:00", "2026-07-18T09:00:00+09:00") == 1

    def test_equal_instants(self):
        assert compare_instants("2026-07-18T00:00:00+00:00", "2026-07-18T09:00:00+09:00") == 0

    def test_cross_timezone_ordering_is_correct(self):
        """東京の朝9時 は NY の同日朝9時より前 -- ローカル時刻の見た目に騙されない。"""
        tokyo = "2026-07-18T09:00:00+09:00"   # = 00:00 UTC
        ny = "2026-07-18T09:00:00-04:00"      # = 13:00 UTC
        assert compare_instants(tokyo, ny) == -1

    def test_unparseable_returns_none(self):
        assert compare_instants("not-a-time", "2026-07-18T00:00:00+00:00") is None

    def test_none_input_returns_none(self):
        assert compare_instants(None, "2026-07-18T00:00:00+00:00") is None

    def test_accepts_stamp_dict(self):
        a = stamp("JP", at=datetime(2026, 7, 15, tzinfo=timezone.utc))
        b = stamp("US", at=datetime(2026, 7, 18, tzinfo=timezone.utc))
        assert compare_instants(a, b) == -1

    def test_z_suffix_is_parsed(self):
        assert parse_instant("2026-07-18T00:00:00Z") == datetime(
            2026, 7, 18, tzinfo=timezone.utc
        )


class TestSeal:
    def test_canonical_json_is_key_order_independent(self):
        assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})

    def test_seal_is_stable_across_key_order(self):
        assert seal({"b": 1, "a": 2}) == seal({"a": 2, "b": 1})

    def test_apply_seal_then_verify_passes(self):
        payload = apply_seal({"decision": "buy", "symbol": "7203.T"})
        assert verify_seal(payload) is True

    def test_tampering_is_detected(self):
        payload = apply_seal({"decision": "buy", "symbol": "7203.T"})
        payload["decision"] = "sell"
        assert verify_seal(payload) is False

    def test_unsealed_payload_is_not_verified(self):
        assert verify_seal({"decision": "buy"}) is False

    def test_seal_excludes_itself_so_resealing_is_idempotent(self):
        payload = apply_seal({"a": 1})
        first = payload["seal"]
        apply_seal(payload)
        assert payload["seal"] == first
