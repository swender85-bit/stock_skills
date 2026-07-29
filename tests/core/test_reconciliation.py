"""三点照合のテスト (土曜設計書 提案1-⑨ 受け入れ基準)。

受け入れ基準:
1. 実口座と模型の差分が正しく列挙され、分類される
2. 分割を含む週で、コーポレートアクション由来差分が自動補正される
3. 孤児・幽霊が定義通り検出される（thesis有無・政策有無・口座実在の3軸）
4. API停止をモックした場合、黙って古い残高を使わず未照合フラグが立つ
"""

from __future__ import annotations

import pytest

from src.core.portfolio import reconciliation as rc
from src.data.brokers.base import make_position, make_snapshot


def _model(*rows):
    return [
        {"name": n, "quote_symbol": s, "shares": q, "account": a, "cost_price": c}
        for n, s, q, a, c in rows
    ]


def _broker_snap(positions, **kw):
    kw.setdefault("scope", ["JP", "US", "FUND"])
    return make_snapshot("test_broker", available=True, positions=positions, **kw)


def _pos(symbol, shares, name=None, account=None):
    return make_position(symbol, shares, name=name or symbol, account=account)


@pytest.fixture(autouse=True)
def _no_intent(monkeypatch):
    """既定では意図（thesis/政策）を空にする。孤児判定を明示的にテストする。"""
    monkeypatch.setattr(rc, "_load_intent", lambda s, n: {
        "theses": [], "policies": [], "has_thesis": False, "has_policy": False})


# ---------------------------------------------------------------------------
# 同定
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("7203.T", "7203"), ("US.AAPL", "AAPL"), ("aapl", "AAPL"),
    ("JP.2802", "2802"), ("", ""), (None, ""),
])
def test_normalize_symbol(raw, expected):
    assert rc.normalize_symbol(raw) == expected


def test_position_key_ignores_account():
    """同じ銘柄を特定とNISAで持っていても1つのキーになる。

    口座を含めると、口座を分けているだけで幽霊2件に化ける。
    """
    a = rc.position_key({"symbol": "2802.T", "account": "特定"})
    b = rc.position_key({"symbol": "2802.T", "account": "NISA成長"})
    assert a == b == "sym:2802"


def test_position_key_falls_back_to_name_for_funds():
    """ティッカーの無い投信も必ずキーを持つ（捨てると保有が黙って消える）。"""
    k1 = rc.position_key({"symbol": None, "name": "iFreeNEXT FANG+"})
    k2 = rc.position_key({"symbol": None, "name": "iFreeNEXT FANG+インデックス"})
    assert k1 == k2 and k1.startswith("name:")


def test_aggregate_sums_multiple_accounts():
    agg = rc.aggregate([_pos("2802.T", 400, account="特定"),
                        _pos("2802.T", 39, account="NISA成長")])
    assert agg["sym:2802"]["shares"] == 439
    assert set(agg["sym:2802"]["accounts"]) == {"特定", "NISA成長"}


# ---------------------------------------------------------------------------
# 差分の分類（受け入れ基準1・2）
# ---------------------------------------------------------------------------


def test_classify_match():
    assert rc.classify_quantity_diff(100, 100)["classification"] == "match"


def test_classify_below_threshold_uses_value():
    """端株レベルの差は金額で握り潰す（ノイズで赤字を埋めない）。"""
    r = rc.classify_quantity_diff(100, 102, value_per_share_jpy=100.0)
    assert r["classification"] == "below_threshold"


def test_classify_split_is_auto_fixable():
    r = rc.classify_quantity_diff(100, 200, value_per_share_jpy=3000.0)
    assert r["classification"] == "corporate_action"
    assert r["auto_fixable"] is True
    assert r["ratio"] == pytest.approx(2.0)


def test_classify_reverse_split():
    r = rc.classify_quantity_diff(300, 100, value_per_share_jpy=3000.0)
    assert r["classification"] == "corporate_action"
    assert r["ratio"] == pytest.approx(1 / 3, rel=1e-3)


def test_classify_unrecorded_trade():
    r = rc.classify_quantity_diff(100, 130, value_per_share_jpy=3000.0)
    assert r["classification"] == "unrecorded_trade"
    assert r["auto_fixable"] is False


def test_classify_unknown_when_shares_missing():
    assert rc.classify_quantity_diff(None, 100)["classification"] == "unknown"


def test_apply_corporate_actions_adjusts_shares_and_cost():
    config = {"holdings": [
        {"name": "テスト", "quote_symbol": "1234.T", "shares": 100, "cost_price": 3000.0}]}
    result = {"corporate_actions": [{
        "key": "sym:1234", "ratio": 2.0, "auto_fixable": True,
        "broker_shares": 200, "message": "1:2 分割"}]}
    updated, applied = rc.apply_corporate_actions(config, result)
    assert updated["holdings"][0]["shares"] == 200
    assert updated["holdings"][0]["cost_price"] == 1500.0
    assert len(applied) == 1


def test_apply_corporate_actions_ignores_unrecorded_trades():
    """記録漏れを自動で辻褄合わせしない。合わせると弱点が見えなくなる。"""
    config = {"holdings": [
        {"name": "テスト", "quote_symbol": "1234.T", "shares": 100, "cost_price": 3000.0}]}
    result = {"corporate_actions": [{
        "key": "sym:1234", "ratio": 1.3, "auto_fixable": False, "broker_shares": 130}]}
    updated, applied = rc.apply_corporate_actions(config, result)
    assert updated["holdings"][0]["shares"] == 100
    assert applied == []


