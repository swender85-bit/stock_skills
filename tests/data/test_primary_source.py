"""一次情報源（SEC EDGAR / EDINET）と系譜台帳の接続テスト.

## 何を縛っているか

1. **未設定を「開示が無かった」と書かない。** ここを混ぜると、キー未設定が
   「今週この会社は何も開示していない」に化ける（§16-1）。
2. **一次観測は取得元ドメインで機械判定する。** 自己申告を認めない。
   又聞きを一次と偽装できると、深度会計そのものが無意味になる。
3. **日本株を SEC に、米国株を EDINET に投げない。** 市場で振り分ける。
4. **一次観測が0件のとき、それを明示する。** 「深度1の外部言説と自己推論の上に
   立っている」と書けないと、汚染度警告が意味を失う。
"""

from __future__ import annotations

from datetime import date

import pytest

from src.core import primary_source as PS
from src.core.provenance import EXTERNAL, PRIMARY
from src.data import edgar_client, edinet_client


# ---------------------------------------------------------------------------
# 可用性ゲート
# ---------------------------------------------------------------------------


class TestAvailability:
    def test_edgar_needs_a_user_agent(self, monkeypatch):
        monkeypatch.delenv("SEC_EDGAR_UA", raising=False)
        assert edgar_client.is_available() is False

    def test_edgar_available_with_user_agent(self, monkeypatch):
        monkeypatch.setenv("SEC_EDGAR_UA", "tester tester@example.com")
        assert edgar_client.is_available() is True

    def test_edinet_needs_a_key(self, monkeypatch):
        monkeypatch.delenv("EDINET_API_KEY", raising=False)
        assert edinet_client.is_available() is False

    def test_unavailable_never_says_no_disclosure(self):
        for reason in (edgar_client.unavailable()["reason"],
                       edinet_client.unavailable()["reason"]):
            assert "取得できませんでした" in reason
            assert "開示が無かった" in reason      # 「ではありません」と明記している

    def test_source_status_reports_both_markets(self):
        status = PS.source_status()
        assert status["sec_edgar"]["market"] == "US"
        assert status["edinet"]["market"] == "JP"
        assert "無料" in status["sec_edgar"]["cost"]
        assert "無料" in status["edinet"]["cost"]


# ---------------------------------------------------------------------------
# 市場の振り分け
# ---------------------------------------------------------------------------


class TestRouting:
    def test_japanese_symbol_is_not_sent_to_sec(self, monkeypatch):
        monkeypatch.setenv("SEC_EDGAR_UA", "tester tester@example.com")
        called = []
        monkeypatch.setattr(edgar_client, "_get",
                            lambda *a, **k: called.append(a) or None)
        # `.T` 付きは SEC を叩かずに None
        assert edgar_client.resolve_cik("2737.T") is None
        assert called == []

    def test_sec_code_conversion(self):
        assert edinet_client._sec_code("2737.T") == "27370"
        assert edinet_client._sec_code("9843.T") == "98430"
        assert edinet_client._sec_code("AAPL") is None

    def test_fetch_filings_routes_by_market(self, monkeypatch):
        seen: dict[str, str] = {}

        def jp(symbol, **_k):
            seen["jp"] = symbol
            return {"available": True, "symbol": symbol, "filings": []}

        def us(symbol, **_k):
            seen["us"] = symbol
            return {"available": True, "symbol": symbol, "filings": []}

        monkeypatch.setattr(edinet_client, "recent_filings", jp)
        monkeypatch.setattr(edgar_client, "recent_filings", us)
        PS.fetch_filings("2737.T")
        PS.fetch_filings("QCOM")
        assert seen["jp"] == "2737.T"
        assert seen["us"] == "QCOM"
        assert "us" not in {k: v for k, v in seen.items() if v == "2737.T"}


# ---------------------------------------------------------------------------
# 系譜の型付け（最重要）
# ---------------------------------------------------------------------------


