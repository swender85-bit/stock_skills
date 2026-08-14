"""照合まわりのカオス: 古い模型 / 循環のみ / 空CSV (改善7).

| テスト | 壊し方 | 期待される検出 |
|:---|:---|:---|
| test_stale_holdings | 模型を3ヶ月前の版にする | **幽霊ポジション**として検出 |
| test_circular_only | 楽天CSVを模型の生成元と同一にする | `circular=true` +「独立検証ではない」 |
| test_empty_csv | 楽天CSVを0件にする | 「保有なし」ではなく**「取得できなかった」** |
"""

from __future__ import annotations

from src.core.portfolio.reconciliation import reconcile
from src.data.brokers.base import make_position, make_snapshot


def _snapshot(positions, *, available=True, circular=False, detail=None, **kw):
    d = dict(detail or {})
    if circular:
        d.update({"circular": True,
                  "circular_reason": "模型を生成した元CSVとの突合です。"
                                     "取り込み後に約定した売買は原理的に検出できません。"})
    return make_snapshot("rakuten_csv", available=available, positions=positions,
                         cash=[], as_of="2026-08-02", scope={"JP", "US"},
                         detail=d, **kw)


def _model(rows):
    return [{"quote_symbol": s, "shares": n, "name": s, "account": "特定"}
            for s, n in rows]


# ---------------------------------------------------------------------------
# 3ヶ月前の模型を渡す → 幽霊ポジション
# ---------------------------------------------------------------------------


class TestStaleHoldings:
    def test_sold_position_still_in_model_is_a_ghost(self):
        """3ヶ月前に持っていて今は無い銘柄は、一致でも残高不明でもなく幽霊。

        幽霊を検出できないと、**存在しない資産のリスクを計算している状態**になる。
        ストレステストもHHIも、実在しないポジションを含んで走る。
        """
        stale_model = _model([("2802.T", 400), ("9843.T", 345), ("SOXL", 275)])
        # 口座にはもう 9843.T が無い（3ヶ月の間に売った）
        broker = _snapshot([
            make_position("2802.T", 400, name="味の素", market="JP"),
            make_position("SOXL", 275, name="SOXL", market="US"),
        ])

        result = reconcile(stale_model, [broker], check_intent=False)
        ghosts = {g.get("symbol") for g in result["ghosts"]}

        assert "9843.T" in ghosts, "売却済み銘柄が幽霊として検出されていない"
        assert result["counts"]["ghosts"] == 1
        assert any("幽霊" in m or "不在" in m for m in result["messages"])

    def test_ghost_is_not_reported_as_matched(self):
        stale_model = _model([("2802.T", 400), ("TQQQ", 214)])
        broker = _snapshot([make_position("2802.T", 400, name="味の素", market="JP")])
        result = reconcile(stale_model, [broker], check_intent=False)
        assert result["counts"]["matched"] == 1
        assert result["status"] != "ok"

    def test_share_count_drift_is_a_diff_not_a_match(self):
        # 積み増したのに模型が古い場合
        result = reconcile(
            _model([("SOXL", 275)]),
            [_snapshot([make_position("SOXL", 310, name="SOXL", market="US")])],
            check_intent=False)
        assert result["counts"]["diffs"] == 1
        assert result["counts"]["matched"] == 0


# ---------------------------------------------------------------------------
# 循環照合のみ → 「一致」と呼ばない
# ---------------------------------------------------------------------------


class TestCircularOnly:
    def test_circular_snapshot_is_not_independent_verification(self):
        """§16-3。差分0でも、模型の生成元と同じデータなら一致の証拠ではない。"""
        model = _model([("2802.T", 400), ("SOXL", 275)])
        broker = _snapshot([
            make_position("2802.T", 400, name="味の素", market="JP"),
            make_position("SOXL", 275, name="SOXL", market="US"),
        ], circular=True)

        result = reconcile(model, [broker], check_intent=False)

        assert result["independently_verified"] is False
        assert result["status"] == "circular"
        assert result["counts"]["diffs"] == 0  # 差分は0。だが一致ではない。

    def test_circular_reason_reaches_the_output(self):
        model = _model([("2802.T", 400)])
        broker = _snapshot([make_position("2802.T", 400, name="味の素", market="JP")],
                           circular=True)
        result = reconcile(model, [broker], check_intent=False)
        blob = " ".join(result["messages"])
        assert "独立" in blob or "循環" in blob
        assert "取り込む前に" in blob or "検出できません" in blob

    def test_non_circular_snapshot_is_independently_verified(self):
        model = _model([("2802.T", 400)])
        broker = _snapshot([make_position("2802.T", 400, name="味の素", market="JP")])
        result = reconcile(model, [broker], check_intent=False)
        assert result["independently_verified"] is True
        assert result["status"] == "ok"


# ---------------------------------------------------------------------------
# 空CSV → 「保有なし」ではなく「取得できなかった」
# ---------------------------------------------------------------------------


class TestEmptyCsv:
    def test_unavailable_snapshot_yields_unverified_not_ghosts(self):
        """取得できなかった口座を「全部売った」と読んではいけない。

        available=False を空の口座と混同すると、全保有が幽霊になり、
        レポートは「全部売却済み」という完全な嘘を出す。
        """
        model = _model([("2802.T", 400), ("SOXL", 275)])
        dead = make_snapshot("rakuten_csv", available=False, positions=[], cash=[],
                             as_of=None, scope=set(),
                             detail={"error": "CSVが見つかりませんでした"})

        result = reconcile(model, [dead], check_intent=False)

        assert result["ghosts"] == [], "取得失敗を売却済みと解釈している"
        assert result["counts"]["unverified"] == 2
        assert result["reconcilable"] is False
        assert result["status"] == "unreconciled"

    def test_unverified_reason_says_it_could_not_be_fetched(self):
        model = _model([("2802.T", 400)])
        dead = make_snapshot("rakuten_csv", available=False, positions=[], cash=[],
                             as_of=None, scope=set(), detail={})
        result = reconcile(model, [dead], check_intent=False)
        reasons = " ".join(r.get("reason", "") for r in result["unverified"])
        assert "取得" in reasons

    def test_available_but_truly_empty_account_is_a_ghost(self):
        """本当に0件の口座（available=True）は幽霊で正しい。

        `available=False`（取得失敗）と `positions=[]`（本当に空）の
        区別が付いていることを確認する。ここが同じ扱いになると、
        取得失敗が「全部売った」に化ける。
        """
        model = _model([("2802.T", 400)])
        empty = _snapshot([])
        result = reconcile(model, [empty], check_intent=False)
        assert result["counts"]["ghosts"] == 1