# ---------------------------------------------------------------------------
# 幽霊 / 未記録 / 孤児（受け入れ基準3）
# ---------------------------------------------------------------------------


def test_ghost_detected_when_model_has_extra():
    model = _model(("トヨタ", "7203.T", 100, "特定", 2800.0))
    snap = _broker_snap([])
    r = rc.reconcile(model, [snap])
    assert r["counts"]["ghosts"] == 1
    assert r["ghosts"][0]["symbol"] == "7203.T"
    assert r["blocking"] is True


def test_unrecorded_detected_when_broker_has_extra():
    r = rc.reconcile([], [_broker_snap([_pos("AAPL", 10)])])
    assert r["counts"]["unrecorded"] == 1
    assert r["unrecorded"][0]["symbol"] == "AAPL"


def test_matched_positions_produce_ok_status():
    model = _model(("トヨタ", "7203.T", 100, "特定", 2800.0))
    r = rc.reconcile(model, [_broker_snap([_pos("7203.T", 100)])], check_intent=False)
    assert r["status"] == "ok"
    assert r["blocking"] is False
    assert r["counts"]["matched"] == 1


def test_orphan_requires_both_thesis_and_policy_absent(monkeypatch):
    """thesis か 政策 のどちらかがあれば孤児ではない。"""
    model = _model(("A", "1111.T", 10, "特定", 100.0),
                   ("B", "2222.T", 10, "特定", 100.0))

    def fake_intent(symbol, name):
        has = str(symbol).startswith("1111")
        return {"theses": [{"id": "x"}] if has else [], "policies": [],
                "has_thesis": has, "has_policy": False}

    monkeypatch.setattr(rc, "_load_intent", fake_intent)
    snap = _broker_snap([_pos("1111.T", 10), _pos("2222.T", 10)])
    r = rc.reconcile(model, [snap])
    assert [o["symbol"] for o in r["orphans"]] == ["2222.T"]


def test_orphan_burden_uses_weights():
    model = _model(("A", "1111.T", 10, "特定", 100.0))
    snap = _broker_snap([_pos("1111.T", 10)])
    r = rc.reconcile(model, [snap], values_jpy={"sym:1111": 500000.0},
                     total_jpy=1000000.0)
    assert r["orphan_burden_pct"] == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# 縮退（受け入れ基準4）
# ---------------------------------------------------------------------------


def test_unreconcilable_when_all_sources_fail():
    """全ソース停止時、黙って古い残高を使わず未照合になる。"""
    dead = make_snapshot("moomoo", available=False, error="OpenD 未接続", scope=["US"])
    model = _model(("トヨタ", "7203.T", 100, "特定", 2800.0))
    r = rc.reconcile(model, [dead], check_intent=False)
    assert r["status"] == "unreconciled"
    assert r["blocking"] is True
    assert r["counts"]["ghosts"] == 0, "取得失敗を幽霊に化けさせてはいけない"
    assert r["counts"]["unverified"] == 1


def test_out_of_scope_symbols_are_unverified_not_ghosts():
    """moomoo は米国株しか見えない。日本株を幽霊扱いしてはいけない。"""
    us_only = make_snapshot("moomoo", available=True, scope=["US"],
                            positions=[_pos("AAPL", 10)])
    model = _model(("トヨタ", "7203.T", 100, "特定", 2800.0),
                   ("Apple", "AAPL", 10, "特定", 150.0))
    r = rc.reconcile(model, [us_only], check_intent=False)
    assert r["counts"]["ghosts"] == 0
    assert [u["symbol"] for u in r["unverified"]] == ["7203.T"]


def test_circular_source_is_not_reported_as_ok():
    """模型の生成元と同じCSVとの突合を『一致』と呼ばない。"""
    snap = make_snapshot("rakuten_csv", available=True, scope=["JP"],
                         positions=[_pos("7203.T", 100)],
                         detail={"circular": True, "circular_reason": "同一CSV"})
    model = _model(("トヨタ", "7203.T", 100, "特定", 2800.0))
    r = rc.reconcile(model, [snap], check_intent=False)
    assert r["status"] == "circular"
    assert r["independently_verified"] is False


def test_stale_snapshot_produces_warning_message():
    snap = make_snapshot("rakuten_csv", available=True, scope=["JP"],
                         positions=[_pos("7203.T", 100)],
                         as_of="2020-01-01T00:00:00+00:00")
    r = rc.reconcile(_model(("トヨタ", "7203.T", 100, "特定", 2800.0)), [snap],
                     check_intent=False)
    assert any("反映されていません" in m for m in r["messages"])


# ---------------------------------------------------------------------------
# 出力整形
# ---------------------------------------------------------------------------


def test_formatter_renders_without_crashing():
    from src.output.reconcile_formatter import format_compact, format_reconciliation

    snap = _broker_snap([_pos("7203.T", 100)])
    r = rc.reconcile(_model(("トヨタ", "7203.T", 100, "特定", 2800.0)), [snap])
    text = format_reconciliation(r)
    assert "三点照合" in text
    assert "照合" in format_compact(r)


def test_formatter_handles_empty_result():
    from src.output.reconcile_formatter import format_reconciliation

    assert "三点照合" in format_reconciliation({})
