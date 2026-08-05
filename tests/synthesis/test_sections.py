"""節ごとの回帰テスト -- 検査そのものが機能しているかを縛る (改善1).

## ここで測っているもの

`claude -p` は**呼ばない**。呼ぶと (a) CI が課金され (b) 出力が毎回変わって回帰テストに
ならない。ここで縛るのは「assertions.py が、既知の悪い文章を落とし、既知の良い文章を
通すか」であり、**評価軸そのものの回帰**である。

実際の synthesis 出力を評価するのは `scripts/eval_synthesis.py`（週1回・手動/スケジュール）。

## 良い文章 / 悪い文章

各検査について「その原則を破った文章」と「守った文章」を対で持つ。
プロンプトを触って検査が甘くなったとき、ここが赤くなる。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.synthesis import assertions as A


FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
def quiet_pack() -> dict:
    return load("pack_quiet_week")


@pytest.fixture
def busy_pack() -> dict:
    return load("pack_busy_week")


@pytest.fixture
def circular_pack() -> dict:
    return load("pack_circular")


@pytest.fixture
def unavailable_pack() -> dict:
    return load("pack_unavailable")


@pytest.fixture
def orphan_pack() -> dict:
    return load("pack_orphan")


# ---------------------------------------------------------------------------
# fixture の健全性（fixture が壊れたら検査が空振りするので先に縛る）
# ---------------------------------------------------------------------------


def test_fixtures_have_expected_shape(
    quiet_pack, busy_pack, circular_pack, unavailable_pack, orphan_pack
):
    assert quiet_pack["information"]["quiet"] is True
    assert busy_pack["information"]["quiet"] is False
    assert circular_pack["reconciliation"]["status"] == "circular"
    assert circular_pack["reconciliation"]["independently_verified"] is False
    assert unavailable_pack["execution_audit"]["survival"]["available"] is False
    assert len(orphan_pack["reconciliation"]["orphans"]) == 6
    assert orphan_pack["reconciliation"]["orphan_burden_pct"] == 79.1


def test_unavailable_subjects_are_detected(unavailable_pack):
    paths = {s["path"] for s in A.unavailable_subjects(unavailable_pack)}
    # 別名を定義してあるものは拾える
    assert "execution_audit.survival" in paths
    assert "model_audit.score" in paths
    assert "forward.triggers" in paths
    # 銘柄ごとにぶら下がるものは配下のパスで拾う
    assert any(p.startswith("narrative.crowding") for p in paths)
    assert any(p.startswith("schedule_status") for p in paths)


def test_quiet_pack_has_no_unavailable_subjects(quiet_pack):
    assert A.unavailable_subjects(quiet_pack) == []


# ---------------------------------------------------------------------------
# §16-1 取得失敗を結果と混同しない
# ---------------------------------------------------------------------------

BAD_UNAVAILABLE = """## 7. 監査 — 執行と模型の健全性

- 決定生存率: 執行率0%。判断した58件のうち実行できたものはありません。
- 模型健全性: 系統的バイアスは0件で、問題なしです。
- トリガー: 接近・成立ともになし。
"""

GOOD_UNAVAILABLE = """## 7. 監査 — 執行と模型の健全性

- 決定生存率: **取得できませんでした**。約定履歴が無いため計算できず、
  これは「執行率0%」ではなく「測れていない」です。
