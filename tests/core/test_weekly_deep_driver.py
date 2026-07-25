"""週次深掘り駆動スクリプトのテスト。

外部プロセス（claude CLI）は呼ばない。節の組み立て・材料スライス・
中断判定・レポート組み立てといった純ロジックだけを検証する。
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load_driver():
    path = REPO / "scripts" / "weekly_deep_driver.py"
    spec = importlib.util.spec_from_file_location("weekly_deep_driver", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["weekly_deep_driver"] = mod
    spec.loader.exec_module(mod)
    return mod


driver = _load_driver()


@pytest.fixture
def pack():
    return {
        "pack_version": 1,
        "mode": "portfolio",
        "meta": {"as_of": "2026-07-25", "fx_rate": 163.67,
                 "holdings_source": "rakuten_csv", "warnings": ["ニュース取得できず"]},
        "portfolio": {"total_jpy": 27_000_000, "cash_jpy": 10_000,
                      "total_pl_jpy": 14_000_000, "pl_pct": 108.5},
        "holdings": [
            {"symbol": "SOXL", "name": "SOXL", "weight_pct": 30.0, "value_jpy": 7_000_000,
             "pl_pct": 400.0, "week_change_pct": -2.5, "leverage": 3,
             "technicals": {"rsi14": 55.0}, "wow_delta": {"prior_date": "2026-07-18"},
             "competitors": {"peers": [{"symbol": "NVDA"}]}},
            {"symbol": "2802.T", "name": "味の素", "weight_pct": 8.0, "value_jpy": 2_000_000,
             "pl_pct": 23.0, "week_change_pct": 0.5, "leverage": 1,
             "technicals": {"rsi14": 48.0}, "wow_delta": None},
        ],
        "holding_news": {"SOXL": [{"title": "半導体上昇"}]},
        "market_news": [{"title": "FOMC据え置き"}],
        "indices": [{"symbol": "^SOX", "change_pct": 1.2}],
        "forward_schedule": [
            {"kind": "earnings", "symbol": "SOXL", "title": "SOXL 決算"},
            {"kind": "fomc", "title": "FOMC"},
        ],
        "moomoo": {},
        "prior_context": "過去テーゼ: 3xは損切りなし",
        "projection": {"short": {}},
        "scenarios": [],
        "positions_assumptions": [],
    }


class TestBuildSections:
    def test_holdings_become_sections_in_weight_order(self, pack):
        sections = driver.build_sections(pack)
        holding_ids = [s["id"] for s in sections if s["kind"] == "holding"]
        assert holding_ids == ["holding_SOXL", "holding_2802_T"]

    def test_summary_is_written_last_but_ordered_first(self, pack):
        sections = driver.build_sections(pack)
        assert sections[-1]["id"] == "summary"
        assert min(s["order"] for s in sections) == sections[-1]["order"]

    def test_body_dependent_sections_flagged(self, pack):
        sections = driver.build_sections(pack)
        needs = {s["id"] for s in sections if s.get("needs_body")}
        assert needs == {"actions", "limits", "summary"}

    def test_symbol_less_fund_still_gets_a_section(self, pack):
        """ティッカーが無い投信（FANG+等）を落とさない。落とすと保有が黙って消える。"""
        pack["holdings"].append(
            {"symbol": None, "name": "iFreeNEXT FANG+インデックス",
             "account": "NISAつみたて", "weight_pct": 6.5, "value_jpy": 1_400_000}
        )
        sections = driver.build_sections(pack)
        fund = [s for s in sections
                if s["kind"] == "holding" and s.get("symbol") is None]
        assert len(fund) == 1
        assert "FANG+" in fund[0]["heading"]

        material = driver.slice_pack(pack, fund[0])
        assert len(material["holding_rows"]) == 1
        # 銘柄固有イベントは混ぜない（FOMC 等の全体イベントだけ残る）
        assert all(not e.get("symbol") for e in material["forward_schedule"])

    def test_same_symbol_in_two_accounts_becomes_one_section(self, pack):
        """特定口座＋NISA で同じ銘柄を持っていても節は1つ（合算で語る）。"""
        pack["holdings"].append(
            {"symbol": "2802.T", "name": "味の素", "account": "NISA成長",
             "weight_pct": 1.0, "value_jpy": 200_000, "pl_pct": 5.0}
        )
        sections = driver.build_sections(pack)
        assert [s["id"] for s in sections].count("holding_2802_T") == 1

        sec = next(s for s in sections if s.get("symbol") == "2802.T")
        material = driver.slice_pack(pack, sec)
        assert len(material["holding_rows"]) == 2
        assert material["aggregate"]["weight_pct"] == pytest.approx(9.0)
        assert material["aggregate"]["value_jpy"] == 2_200_000
        assert material["aggregate"]["accounts"] == [None, "NISA成長"]

    def test_no_holdings_still_produces_sections(self):
        sections = driver.build_sections({"holdings": []})
        assert [s["id"] for s in sections if s["kind"] == "holding"] == []
        assert any(s["id"] == "summary" for s in sections)


class TestSlicePack:
    def test_holding_slice_targets_one_symbol(self, pack):
        sec = next(s for s in driver.build_sections(pack) if s.get("symbol") == "SOXL")
        material = driver.slice_pack(pack, sec)
        assert [r["symbol"] for r in material["holding_rows"]] == ["SOXL"]
        assert material["news"] == [{"title": "半導体上昇"}]
        # 他銘柄の決算は落ち、銘柄非依存イベント(FOMC)は残る
        kinds = {e["kind"] for e in material["forward_schedule"]}
        assert kinds == {"earnings", "fomc"}

    def test_holding_slice_drops_other_symbols_events(self, pack):
        pack["forward_schedule"].append({"kind": "earnings", "symbol": "AAPL"})
        sec = next(s for s in driver.build_sections(pack) if s.get("symbol") == "2802.T")
        material = driver.slice_pack(pack, sec)
        symbols = {e.get("symbol") for e in material["forward_schedule"]}
        assert "AAPL" not in symbols

    def test_macro_slice_omits_per_holding_detail(self, pack):
        sec = next(s for s in driver.build_sections(pack) if s["kind"] == "macro")
        material = driver.slice_pack(pack, sec)
        assert "indices" in material and "market_news" in material
        assert "technicals" not in material["holdings_overview"][0]

    def test_actions_slice_is_meta_only(self, pack):
        sec = next(s for s in driver.build_sections(pack) if s["id"] == "actions")
        material = driver.slice_pack(pack, sec)
        assert set(material) == {"meta", "portfolio"}

    def test_every_section_slices_without_error(self, pack):
        for sec in driver.build_sections(pack):
            material = driver.slice_pack(pack, sec)
            json.dumps(material, ensure_ascii=False)  # 直列化できること


class TestLimitDetection:
    @pytest.mark.parametrize("text", [
        "Claude usage limit reached",
        "Rate limit exceeded",
        "使用量の上限に達しました",
        "Error: overloaded",
    ])
    def test_interruption_markers(self, text):
        assert driver.looks_like_limit(text) is True

    @pytest.mark.parametrize("text", ["file not found", "", "invalid json"])
    def test_non_interruption(self, text):
        assert driver.looks_like_limit(text) is False


class TestHeaderAndAssembly:
    def test_header_has_frontmatter_and_warning(self, pack):
        header = driver.build_header(pack)
        assert header.startswith("---")
        assert "title: 週次PF分析 2026-07-25" in header
        assert "⚠️ ニュース取得できず" in header

    def test_header_marks_missing_values_explicitly(self):
        header = driver.build_header({"meta": {}, "portfolio": {}})
        assert "取得できず" in header

    def test_assemble_orders_sections_and_skips_pending(self, tmp_path, pack):
        (tmp_path / "d").mkdir()
        (tmp_path / "d" / "a.md").write_text("## 0. サマリー\n本文A", encoding="utf-8")
        (tmp_path / "d" / "b.md").write_text("## 1. マクロ\n本文B", encoding="utf-8")
        state = {"sections": [
            {"id": "macro", "order": 20, "status": "done", "file": "d/b.md"},
            {"id": "summary", "order": 10, "status": "done", "file": "d/a.md"},
            {"id": "heat", "order": 60, "status": "pending", "file": None},
        ]}
        out = driver.assemble(state, tmp_path, pack)
        assert out.index("本文A") < out.index("本文B")
        assert "heat" not in out

    def test_assemble_adds_one_parent_heading_for_holdings(self, tmp_path, pack):
        (tmp_path / "d").mkdir()
        for n in ("x", "y"):
            (tmp_path / "d" / f"{n}.md").write_text(f"### {n}\n本文", encoding="utf-8")
        state = {"sections": [
            {"id": "h1", "kind": "holding", "order": 40, "status": "done", "file": "d/x.md"},
            {"id": "h2", "kind": "holding", "order": 41, "status": "done", "file": "d/y.md"},
        ]}
        out = driver.assemble(state, tmp_path, pack)
        assert out.count("## 3. 銘柄別の深掘り（保有比率順）") == 1

    def test_body_for_prompt_concatenates_done_only(self, tmp_path):
        (tmp_path / "x.md").write_text("済み", encoding="utf-8")
        state = {"sections": [
            {"order": 20, "status": "done", "file": "x.md"},
            {"order": 30, "status": "failed", "file": None},
        ]}
        assert driver.body_for_prompt(state, tmp_path) == "済み"


class TestPromptBuilding:
    def test_prompt_contains_spec_heading_and_material(self, pack):
        sec = next(s for s in driver.build_sections(pack) if s["kind"] == "macro")
        prompt = driver.build_prompt("仕様本文", sec, driver.slice_pack(pack, sec), "")
        assert "仕様本文" in prompt
        assert sec["heading"] in prompt
        assert "FOMC据え置き" in prompt

    def test_body_included_only_when_needed(self, pack):
        macro = next(s for s in driver.build_sections(pack) if s["kind"] == "macro")
        actions = next(s for s in driver.build_sections(pack) if s["id"] == "actions")
        assert "これまでに書いた本文" not in driver.build_prompt("s", macro, {}, "既存本文")
        assert "既存本文" in driver.build_prompt("s", actions, {}, "既存本文")


class TestState:
    def test_roundtrip(self, tmp_path):
        p = driver.state_path(tmp_path, "20260725")
        driver.save_state(p, {"date": "20260725", "sections": []})
        loaded = driver.load_state(p)
        assert loaded["date"] == "20260725"
        assert "updated_at" in loaded

    def test_missing_state_returns_none(self, tmp_path):
        assert driver.load_state(tmp_path / "nope.json") is None