class TestProvenanceTyping:
    def _sec_result(self):
        return {
            "available": True, "symbol": "QCOM", "cik": "0000804328",
            "filings": [{
                "form": "10-Q", "accession": "0000804328-26-000045",
                "filed_at": "2026-07-30", "title": "Quarterly report",
                "url": ("https://www.sec.gov/Archives/edgar/data/804328/"
                        "000080432826000045/qcom-20260628.htm"),
                "source": "sec.gov",
            }],
        }

    def test_sec_filing_becomes_primary_observation(self):
        claims = PS.claims_from_filings(self._sec_result())
        assert len(claims) == 1
        assert claims[0]["provenance"] == PRIMARY
        assert claims[0]["depth"] == 0, "一次観測は深度0の錨でなければならない"

    def test_edinet_filing_becomes_primary_observation(self):
        result = {
            "available": True, "symbol": "2737.T",
            "filings": [{
                "doc_type_label": "大量保有報告書", "doc_id": "S100XXXX",
                "submitted_at": "2026-08-05", "title": "大量保有報告書",
                "url": "https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?S100XXXX",
                "source": "disclosure2.edinet-fsa.go.jp",
            }],
        }
        claims = PS.claims_from_filings(result)
        assert claims[0]["provenance"] == PRIMARY
        assert claims[0]["depth"] == 0

    def test_redistributor_url_is_not_primary(self):
        """再配信サービス経由は一次にしない。

        又聞きを一次と偽装できると、深度会計そのものが無意味になる。
        """
        result = {
            "available": True, "symbol": "QCOM",
            "filings": [{
                "form": "10-Q", "filed_at": "2026-07-30", "title": "Quarterly report",
                "url": "https://mcp.financialdatasets.ai/filings/QCOM/10-Q",
            }],
        }
        claims = PS.claims_from_filings(result)
        assert claims[0]["provenance"] == EXTERNAL
        assert claims[0]["depth"] == 1

    def test_yahoo_url_stays_external(self):
        result = {"available": True, "symbol": "QCOM", "filings": [
            {"form": "news", "title": "x", "url": "https://finance.yahoo.com/news/x"}]}
        assert PS.claims_from_filings(result)[0]["provenance"] == EXTERNAL

    def test_unavailable_result_produces_no_claims(self):
        assert PS.claims_from_filings({"available": False, "filings": []}) == []

    def test_primary_claim_can_anchor_a_regrounding(self):
        """**これが動かないと案C の再接地は永久に不可能。**

        `reground()` は錨に一次観測を要求する。一次観測を作る経路が無かったため、
        この機能はこれまで一度も使えなかった。
        """
        from src.core.provenance import SELF, build_claim, needs_regrounding, reground

        primary = PS.claims_from_filings(self._sec_result())[0]
        deep = build_claim("QCOMは割安", SELF, symbol="QCOM")
        deep["depth"] = 4
        assert needs_regrounding(deep) is True

        reground(deep, primary)
        assert deep["depth"] == 1
        assert needs_regrounding(deep) is False


# ---------------------------------------------------------------------------
# セクション組み立て
# ---------------------------------------------------------------------------


class TestPrimarySection:
    def test_missing_sources_are_named_not_silently_dropped(self, monkeypatch):
        monkeypatch.setattr(PS, "fetch_filings",
                            lambda s, **k: {"available": False, "symbol": s,
                                            "filings": [], "reason": "キー未設定"})
        section = PS.build_primary_section(["QCOM", "2737.T"])
        assert section["unavailable_symbols"] == ["QCOM", "2737.T"]
        assert "開示が無かった" in section["note"]
        assert section["primary_count"] == 0

    def test_zero_primary_is_stated_explicitly(self, monkeypatch):
        monkeypatch.setattr(PS, "fetch_filings",
                            lambda s, **k: {"available": False, "symbol": s,
                                            "filings": []})
        section = PS.build_primary_section(["QCOM"])
        # 汚染度警告が意味を持つために、これが書けている必要がある
        assert "外部言説（深度1）と自己推論の上に立っています" in section["note"]

    def test_counts_primary_claims(self, monkeypatch):
        monkeypatch.setattr(PS, "fetch_filings", lambda s, **k: {
            "available": True, "symbol": s, "source": "SEC EDGAR", "filings": [
                {"form": "10-K", "filed_at": "2026-07-01", "title": "Annual",
                 "url": "https://www.sec.gov/Archives/edgar/data/1/2/a.htm"}]})
        section = PS.build_primary_section(["QCOM", "MDT"])
        assert section["primary_count"] == 2
        assert section["available"] is True
        assert "深度0の錨" in section["note"]

    def test_one_failure_does_not_stop_the_others(self, monkeypatch):
        def flaky(symbol, **_k):
            if symbol == "MDT":
                raise RuntimeError("boom")
            return {"available": True, "symbol": symbol, "source": "SEC EDGAR",
                    "filings": []}

        monkeypatch.setattr(PS, "fetch_filings", flaky)
        section = PS.build_primary_section(["QCOM", "MDT"])
        assert "MDT" in section["unavailable_symbols"]
        assert section["by_symbol"]["QCOM"]["available"] is True