- 模型健全性: データ蓄積中（4/26週）。26週未満で結論を出すと偶然を欠陥と誤認します。
- トリガー: 株価の一部が取得できず評価できませんでした（判定不能）。
"""


def test_unavailable_as_zero_is_caught(unavailable_pack):
    res = A.check_no_unavailable_as_zero(BAD_UNAVAILABLE, unavailable_pack)
    assert res["status"] == A.FAIL
    assert res["evidence"]


def test_unavailable_written_honestly_passes(unavailable_pack):
    res = A.check_no_unavailable_as_zero(GOOD_UNAVAILABLE, unavailable_pack)
    assert res["status"] == A.PASS


def test_unavailable_check_skips_when_nothing_failed(quiet_pack):
    res = A.check_no_unavailable_as_zero(BAD_UNAVAILABLE, quiet_pack)
    # 取得失敗が無いパックでは判定対象そのものが無い。pass ではなく skip。
    assert res["status"] == A.SKIP


def test_assert_wrapper_raises_on_failure(unavailable_pack):
    with pytest.raises(AssertionError, match="取得できなかった"):
        A.assert_no_unavailable_as_zero(BAD_UNAVAILABLE, unavailable_pack)


# ---------------------------------------------------------------------------
# §16-3 循環照合を「一致」と呼ばない
# ---------------------------------------------------------------------------

BAD_CIRCULAR = """## 1. 照合 — 模型は現実と一致しているか

✅ 一致。口座3銘柄・模型3銘柄がすべて合致しており、差分なしです。照合OK。
"""

GOOD_CIRCULAR = """## 1. 照合 — 模型は現実と一致しているか

🟡 差分は0件ですが、これは**独立検証ではありません**。照合に使ったCSVは模型の
生成元と同一ファイルであり、差分が0なのは当然です。次回は取り込む前に
`run_portfolio.py reconcile` を実行してください。
"""


def test_circular_claimed_as_match_is_caught(circular_pack):
    res = A.check_circular_disclosed(BAD_CIRCULAR, circular_pack)
    assert res["status"] == A.FAIL


def test_circular_disclosed_passes(circular_pack):
    res = A.check_circular_disclosed(GOOD_CIRCULAR, circular_pack)
    assert res["status"] == A.PASS


def test_circular_check_skips_when_independently_verified(quiet_pack):
    res = A.check_circular_disclosed(BAD_CIRCULAR, quiet_pack)
    assert res["status"] == A.SKIP


# ---------------------------------------------------------------------------
# §16-5 土曜は買い推奨を出さない
# ---------------------------------------------------------------------------

BAD_BUY = """### クアルコム（QCOM）

バリュエーションは魅力的であり、今が買い場です。追加で50株を買うべきでしょう。
"""

GOOD_BUY = """### クアルコム（QCOM）

翌週決めること: 月曜寄付で $172.12 を割れたなら弾再生の順序を MDT 優先に固定する。
条件を外れたら見送り、次の土曜に再評価する。**この節に買い推奨は書かない。**
"""


def test_unconditional_buy_recommendation_is_caught():
    res = A.check_no_buy_recommendation(BAD_BUY, {})
    assert res["status"] == A.FAIL
    assert res["evidence"]


def test_conditional_policy_is_not_flagged_as_buy():
    res = A.check_no_buy_recommendation(GOOD_BUY, {})
    assert res["status"] == A.PASS


def test_negated_buy_language_is_not_flagged():
    text = "第0原則により、この節に買い推奨を書くことは禁止されている。"
    assert A.check_no_buy_recommendation(text, {})["status"] == A.PASS


# ---------------------------------------------------------------------------
# §7.4 静穏週は30行以内
# ---------------------------------------------------------------------------

QUIET_REPORT_OK = """---
title: 週次PF分析 2026-08-01
---

# 週次ポートフォリオ深掘り分析 2026-08-01

## 0. 今週の判定

■ 静穏週（要対応 0件 / 点検 42項目）。
先週からの実質的な変化はありません。今週は何もしないことが正しい選択です。

## 1. 照合

一致（独立検証済み）。口座4銘柄・模型4銘柄。

## 2. 信念の変化

反証条件に抵触した保有 0件（4件が健在）。

## 3. 前方イベント

翌週の確定イベントはありません。政策カバレッジの穴もありません。

## 4. 制約

現金 ¥380,000（用途割当済み）。NISA残枠あり。

## 5. 機会

[折り畳み] 銘柄別の詳細 2件。過熱・売られすぎともに該当なし。

## 6. 事前決定

翌週は「何もしない」を明示的な決定とする。

## 7. 監査

