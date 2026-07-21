# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Design Philosophy

**このシステムは「自然言語ファースト」で設計されている。**

ユーザーはスラッシュコマンドやパラメータを覚える必要はない。日本語で意図を伝えるだけで、適切なスキルが自動的に選択・実行される。

- 「いい日本株ある？」→ スクリーニングが走る
- 「トヨタってどう？」→ 個別レポートが出る
- 「PF大丈夫かな」→ ヘルスチェックが実行される
- 「改善点ある？」→ システム自身を分析して提案する

スキル（`/screen-stocks` 等）はあくまで内部実装であり、ユーザーインターフェースではない。自然言語からの意図推論が第一の入口であり、コマンドは補助手段に過ぎない。

新機能を追加する際は、**ユーザーがどんな言葉でその機能を呼び出すか**を常に考え、`intent-routing.md` にその表現を反映すること。

## Project Overview

割安株スクリーニングシステム。Yahoo Finance API（yfinance）を使って日本株・米国株・ASEAN株・香港株・韓国株・台湾株等60地域から割安銘柄をスクリーニングする。Claude Code Skills として動作し、自然言語で話しかけるだけで適切な機能が実行される。

## Commands

各スキルのコマンド詳細は [docs/skill-catalog.md](docs/skill-catalog.md) を参照。

### 代表コマンド
```bash
# スクリーニング
python3 .claude/skills/screen-stocks/scripts/run_screen.py --region japan --preset alpha --top 10

# 個別レポート
python3 .claude/skills/stock-report/scripts/generate_report.py 7203.T

# ポートフォリオ
python3 .claude/skills/stock-portfolio/scripts/run_portfolio.py snapshot

# テスト
python3 -m pytest tests/ -q

# 依存インストール
pip install -r requirements.txt
```

## Architecture

詳細は [docs/architecture.md](docs/architecture.md)（3層構成・Mermaid図）、[docs/neo4j-schema.md](docs/neo4j-schema.md)（グラフスキーマ）、[docs/skill-catalog.md](docs/skill-catalog.md)（8スキル）を参照。

### レイヤー概要
<!-- BEGIN AUTO-GENERATED ARCHITECTURE -->
```
Skills (.claude/skills/*/SKILL.md → scripts/*.py) — 9スキル
Core   (src/core/) — decision/, health/, policy/, portfolio/, ports/, research/, risk/, screening/, action_item_bridge, action_item_detector, common, health_check, health_etf, health_labels, lesson_gate, market_dashboard, models, proactive_engine, return_estimate, technicals, temporal, ticker_utils, value_trap
Data   (src/data/) — context/, graph_query/, graph_store/, grok_client/, history/, yahoo_client/, embedding_client, lesson_community, lesson_conflict, linear_client, note_manager, question_log, rakuten_rss, user_profile
Output (src/output/) — adjust_formatter, analyze_formatter, forecast_formatter, formatter, health_formatter, portfolio_formatter, rebalance_formatter, research_formatter, review_formatter, screening_summary_formatter, simulate_formatter, stress_formatter, sync, verify, weekly_formatter

Config: config/screening_presets.yaml (16 presets), config/exchanges.yaml (60+ regions)
Rules:  .claude/rules/ (graph-context, intent-routing, workflow, development, screening, portfolio, testing)
Docs:   docs/ (architecture, neo4j-schema, skill-catalog, api-reference, data-models)
```
<!-- END AUTO-GENERATED ARCHITECTURE -->

## Post-Implementation Rule

**機能実装後は必ずドキュメント・ルールを更新すること。** 詳細は `.claude/rules/workflow.md` の「7. ドキュメント・ルール更新」を参照。

自動生成: `docs/api-reference.md`、`CLAUDE.md` Architecture、`development.md` テスト数、`docs/skill-catalog.md` 概要（pre-commit hook で自動実行）
手動更新: `intent-routing.md`、該当 `SKILL.md`、`rules/*.md`、`README.md`
## Obsidian Vault / 分析結果の保存ルール