# ---------------------------------------------------------------------------
# 急騰の説明（2026-08-06 の 2737.T で必要になった機能）
# ---------------------------------------------------------------------------


class TestExplainMove:
    def test_large_holding_report_is_flagged_as_supply_demand(self, monkeypatch):
        monkeypatch.setattr(edinet_client, "recent_filings", lambda s, **k: {
            "available": True, "symbol": s, "filings": [
                {"doc_type": "350", "doc_type_label": "大量保有報告書",
                 "filer": "某ファンド", "submitted_at": "2026-08-05",
                 "supply_demand": True, "url": "https://disclosure2.edinet-fsa.go.jp/x"},
            ]})
        result = edinet_client.explain_move("2737.T")
        assert result["explained"] is True
        assert "某ファンド" in result["explanation"]

    def test_absence_is_not_proof_of_no_supply_demand_cause(self, monkeypatch):
        monkeypatch.setattr(edinet_client, "recent_filings",
                            lambda s, **k: {"available": True, "symbol": s,
                                            "filings": []})
        result = edinet_client.explain_move("2737.T")
        assert result["explained"] is False
        # 見つからないことを「需給要因ではない」と読ませない
        assert "証明ではありません" in result["explanation"]
        assert "TDnet" in result["explanation"]

    def test_supply_demand_doc_types_cover_large_holdings(self):
        assert "350" in edinet_client.SUPPLY_DEMAND_TYPES   # 大量保有報告書
        assert "360" in edinet_client.SUPPLY_DEMAND_TYPES   # 変更報告書


# ---------------------------------------------------------------------------
# 失敗した日を「開示なし」と混同しない
# ---------------------------------------------------------------------------


def test_failed_days_are_reported(monkeypatch):
    monkeypatch.setenv("EDINET_API_KEY", "test-key")
    calls = {"n": 0}

    def fake_get(day, timeout=20):
        calls["n"] += 1
        return None if calls["n"] % 2 else []

    monkeypatch.setattr(edinet_client, "_get_documents", fake_get)
    result = edinet_client.recent_filings("2737.T", days=6,
                                          today=date(2026, 8, 6))
    assert result["failed_days"], "取得できなかった日を握り潰している"
    assert "見えていません" in result["note"]


def test_no_network_call_without_credentials(monkeypatch):
    """資格情報が無いとき、実際のHTTPを一切叩かない。"""
    import requests

    def explode(*_a, **_k):
        raise AssertionError("資格情報が無いのにネットワークを叩いた")

    monkeypatch.setattr(requests, "get", explode)
    monkeypatch.delenv("SEC_EDGAR_UA", raising=False)
    monkeypatch.delenv("EDINET_API_KEY", raising=False)

    assert edgar_client.recent_filings("QCOM")["available"] is False
    assert edinet_client.recent_filings("2737.T")["available"] is False
    assert edgar_client.key_financials("QCOM")["available"] is False


# ---------------------------------------------------------------------------
# XBRL: 期間量と時点量 / 廃止タグの検出
#
# どちらも実装中に実データで踏んだ欠陥。回帰させない。
# ---------------------------------------------------------------------------


def _concept(rows):
    return {"units": {"USD": rows}}


class TestXbrlWindows:
    def test_duration_facts_reject_quarterly_rows(self):
        """期間量に四半期を混ぜない（§16-2 窓の違う量を比較しない）。"""
        rows = [
            {"start": "2025-06-30", "end": "2025-09-28", "val": 11,
             "form": "10-Q", "fp": "Q4"},
            {"start": "2024-09-30", "end": "2025-09-28", "val": 44,
             "form": "10-K", "fp": "FY"},
        ]
        got = edgar_client._latest_annual(_concept(rows), instant=False)
        assert got["value"] == 44
        assert got["window"] == "annual"

    def test_instant_facts_have_no_start(self):
        rows = [
            {"start": None, "end": "2019-09-29", "val": 4909, "form": "10-K", "fp": "FY"},
            {"start": None, "end": "2025-09-28", "val": 21206, "form": "10-K", "fp": "FY"},
        ]
        got = edgar_client._latest_annual(_concept(rows), instant=True)
        assert got["value"] == 21206, "時点量で古い残高を掴んでいる"
        assert got["window"] == "instant"

    def test_empty_concept_returns_none(self):
        assert edgar_client._latest_annual(_concept([]), instant=True) is None
        assert edgar_client._latest_annual({}, instant=False) is None