[折り畳み] 執行・模型ともに前週から変化なし。

## 8. 前提と限界

予測レンジは統計的前提であり予言ではありません。
"""


def test_quiet_week_within_budget_passes(quiet_pack):
    res = A.check_quiet_week_length(QUIET_REPORT_OK, quiet_pack)
    assert res["status"] == A.PASS


def test_quiet_week_overlong_is_caught(quiet_pack):
    bloated = QUIET_REPORT_OK + "\n" + "\n".join(f"- 追記行 {i}" for i in range(40))
    res = A.check_quiet_week_length(bloated, quiet_pack)
    assert res["status"] == A.FAIL
    assert "30行" in res["message"]


def test_quiet_length_skips_for_single_section(quiet_pack):
    # 節単位では全体分量を判定できない。pass にすると通過率が嘘になる。
    res = A.check_quiet_week_length("## 1. 照合\n\n一致。", quiet_pack)
    assert res["status"] == A.SKIP


def test_quiet_length_skips_on_busy_week(busy_pack):
    assert A.check_quiet_week_length(QUIET_REPORT_OK, busy_pack)["status"] == A.SKIP


# ---------------------------------------------------------------------------
# §17.2 孤児ポジション
# ---------------------------------------------------------------------------

BAD_ORPHAN = """## 1. 照合 — 模型は現実と一致しているか

一致（独立検証済み）。口座9銘柄・模型9銘柄がすべて突合しました。差分はありません。
"""

GOOD_ORPHAN = """## 1. 照合 — 模型は現実と一致しているか

残高は一致（独立検証済み）。ただし⚠️ **孤児ポジションが6件、評価額の79.1%**を占めます。
SOXL・TECL・TQQQ・FANG+・ニトリは thesis も政策も無く、**なぜ持っているかが未記述**です。
MDT は thesis はありますが政策が無く、撤退・利確の判断基準がありません。
"""


def test_orphans_not_flagged_is_caught(orphan_pack):
    res = A.check_orphan_flagged(BAD_ORPHAN, orphan_pack)
    assert res["status"] == A.FAIL
    assert "SOXL" in res["evidence"]


def test_orphans_flagged_passes(orphan_pack):
    res = A.check_orphan_flagged(GOOD_ORPHAN, orphan_pack)
    assert res["status"] == A.PASS


def test_orphan_check_skips_without_orphans(quiet_pack):
    assert A.check_orphan_flagged(BAD_ORPHAN, quiet_pack)["status"] == A.SKIP


# ---------------------------------------------------------------------------
# §7.1 固定骨格
# ---------------------------------------------------------------------------

BAD_ORDER = """## 0. 今週の判定
要対応週。

## 5. 機会 — 保有の立ち位置
SOXL は…

## 1. 照合 — 模型は現実と一致しているか
一致。

## 4. 制約 — 行動可能な空間
現金は…
"""


def test_opportunity_before_reconcile_is_caught():
    res = A.check_section_order(BAD_ORDER)
    assert res["status"] == A.FAIL
    assert "機会" in res["message"]


def test_fixed_skeleton_order_passes():
    assert A.check_section_order(QUIET_REPORT_OK)["status"] == A.PASS


def test_section_order_skips_for_single_section():
    assert A.check_section_order("## 1. 照合\n\n一致。")["status"] == A.SKIP


# ---------------------------------------------------------------------------
# §16-2 窓の違う量を比較しない（四半期YoY vs 年度）
# ---------------------------------------------------------------------------

BAD_GROWTH = """### クアルコム（QCOM）

利益成長率は +1043% と極めて強い。増収率も二桁を維持している。
"""

GOOD_GROWTH = """### クアルコム（QCOM）

