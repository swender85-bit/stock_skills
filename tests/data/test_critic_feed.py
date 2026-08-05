"""X 批評家フィードの取得・取り込み・採点のテスト (改善5).

## 何を縛っているか

1. **「発言が無かった」と「取得できなかった」を混同しない。**
   混ぜると、APIキー未設定やレート制限が「今週この人は何も言わなかった」に化ける。
2. **要約しない。** 原文をそのまま台帳に入れる（要約は自己推論の混入）。
3. **分野は発言ごとに分類する。** 「この人は需給の人」と決め打ちしない。
4. **採点できないものを採点しない。** 価格が取れなかったのを `refuted` にしない。
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from src.core.critic_calibration import (
    build_external_views,
    classify_domain,
    due_for_scoring,
    extract_verifiable,
    ingest_posts,
    load_critic,
    post_to_thesis,
    score_verifiable,
    unscorable_count,
)
from src.data.critic_feed import (
    enabled_critics,
    fetch_all,
    fetch_recent_posts,
    load_critics_config,
)


@pytest.fixture
def critics_dir(tmp_path):
    return str(tmp_path / "critics")


def _posts_json(rows) -> str:
    return json.dumps(rows, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------


class TestConfig:
    def test_five_accounts_are_registered(self):
        critics = enabled_critics(load_critics_config())
        handles = {c["handle"] for c in critics}
        assert handles == {"pirania0630", "noirinvestor", "kokko_coco",
                           "imuvill", "noatake1127"}

    def test_no_preassigned_expertise(self):
        """得意分野を先に決めない。実測がそれに上書きされる。"""
        for critic in enabled_critics(load_critics_config()):
            assert not critic.get("domains")
            assert not (critic.get("note") or "").strip()

    def test_missing_config_returns_empty(self, tmp_path):
        cfg = load_critics_config(str(tmp_path / "nope.yaml"))
        assert cfg["critics"] == []


# ---------------------------------------------------------------------------
# 取得
# ---------------------------------------------------------------------------


class TestFetch:
    def test_posts_are_parsed(self, monkeypatch):
        monkeypatch.setenv("XAI_API_KEY", "test-key")
        today = date.today().isoformat()
        raw = _posts_json([
            {"posted_at": today, "text": "半導体の需給が締まってきた",
             "url": "https://x.com/x/status/1", "symbols": ["SOXL"], "topic": "半導体"},
        ])
        res = fetch_recent_posts("someone", days=7, caller=lambda *a, **k: raw)
        assert res["available"] is True
        assert len(res["posts"]) == 1
        assert res["posts"][0]["text"] == "半導体の需給が締まってきた"

    def test_no_api_key_is_fetch_failure_not_silence(self, monkeypatch):
        monkeypatch.delenv("XAI_API_KEY", raising=False)
        res = fetch_recent_posts("someone", days=7)
        assert res["available"] is False
        assert res["posts"] == []
        assert "発言が無かった" in res["error"]   # 混同しないと明記している

    def test_empty_array_is_silence_not_failure(self, monkeypatch):
        monkeypatch.setenv("XAI_API_KEY", "test-key")
        res = fetch_recent_posts("someone", days=7, caller=lambda *a, **k: "[]")
        # 取得は成功して発言が無かった。これは情報として意味がある。
        assert res["available"] is True
        assert res["posts"] == []
        assert res["error"] is None

    def test_inaccessible_account_is_failure(self, monkeypatch):
        monkeypatch.setenv("XAI_API_KEY", "test-key")
        res = fetch_recent_posts("someone", days=7,
                                 caller=lambda *a, **k: '{"error": "inaccessible"}')
        assert res["available"] is False
        assert "アクセスできません" in res["error"]

    def test_empty_response_is_failure(self, monkeypatch):
        monkeypatch.setenv("XAI_API_KEY", "test-key")
        res = fetch_recent_posts("someone", days=7, caller=lambda *a, **k: "")
        assert res["available"] is False

    def test_old_posts_are_dropped(self, monkeypatch):
        monkeypatch.setenv("XAI_API_KEY", "test-key")
        old = (date.today() - timedelta(days=60)).isoformat()
        raw = _posts_json([{"posted_at": old, "text": "古い発言", "symbols": []}])
        res = fetch_recent_posts("someone", days=7, caller=lambda *a, **k: raw)
        assert res["posts"] == []

    def test_total_failure_is_distinguished_from_quiet_week(self, monkeypatch):
        monkeypatch.delenv("XAI_API_KEY", raising=False)
        result = fetch_all(days=7)
        assert result["available_sources"] == []
        assert "発言が無かった" in result["summary"]

    def test_partial_failure_names_the_failed_sources(self, monkeypatch):
        monkeypatch.setenv("XAI_API_KEY", "test-key")
        calls = {"n": 0}

        def caller(*_a, **_k):
            calls["n"] += 1
            return "[]" if calls["n"] % 2 else ""

        result = fetch_all(days=7, caller=caller)
        assert result["failed_sources"]
        assert "発言なしではありません" in result["summary"]


# ---------------------------------------------------------------------------
# 分野分類
# ---------------------------------------------------------------------------


class TestDomainClassification:
    @pytest.mark.parametrize("text,expected", [
        ("信用残と空売り比率から需給が締まっている", "supply_demand"),
        ("FOMCの利下げ観測で長期金利が低下", "macro"),
        ("決算は増収増益でガイダンスも上方修正", "fundamentals"),
        ("HBMの供給がAI需要に追いつかない", "technology"),
        ("関税の法案が通れば当局の規制が強まる", "regulation"),
    ])
    def test_classified_by_content(self, text, expected):
        assert classify_domain(text) == expected

    def test_unclassifiable_falls_back_to_sentiment(self):
        # 「分類できなかった」の逃げ場。精度を主張するものではない。
        assert classify_domain("おはようございます") == "sentiment"

    def test_same_person_can_have_different_domains(self, critics_dir):
        posts = [
            {"posted_at": "2026-08-01", "text": "需給が締まって信用残が減った", "symbols": []},
            {"posted_at": "2026-08-02", "text": "FOMCで利下げなら金利は低下する", "symbols": []},
        ]
        ingest_posts("critic_x", posts, base_dir=critics_dir)
        domains = {t["domain"] for t in load_critic("critic_x", critics_dir)["theses"]}
        assert domains == {"supply_demand", "macro"}


# ---------------------------------------------------------------------------
# 検証可能性の抽出
# ---------------------------------------------------------------------------


class TestVerifiableExtraction:
    def test_price_target_is_extracted(self):
        v = extract_verifiable("NVDAは$250まで行く", ["NVDA"])
        assert v["kind"] == "price_target"
        assert v["target"] == 250.0

    def test_yen_target_is_extracted(self):
        v = extract_verifiable("7203.T は3000円まで上がる", ["7203.T"])
        assert v["kind"] == "price_target"
        assert v["target"] == 3000.0

    def test_direction_is_extracted(self):
        v = extract_verifiable("SOXL は来週上昇する", ["SOXL"])
        assert v["kind"] == "direction"
        assert v["direction"] == "up"
        assert v["horizon_days"] == 7

    def test_no_symbol_means_not_verifiable(self):
        assert extract_verifiable("相場は上がる", []) is None

    def test_qualitative_claim_is_not_verifiable(self):
        # 定性的な主張を無理に採点対象にしない
        assert extract_verifiable("今の地合いは良くない", ["SOXL"]) is None

    def test_contradictory_direction_is_skipped(self):
        assert extract_verifiable("売りが出たが上昇する", ["SOXL"]) is None


# ---------------------------------------------------------------------------
# 取り込み
# ---------------------------------------------------------------------------


class TestIngest:
    def test_original_text_is_preserved(self, critics_dir):
        text = "半導体の需給が締まってきた。ただし在庫は多い。"
        ingest_posts("critic_x", [{"posted_at": "2026-08-01", "text": text,
                                   "symbols": []}], base_dir=critics_dir)
        thesis = load_critic("critic_x", critics_dir)["theses"][0]
        assert thesis["claim"] == text     # 要約していない

    def test_ingested_posts_start_as_pending(self, critics_dir):
        ingest_posts("critic_x", [{"posted_at": "2026-08-01", "text": "需給が締まる",
                                   "symbols": []}], base_dir=critics_dir)
        assert load_critic("critic_x", critics_dir)["theses"][0]["score"] == "pending"

    def test_duplicate_posts_are_skipped(self, critics_dir):
        post = {"posted_at": "2026-08-01", "text": "需給が締まる",
                "url": "https://x.com/x/status/1", "symbols": []}
        first = ingest_posts("critic_x", [post], base_dir=critics_dir)
        second = ingest_posts("critic_x", [post], base_dir=critics_dir)
        assert first["added"] == 1
        assert second["added"] == 0 and second["skipped"] == 1

    def test_dry_run_writes_nothing(self, critics_dir):
        res = ingest_posts("critic_x", [{"posted_at": "2026-08-01", "text": "需給",
                                         "symbols": []}],
                           base_dir=critics_dir, apply=False)
        assert res["added"] == 1
        assert load_critic("critic_x", critics_dir)["theses"] == []

    def test_verifiable_posts_get_a_deadline(self, critics_dir):
        res = ingest_posts("critic_x", [{"posted_at": "2026-08-01",
                                         "text": "NVDAは$250まで行く",
                                         "symbols": ["NVDA"]}], base_dir=critics_dir)
        assert res["verifiable"] == 1
        assert res["theses"][0]["verify_after"] == "2026-08-31"


# ---------------------------------------------------------------------------
# 採点
# ---------------------------------------------------------------------------


class TestScoring:
    def _thesis(self, **kw):
        base = post_to_thesis({"posted_at": "2026-07-01",
                               "text": "NVDAは$250まで行く", "symbols": ["NVDA"]})
        base.update(kw)
        return base

    def test_reaching_the_target_is_hit_exact(self):
        res = score_verifiable(self._thesis(), price_now=255.0, price_then=200.0)
        assert res["score"] == "hit_exact"
        assert "255" in res["evidence"]

    def test_right_direction_without_reaching_is_hit_direction(self):
        res = score_verifiable(self._thesis(), price_now=220.0, price_then=200.0)
        assert res["score"] == "hit_direction"

    def test_wrong_direction_is_refuted(self):
        res = score_verifiable(self._thesis(), price_now=180.0, price_then=200.0)
        assert res["score"] == "refuted"

    def test_missing_price_is_unscorable_not_refuted(self):
        # 取得失敗を「外れた」と記録すると台帳が汚染される
        assert score_verifiable(self._thesis(), None, 200.0) is None
        assert score_verifiable(self._thesis(), 250.0, None) is None

    def test_non_verifiable_thesis_is_not_scored(self):
        thesis = post_to_thesis({"posted_at": "2026-07-01",
                                 "text": "地合いが悪い", "symbols": []})
        assert score_verifiable(thesis, 250.0, 200.0) is None

    def test_direction_claim_scoring(self):
        thesis = post_to_thesis({"posted_at": "2026-07-01",
                                 "text": "SOXL は下落する", "symbols": ["SOXL"]})
        assert score_verifiable(thesis, 90.0, 100.0)["score"] == "hit_direction"
        assert score_verifiable(thesis, 110.0, 100.0)["score"] == "refuted"

    def test_due_list_respects_the_deadline(self, critics_dir):
        ingest_posts("critic_x", [{"posted_at": "2026-07-01",
                                   "text": "NVDAは$250まで行く",
                                   "symbols": ["NVDA"]}], base_dir=critics_dir)
        critic = load_critic("critic_x", critics_dir)
        assert due_for_scoring(critic, today=date(2026, 7, 15)) == []
        assert len(due_for_scoring(critic, today=date(2026, 8, 5))) == 1

    def test_unscorable_count_is_not_zero_when_qualitative(self, critics_dir):
        ingest_posts("critic_x", [{"posted_at": "2026-08-01",
                                   "text": "地合いが悪い", "symbols": []}],
                     base_dir=critics_dir)
        # 「採点対象が無い」ではなく「手で採点する対象が残っている」
        assert unscorable_count(load_critic("critic_x", critics_dir)) == 1


# ---------------------------------------------------------------------------
# レポートへの供給
# ---------------------------------------------------------------------------


class TestExternalViews:
    def test_missing_ledger_is_not_reported_as_silence(self, critics_dir):
        views = build_external_views(base_dir=critics_dir)
        assert views["available"] is False
        assert "誰も何も言っていない" in views["note"]

    def test_unmeasured_views_are_not_usable_as_evidence(self, critics_dir):
        ingest_posts("critic_x", [{"posted_at": date.today().isoformat(),
                                   "text": "SOXL の需給が締まる",
                                   "symbols": ["SOXL"]}], base_dir=critics_dir)
        views = build_external_views(base_dir=critics_dir, symbols=["SOXL"])
        assert views["available"] is True
        assert views["usable_count"] == 0
        assert all(v["usable_as_evidence"] is False for v in views["views"])
        assert "根拠に使えるものはありません" in views["note"]

    def test_views_are_grouped_by_symbol(self, critics_dir):
        ingest_posts("critic_x", [
            {"posted_at": date.today().isoformat(), "text": "SOXL の需給",
             "symbols": ["SOXL"]},
            {"posted_at": date.today().isoformat(), "text": "FOMCで利下げ観測",
             "symbols": []},
        ], base_dir=critics_dir)
        views = build_external_views(base_dir=critics_dir, symbols=["SOXL"])
        assert "SOXL" in views["by_symbol"]
        assert len(views["macro_views"]) == 1

    def test_unheld_symbols_are_excluded(self, critics_dir):
        ingest_posts("critic_x", [{"posted_at": date.today().isoformat(),
                                   "text": "AAPL の需給", "symbols": ["AAPL"]}],
                     base_dir=critics_dir)
        views = build_external_views(base_dir=critics_dir, symbols=["SOXL"])
        assert views["by_symbol"] == {}

    def test_all_views_are_external_discourse(self, critics_dir):
        ingest_posts("critic_x", [{"posted_at": date.today().isoformat(),
                                   "text": "決算は増益", "symbols": ["SOXL"]}],
                     base_dir=critics_dir)
        views = build_external_views(base_dir=critics_dir)
        # 本人が開示原文を引用していても、又聞きは又聞き
        assert all(v["provenance"] == "external_discourse" for v in views["views"])


# ---------------------------------------------------------------------------
# 最重要情報源（trust: primary）— 2026-08-06
# ---------------------------------------------------------------------------


class TestPrimaryTrust:
    """信用の宣言は**可視性**を変える。**的中率は変えない。**

    上書きしてしまうと、その人がどの分野で強くどこで弱いかが永久に分からなくなり、
    「最も信用している」の根拠が本人の記憶だけになる。測る意味が消える。
    """

    def test_pirania_is_the_primary_source(self):
        from src.data.critic_feed import trust_map

        tiers = trust_map()
        assert tiers["pirania0630"] == "primary"
        assert all(v == "standard" for k, v in tiers.items() if k != "pirania0630")

    def test_primary_is_fetched_wider_and_deeper(self, monkeypatch):
        monkeypatch.setenv("XAI_API_KEY", "test-key")
        seen: list[dict] = []

        def caller(prompt, **_kw):
            seen.append({"prompt": prompt})
            return "[]"

        from src.data.critic_feed import fetch_all

        fetch_all(caller=caller)
        primary = [s for s in seen if "@pirania0630" in s["prompt"]][0]
        other = [s for s in seen if "@noatake1127" in s["prompt"]][0]
        # 取りこぼしを減らす方向にだけ倒す
        assert "last 14 days" in primary["prompt"]
        assert "last 7 days" in other["prompt"]
        assert "at most 40 posts" in primary["prompt"]
        assert "at most 20 posts" in other["prompt"]

    def test_primary_failure_is_called_out_by_name(self, monkeypatch):
        monkeypatch.setenv("XAI_API_KEY", "test-key")

        def caller(prompt, **_kw):
            return "" if "@pirania0630" in prompt else "[]"

        from src.data.critic_feed import fetch_all

        result = fetch_all(caller=caller)
        assert result["primary_failed"] == ["pirania0630"]
        # 他が取れていても「材料が揃った週」と読ませない
        assert "最重要情報源" in result["summary"]
        assert "材料が揃った週ではありません" in result["summary"]

    def test_primary_view_is_citable_as_reference_while_unmeasured(self, critics_dir):
        from src.core.critic_calibration import citation_style

        standard = citation_style("noatake1127", "macro", critics_dir, trust="standard")
        primary = citation_style("pirania0630", "macro", critics_dir, trust="primary")

        # どちらも「根拠」にはならない
        assert standard["usable_as_evidence"] is False
        assert primary["usable_as_evidence"] is False
        # だが最重要情報源は参考見解として本文に載せてよい
        assert standard["usable_as_reference"] is False
        assert primary["usable_as_reference"] is True
        assert primary["style"] == "trusted_unverified"
        assert "未測定" in primary["prefix"]
        assert "判断を成立させない" in primary["note"]

    def test_declared_trust_never_overrides_measured_weight(self, critics_dir):
        from src.core.critic_calibration import (
            build_thesis,
            citation_style,
            domain_weight,
            save_critic,
        )

        # 実測で 0.6 を割っている分野（価格水準の断言 0勝5敗）
        save_critic({"source_id": "pirania0630", "name": "pirania0630", "theses": [
            build_thesis(f"主張{i}", "price_level", at="2026-07-01",
                         score="refuted", verified_on="2026-07-15")
            for i in range(5)
        ]}, critics_dir)
        w = domain_weight("pirania0630", "price_level", critics_dir)
        style = citation_style("pirania0630", "price_level", critics_dir, trust="primary")

        assert w["weight"] == 0.0
        # 信用の宣言で実測を覆さない
        assert style["usable_as_evidence"] is False
        assert style["style"] == "quotation"
        assert "実測は 0.6 を下回っている" in style["note"]

    def test_primary_views_come_first(self, critics_dir, monkeypatch):
        from src.core import critic_calibration as CC

        monkeypatch.setattr(
            "src.data.critic_feed.trust_map",
            lambda *a, **k: {"pirania0630": "primary", "noatake1127": "standard"})
        today = date.today().isoformat()
        ingest_posts("noatake1127", [{"posted_at": today, "text": "需給の話",
                                      "symbols": []}], base_dir=critics_dir)
        ingest_posts("pirania0630", [{"posted_at": today, "text": "金利の話",
                                      "symbols": []}], base_dir=critics_dir)

        views = CC.build_external_views(base_dir=critics_dir)
        assert views["views"][0]["source_id"] == "pirania0630"
        assert views["primary_sources"] == ["pirania0630"]

    def test_absent_primary_is_flagged_not_treated_as_silence(self, critics_dir, monkeypatch):
        from src.core import critic_calibration as CC

        monkeypatch.setattr(
            "src.data.critic_feed.trust_map",
            lambda *a, **k: {"pirania0630": "primary", "noatake1127": "standard"})
        ingest_posts("noatake1127", [{"posted_at": date.today().isoformat(),
                                      "text": "需給の話", "symbols": []}],
                     base_dir=critics_dir)
        # pirania の台帳ファイルだけ作って中身を空にする
        ingest_posts("pirania0630", [], base_dir=critics_dir)
        (Path(critics_dir) / "pirania0630.json").write_text(
            json.dumps({"source_id": "pirania0630", "theses": []}, ensure_ascii=False),
            encoding="utf-8")

        views = CC.build_external_views(base_dir=critics_dir)
        assert views["missing_primary"] == ["pirania0630"]
        assert "台帳にありません" in views["note"]