class TestAbandonedTagDetection:
    """企業がタグを乗り換えると、旧タグは「その概念では最新」のまま止まる。

    QCOM の `StockholdersEquity` は 2019-09-29 で終わっており、
    概念内だけを見ると古さに気づけず **6年前の純資産が最新として出た**。
    会社全体の最新日と比べる必要がある。
    """

    def _fake_concepts(self, mapping):
        def fake(cik, tag):
            return _concept(mapping.get(tag, []))
        return fake

    def test_newest_tag_wins_for_instant_facts(self, monkeypatch):
        monkeypatch.setenv("SEC_EDGAR_UA", "tester tester@example.com")
        monkeypatch.setattr(edgar_client, "resolve_cik", lambda s: "0000804328")
        monkeypatch.setattr(edgar_client, "_concept", self._fake_concepts({
            "Revenues": [{"start": "2024-09-30", "end": "2025-09-28", "val": 44,
                          "form": "10-K", "fp": "FY"}],
            # 旧タグ: 2019年で止まっている
            "StockholdersEquity": [{"start": None, "end": "2019-09-29", "val": 4909,
                                    "form": "10-K", "fp": "FY"}],
            # 新タグ: 現在も使われている
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest":
                [{"start": None, "end": "2025-09-28", "val": 21206,
                  "form": "10-K", "fp": "FY"}],
        }))
        result = edgar_client.key_financials("QCOM")
        assert result["facts"]["equity"]["value"] == 21206
        assert result["facts"]["equity"]["end"] == "2025-09-28"

    def test_stale_fact_is_flagged_against_company_latest(self, monkeypatch):
        monkeypatch.setenv("SEC_EDGAR_UA", "tester tester@example.com")
        monkeypatch.setattr(edgar_client, "resolve_cik", lambda s: "0000804328")
        monkeypatch.setattr(edgar_client, "_concept", self._fake_concepts({
            "Revenues": [{"start": "2024-09-30", "end": "2025-09-28", "val": 44,
                          "form": "10-K", "fp": "FY"}],
            "StockholdersEquity": [{"start": None, "end": "2019-09-29", "val": 4909,
                                    "form": "10-K", "fp": "FY"}],
        }))
        result = edgar_client.key_financials("QCOM")
        # 概念内では最新でも、他項目より6年遅れている
        assert result["facts"]["equity"]["stale_years"] >= 5
        assert result["stale"], "廃止タグの古さを検出できていない"
        assert "古い値が混ざっています" in result["note"]

    def test_missing_tag_is_not_reported_as_zero(self, monkeypatch):
        monkeypatch.setenv("SEC_EDGAR_UA", "tester tester@example.com")
        monkeypatch.setattr(edgar_client, "resolve_cik", lambda s: "0000804328")
        monkeypatch.setattr(edgar_client, "_concept", self._fake_concepts({
            "Revenues": [{"start": "2024-09-30", "end": "2025-09-28", "val": 44,
                          "form": "10-K", "fp": "FY"}],
        }))
        result = edgar_client.key_financials("QCOM")
        assert "equity" in result["missing"]
        assert "equity" not in result["facts"]
        assert "0ではありません" in result["note"]

    def test_no_facts_at_all_suggests_etf(self, monkeypatch):
        monkeypatch.setenv("SEC_EDGAR_UA", "tester tester@example.com")
        monkeypatch.setattr(edgar_client, "resolve_cik", lambda s: "0001")
        monkeypatch.setattr(edgar_client, "_concept", lambda cik, tag: None)
        result = edgar_client.key_financials("SOXL")
        assert result["available"] is False
        # ETF は財務三表が「取得失敗」ではなく「存在しない」
        assert "存在しない" in result["reason"]


def test_concept_404_is_quiet(monkeypatch):
    """XBRL のタグ探索での 404 は正常系。警告を出すと障害に見える。"""
    monkeypatch.setenv("SEC_EDGAR_UA", "tester tester@example.com")

    class Res:
        status_code = 404

        def json(self):
            return {}

    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **k: Res())
    edgar_client._warned[0] = False
    assert edgar_client._get("https://data.sec.gov/x", quiet_404=True) is None
    assert edgar_client._warned[0] is False, "404 で警告を出している"