yfinance が返す利益成長率 +1043% は**四半期YoY**であり、前年同期の落ち込みによる
スパイクである。年度基準（`growth_annual`）では +21% であり、こちらを主として読む。
期間が違うので同じ行に並べない。
"""


def test_growth_spike_without_window_label_is_caught(busy_pack):
    res = A.check_growth_window_labeled(BAD_GROWTH, busy_pack)
    assert res["status"] == A.FAIL
    assert "QCOM" in res["message"]


def test_growth_spike_with_window_label_passes(busy_pack):
    res = A.check_growth_window_labeled(GOOD_GROWTH, busy_pack)
    assert res["status"] == A.PASS


def test_growth_check_skips_without_spike(quiet_pack):
    assert A.check_growth_window_labeled(BAD_GROWTH, quiet_pack)["status"] == A.SKIP


def test_growth_check_passes_when_spike_not_quoted(busy_pack):
    res = A.check_growth_window_labeled("### QCOM\n\n値動きは横ばい。", busy_pack)
    assert res["status"] == A.PASS


# ---------------------------------------------------------------------------
# §12.2 退避キャッシュの鮮度を伏せない
# ---------------------------------------------------------------------------

BAD_CACHE = """## 3. 前方イベント

- 水 8/5: **FOMC 政策金利発表**。据え置き確率 79%。
- 金 8/7: 米雇用統計（市場予想 +14.0万人）。
"""

GOOD_CACHE = """## 3. 前方イベント

- 水 8/5: **FOMC 政策金利発表**。据え置き確率 79%。
  ⚠️ この日程は moomoo が落ちた週の**退避キャッシュ**（取得から168時間経過）であり、
  最新の値ではありません。日程は変更され得ます。
- 金 8/7: 米雇用統計。出所は同じ退避キャッシュ。
"""


def test_cached_macro_without_age_is_caught(unavailable_pack):
    res = A.check_cache_age_disclosed(BAD_CACHE, unavailable_pack)
    assert res["status"] == A.FAIL


def test_cached_macro_with_age_passes(unavailable_pack):
    res = A.check_cache_age_disclosed(GOOD_CACHE, unavailable_pack)
    assert res["status"] == A.PASS


def test_cache_check_skips_without_cached_material(busy_pack):
    # busy_pack の FOMC は source="moomoo"（退避ではない）
    assert A.check_cache_age_disclosed(BAD_CACHE, busy_pack)["status"] == A.SKIP


# ---------------------------------------------------------------------------
# レジストリと集計
# ---------------------------------------------------------------------------


def test_run_checks_filters_by_section_kind(unavailable_pack):
    names = {r["name"] for r in A.run_checks("本文", unavailable_pack, section_kind="reconcile")}
    assert "orphan_flagged" in names
    assert "section_order" not in names       # 全体レポートにしか適用しない
    assert "growth_window_labeled" not in names


def test_run_checks_report_kind_runs_everything(unavailable_pack):
    names = {r["name"] for r in A.run_checks("本文", unavailable_pack, section_kind="report")}
    assert names == set(A.CHECKS)


def test_summarize_excludes_skips_from_denominator():
    results = [
        A.result("a", A.PASS, "p", "ok"),
        A.result("b", A.FAIL, "p", "ng"),
        A.result("c", A.SKIP, "p", "対象外"),
    ]
    summary = A.summarize(results)
    assert summary["judged"] == 2
    assert summary["pass_rate"] == 0.5
    assert summary["skipped"] == 1
    assert summary["ok"] is False


def test_summarize_pass_rate_is_none_when_nothing_judged():
    summary = A.summarize([A.result("a", A.SKIP, "p", "対象外")])
    assert summary["pass_rate"] is None
    assert summary["ok"] is True


def test_assert_all_reports_every_failure(unavailable_pack):
    with pytest.raises(AssertionError) as exc:
        A.assert_all(BAD_UNAVAILABLE + "\n" + BAD_CACHE, unavailable_pack, "report")
    assert "no_unavailable_as_zero" in str(exc.value)


def test_check_never_raises_on_broken_pack():
    # 検査自身の事故でレポート評価を止めない
    results = A.run_checks("本文", {"reconciliation": "壊れた型"}, section_kind="report")
    assert all(r["status"] in (A.PASS, A.FAIL, A.SKIP) for r in results)
