"""テーゼ本文からの撤退ライン抽出。

2026-08-01 時点で QCOM のテーゼには「撤退ライン 約$158」と**本文に書かれていた**が
反証条件として登録されておらず、7/29 終値 $155.68 で**既に割れていたのに
検出されなかった**。レポートは「反証条件が未定義です」という一般的な促しを
出しただけだった。書いてあるのに拾えないのは促しの問題ではなく抽出の問題。

守るべき性質:
- 数値で書かれた撤退ラインだけを拾う（曖昧な表現は拾わない）
- 既に割れているなら、それを明示する
- **提案するだけ。自動登録はしない**（条件の確定は判断であり機械が断定しない）
"""

from __future__ import annotations

from src.core.portfolio.falsification import (
    check_thesis,
    evaluate_suggestions,
    suggest_conditions_from_content,
)


def _exprs(text: str, symbol: str = "X") -> list[str]:
    return [s["expression"] for s in suggest_conditions_from_content(text, symbol)]


# ---------------------------------------------------------------------------
# 抽出
# ---------------------------------------------------------------------------


def test_extracts_the_qcom_exit_line_that_was_missed():
    assert _exprs("業績悪化に株価が追随。撤退ライン 約$158 を割ったら見直す。",
                  "QCOM") == ["price <= 158"]


def test_extracts_yen_price_with_thousands_separator():
    assert _exprs("ニトリは損切り 2,400円 を下回ったら撤退する") == ["price <= 2400"]


def test_extracts_drawdown_in_either_word_order():
    assert _exprs("-25% で撤退する") == ["drawdown_pct <= -25"]
    assert _exprs("撤退は -15% を目安に") == ["drawdown_pct <= -15"]


def test_extracts_decimal_price():
    assert _exprs("ストップロス $95.5") == ["price <= 95.5"]


def test_percentage_is_never_read_as_a_price():
    """「撤退は -15% を目安に」から price <= 15 が出ると無意味な条件になる。"""
    assert "price <= 15" not in _exprs("撤退は -15% を目安に")


def test_vague_wording_is_not_extracted():
    """「調子が悪ければ売る」は測定不能。推測して条件を作らない。"""
    assert _exprs("調子が悪ければ売る") == []
    assert _exprs("そろそろ危ないので撤退を検討") == []


def test_empty_content_yields_nothing():
    assert suggest_conditions_from_content(None) == []
    assert suggest_conditions_from_content("   ") == []


def test_duplicate_mentions_are_deduplicated():
    assert _exprs("撤退ライン $158。念のため再掲すると撤退ライン $158。") == \
        ["price <= 158"]


def test_suggestion_carries_the_registration_command():
    s = suggest_conditions_from_content("撤退ライン $158", "QCOM")[0]
    assert "QCOM" in s["how_to"]
    assert "price <= 158" in s["how_to"]
    assert s["matched_text"]


# ---------------------------------------------------------------------------
# 既に割れているかの評価
# ---------------------------------------------------------------------------


def test_already_breached_line_is_called_out():
    rows = evaluate_suggestions(
        suggest_conditions_from_content("撤退ライン $158", "QCOM"),
        {"price": 155.68})
    assert rows[0]["state"] == "met"
    assert "既に成立しています" in rows[0]["message"]


def test_intact_line_is_not_flagged():
    rows = evaluate_suggestions(
        suggest_conditions_from_content("撤退ライン $158", "QCOM"),
        {"price": 170.0})
    assert rows[0]["state"] != "met"
    assert "message" not in rows[0]


def test_missing_state_yields_unknown_not_breached():
    rows = evaluate_suggestions(
        suggest_conditions_from_content("撤退ライン $158", "QCOM"), {})
    assert rows[0]["state"] != "met"


# ---------------------------------------------------------------------------
# check_thesis への統合
# ---------------------------------------------------------------------------


def test_undefined_thesis_reports_the_breach_it_found_in_the_text():
    r = check_thesis(
        {"symbol": "QCOM", "content": "撤退ライン 約$158 を割ったら見直す"},
        {"price": 155.68})
    assert r["state"] == "undefined"
    assert r["suggested_breached"] is True
    assert "既に成立しています" in r["message"]
    assert "週次の点検対象になっていませんでした" in r["message"]


def test_undefined_thesis_offers_registration_when_not_yet_breached():
    r = check_thesis(
        {"symbol": "QCOM", "content": "撤退ライン 約$158"}, {"price": 170.0})
    assert r["suggested_breached"] is False
    assert "登録すれば" in r["message"]


def test_undefined_thesis_without_numbers_keeps_the_generic_message():
    r = check_thesis({"symbol": "X", "content": "良い会社なので持つ"}, {"price": 10.0})
    assert r["suggestions"] == []
    assert "何が起きたら間違いと認めるか" in r["message"]


def test_suggestions_never_become_registered_conditions():
    """提案は提案のまま。自動で反証条件にはしない。"""
    r = check_thesis({"symbol": "QCOM", "content": "撤退ライン $158"},
                     {"price": 155.68})
    assert r["has_falsification"] is False
    assert r["conditions"] == []
    assert r["falsified"] is False, "提案だけで反証扱いにしてはいけない"