**唯一の正しい保存先**: `C:\Users\swend\iCloudDrive\swender\投資記録\`
（`.obsidian/` フォルダを持つ実 vault はこちら。旧記載の `iCloudDrive\Obsidian\swender` は vault ではないため使わない — KIK upgrade v1.0 で統一）

- 分析結果は必ず「投資記録」フォルダに自動保存する（毎回指示しなくてよい）。
- ファイル名: `銘柄または分析種別_YYYYMMDD.md`（例: `7203T_分析_20260718.md`）
- フォーマット: Obsidian Markdown（frontmatter + wikiリンク）。frontmatter は `title` / `tags` / `created` を含める。

### 保存＝検証の統一入口（upgrade v1.0 Phase 2）

分析 Markdown を「output/ 保存 → vault 同期 → 実在＋体裁検証」まで一括で行う統一 CLI:

```bash
python scripts/save_report.py --name "7203T_分析_YYYYMMDD.md" --stdin < report.md
```

- 保存先は `config/output.yaml` の `obsidian_vault_path` を参照（上記の実 vault パス）。
- 非破壊: 同名ファイルがあれば上書きせず `_v2` 等を付けて保存。
- vault 未設定/不在なら `output/` のみで完了し案内を表示（graceful degradation）。
- 検証 NG（未達・空・体裁不備）なら exit code 1。「完了＝実物が届き検証済み」を保証する。
- Python から使う場合: `from src.output.sync import save_and_sync`。

## 投資ポートフォリオ（ユーザー保有 / 投資作業時は毎回参照すること）

**運用ルール**: 投資関連の作業（分析・レビュー・レポート・スクリーニング・ストレステスト等）では、毎回このセクションを保有銘柄の前提として参照すること。株価更新時は、ユーザーが新しい値を渡すか Web 検索が使える環境なら最新値を取得し、評価額・損益・各比率を再計算する。**基準日 (2026/6/13) の値は触らず、更新分は「### 株価更新ログ」に別途追記する形にすること。**

### 基準スナップショット（基準: 2026/6/13 12:24 楽天証券スクショ実値 / 為替 ¥160.23/USD）

#### サマリー
- 総資産: ¥27,227,700
- 保有評価額合計: ¥23,417,950 / 預り金(USD現金): ¥3,809,750
- 評価損益: +¥14,338,210 / 実現損益: +¥5,629,194（課税区分の特定/NISA別は未確認）

#### 保有銘柄
| 銘柄 | コード | 口座 | 数量 | 取得単価 | 現在値(6/13) | 評価額(¥) | 損益(¥) |
|---|---|---|---|---|---|---|---|
| SOXL (半導体3x) | — | 特定 | 275株 | $29.7495 | $234.68 | 10,340,763 | +9,156,983 |
| TECL (テック3x) | — | 特定 | 154株 | $105.3282 | $215.49 | 5,317,306 | +2,825,689 |
| TQQQ (ナス100 3x) | — | 特定 | 214株 | $26.8726 | $77.52 | 2,658,100 | +1,822,644 |
| 味の素 | 2802 | 特定 | 400株 | ¥3,906.03 | ¥5,158 | 2,063,200 | +500,785 |
| 味の素 | 2802 | NISA成長 | 39株 | ¥4,813.00 | ¥5,158 | 201,162 | +13,455 |
| ニトリHD | 9843 | 特定 | 345株 | ¥3,477.00 | ¥2,690.5 | 928,222 | -271,342 |
| トーメンデバイス | 2737 | 特定 | 40株 | ¥14,230.00 | ¥14,490 | 579,600 | +10,400 |
| iFreeNEXT FANG+ | — | NISAつみたて | 144,592口 | ¥72,618.13 | ¥91,955 | 1,329,596 | +279,596 |
| 米ドル現金(INTC弾) | — | — | $23,776.76 | — | ¥160.23/USD | 3,809,750 | — |

※新電元(6844)は保有なし。FANG+は¥5万/月の積立(DCA)継続中。

#### 構成（資産合計比）
- セクター: 米テック・半導体 約72%（トーメン含め約74%）/ 現金USD 14.0% / 日本・食品(味の素) 8.3% / 日本・小売(ニトリ) 3.4% / 日本・半導体商社(トーメン) 2.1%
- 国別: 米国連動 約86.2%（うちUSD直接 81.3%）/ 日本 13.9%
- レバレッジ調整後の実効テック/半導体エクスポージャー: 純資産比 約207%（3xを原資産換算×3した一次近似）

### 運用ルール（「株式介護手法」）
- 3xスリーブ(SOXL/TECL/TQQQ)は損切りなし・下落で株数積み増し・10年軸。評価額1億円で全利確しVT/VTI等へ移行。
- FANG+は壊れない1倍の保険コア（非売却・非課税複利）。日本株は円建ての流動性枠。
- INTC弾(USD現金)は逆回転の大底用に温存。投入はFOMC等の関門通過後に1/3ずつ分割。

### 株価更新ログ
（更新時はここに「YYYY/MM/DD: 銘柄 旧値→新値、再計算した評価額・損益・比率」を追記。基準スナップショットは改変しない）