"""楽天の取引履歴CSV取り込み（提案5 執行監査の入力）。

2026-08-01 の週次レポートは「約定履歴が取れず決定生存率は90日間測定不能」と
書いていた。取得元が moomoo だけだったのが原因で、実際の売買は全て楽天にある。
**取得先が実態と食い違っていたので、原理的に永久に測定できなかった。**

守るべき性質:
- 見出しを特定できないとき、推測せず・0件を返さず、検出した見出しを添えて失敗する
- 「取れなかった」と「0件だった」を呼び出し側が区別できる
- 売買区分を判定できない行も捨てない（side=None で残す）
- 国内4桁コードは .T を付ける
"""

from __future__ import annotations

from src.data import rakuten_trades as rt

HEADER = "約定日,銘柄コード,銘柄名,売買区分,口座区分,数量,単価,手数料,受渡金額\n"


def _csv(*rows: str) -> bytes:
    return (HEADER + "".join(rows)).encode("cp932")


# ---------------------------------------------------------------------------
# 正常系
# ---------------------------------------------------------------------------


def test_parses_domestic_buy_and_normalizes_symbol():
    raw = _csv("2026/07/23,2737,トーメンデバイス,買付,特定,40,14230,1100,570300\n")
    r = rt.parse_trades(raw)
    assert r["available"] is True
    e = r["executions"][0]
    assert e["symbol"] == "2737.T", "4桁コードには .T を付ける"
    assert e["side"] == "buy"
    assert e["executed_at"] == "2026-07-23"
    assert e["shares"] == 40.0
    assert e["price"] == 14230.0


def test_us_ticker_is_left_alone():
    raw = _csv("2026/07/23,QCOM,クアルコム,買付,特定,85,186.26,0,2443355\n")
    assert rt.parse_trades(raw)["executions"][0]["symbol"] == "QCOM"


def test_sell_is_detected_before_buy_to_avoid_substring_confusion():
    raw = _csv("2026/07/23,7203,トヨタ,売却,特定,100,2850,500,284500\n")
    assert rt.parse_trades(raw)["executions"][0]["side"] == "sell"


def test_multiple_rows_are_all_kept():
    raw = _csv(
        "2026/07/23,2737,トーメン,買付,特定,40,14230,1100,570300\n",
        "2026/07/24,QCOM,クアルコム,買付,特定,85,186.26,0,2443355\n",
    )
    assert rt.parse_trades(raw)["count"] == 2


def test_numbers_with_commas_are_parsed():
    raw = _csv("2026/07/23,2737,トーメン,買付,特定,\"1,000\",\"14,230\",1100,\"5,703,000\"\n")
    e = rt.parse_trades(raw)["executions"][0]
    assert e["shares"] == 1000.0
    assert e["amount"] == 5703000.0


def test_iso_and_compact_dates_are_accepted():
    for raw_date, want in (("2026-07-23", "2026-07-23"), ("20260723", "2026-07-23")):
        raw = _csv(f"{raw_date},2737,トーメン,買付,特定,40,14230,0,0\n")
        assert rt.parse_trades(raw)["executions"][0]["executed_at"] == want


# ---------------------------------------------------------------------------
# 「取れなかった」と「0件」の区別
# ---------------------------------------------------------------------------


def test_unrecognized_header_fails_loudly_with_the_headers_it_saw():
    """黙って0件を返すと「約定が無かった」と誤読され、執行率0%の嘘が出る。"""
    raw = "適当,列,名前\n1,2,3\n".encode("cp932")
    try:
        rt.parse_trades(raw)
    except rt.TradeHistoryUnavailable as e:
        assert "0件" in str(e)
        assert "適当" in str(e), "検出した見出しを示して原因を追えるようにする"
    else:
        raise AssertionError("見出し不明なら例外にすべき")


def test_holdings_csv_is_not_mistaken_for_trade_history():
    """保有CSVには売買区分が無い。取引履歴として読んではいけない。"""
    raw = "種別,銘柄コード,銘柄,保有数量,平均取得価額\n国内株式,2737,トーメン,40,14230\n".encode("cp932")
    try:
        rt.parse_trades(raw)
    except rt.TradeHistoryUnavailable:
        pass
    else:
        raise AssertionError("保有CSVは取引履歴として読めてはいけない")


def test_load_trades_without_file_reports_unavailable_not_empty(monkeypatch):
    monkeypatch.setattr(rt, "find_latest", lambda *a, **k: None)
    r = rt.load_trades(path=None)
    assert r["available"] is False
    assert "0件" in r["error"], "「取れなかった」を「0件」と誤読させない"
    assert r["executions"] == []


def test_total_rows_are_skipped_but_recorded():
    raw = _csv("2026/07/23,2737,トーメン,買付,特定,40,14230,0,0\n",
               ",,合計,,,,,,570300\n")
    r = rt.parse_trades(raw)
    assert r["count"] == 1
    assert len(r["skipped"]) == 1


def test_unknown_side_row_is_kept_not_dropped():
    raw = _csv("2026/07/23,2737,トーメン,振替,特定,40,14230,0,0\n")
    r = rt.parse_trades(raw)
    assert r["count"] == 1
    assert r["executions"][0]["side"] is None
    assert r["unknown_side"] == 1


# ---------------------------------------------------------------------------
# 期間の絞り込み
# ---------------------------------------------------------------------------


def test_days_filter_drops_old_trades(tmp_path):
    p = tmp_path / "tradehistory.csv"
    p.write_bytes(_csv("2020/01/05,2737,トーメン,買付,特定,40,14230,0,0\n"))
    r = rt.load_trades(path=str(p), days=90)
    assert r["available"] is True
    assert r["count"] == 0, "古い約定は窓の外"


def test_side_normalization_table():
    assert rt.normalize_side("買付") == "buy"
    assert rt.normalize_side("現物売") == "sell"
    assert rt.normalize_side("解約") == "sell"
    assert rt.normalize_side("") is None
    assert rt.normalize_side("振替") is None


# ---------------------------------------------------------------------------
# 執行監査との接続
# ---------------------------------------------------------------------------


def test_execution_audit_uses_rakuten_first(monkeypatch, tmp_path):
    from src.core.portfolio import execution_audit as ea

    p = tmp_path / "tradehistory.csv"
    p.write_bytes(_csv("2026/07/23,QCOM,クアルコム,買付,特定,85,186.26,0,0\n"))
    monkeypatch.setattr(rt, "find_latest", lambda *a, **k: str(p))

    rows, err = ea._load_executions(100000)
    assert err is None
    assert rows and rows[0]["symbol"] == "QCOM"


def test_execution_audit_reports_all_reasons_when_nothing_works(monkeypatch):
    from src.core.portfolio import execution_audit as ea
    from src.data.brokers import moomoo_broker

    monkeypatch.setattr(rt, "find_latest", lambda *a, **k: None)
    # moomoo は OpenD を起動しに行くのでテストでは必ず塞ぐ
    monkeypatch.setattr(moomoo_broker, "fetch_executions",
                        lambda **k: {"available": False, "error": "無効"})
    rows, err = ea._load_executions(90)
    assert rows == []
    assert "楽天CSV" in err
    assert "moomoo" in err
