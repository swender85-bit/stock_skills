# Stock Skills 構造リファレンス

このドキュメントは、リポジトリを**実走査して**書いた構造の全体像である。
数値（モジュール数・テスト数・行数）は 2026-08-02 時点の実測値。

- 行数・モジュール一覧: `find src -name "*.py" | xargs wc -l`
- 説明文: 各モジュールの docstring 1行目を AST で抽出
- テスト数: `pytest --co -q`
- スケジュール: `Get-ScheduledTask -TaskPath '\StockSkills\'`

関連: [architecture.md](architecture.md)（3層とMermaid図）/
[api-reference.md](api-reference.md)（自動生成の関数シグネチャ）/
[skill-catalog.md](skill-catalog.md)（スキル一覧）/
[neo4j-schema.md](neo4j-schema.md)（グラフスキーマ）/
[SATURDAY_UPGRADE_PROGRESS.md](SATURDAY_UPGRADE_PROGRESS.md)（土曜設計書の実装記録）

---

## 0. 全体像を1枚で

```
                        ユーザーの日本語
                             │
                             ▼
                 ┌───────────────────────┐
                 │  意図推論（第一の入口）│  .claude/rules/intent-routing.md
                 │  10ドメインに分類      │  + graph-context.md（過去の経緯を注入）
                 └───────────┬───────────┘
                             │
        ┌────────────────────┼────────────────────┐
        ▼                    ▼                    ▼
  ┌──────────┐      ┌────────────────┐    ┌──────────────┐
  │ 9スキル  │      │ 直接スクリプト │    │ 無人スケジュール│
  │ SKILL.md │      │ scripts/*.py   │    │ 土曜 07:12    │
  └────┬─────┘      └───────┬────────┘    └──────┬───────┘
       │                    │                     │
       └────────────────────┴─────────────────────┘
                            │
                            ▼
   ┌────────────────────────────────────────────────────┐
   │  Core (src/core/)  — ドメインロジック 100+モジュール │
   │  screening / portfolio / risk / research /          │
   │  health / policy / decision / ports                 │
   └────────────────┬───────────────────────────────────┘
                    │
        ┌───────────┴───────────┐
        ▼                       ▼
┌────────────────┐      ┌────────────────┐
│ Data (src/data)│      │ Output(src/output)│
│ 取得・永続化   │      │ 整形・保存・検証 │
└───────┬────────┘      └───────┬────────┘
        │                       │
        ▼                       ▼
  外部サービス            output/ → Obsidian vault
  yfinance / Finnhub      + 実在検証
  Grok / moomoo(OpenD)
  Neo4j / TEI / Linear
  楽天CSV
```

**規模（実測）**

| 対象 | 数 |
|:---|---:|
| `src/` の Python モジュール | 208ファイル / 47,938行 |
| スキル | 9 |
| `scripts/` の CLI | 24 Python + 2 バッチ |
| テストファイル | 183 |
| テスト数 | 4,584 |
| 設定ファイル | 14 |
| ルール（`.claude/rules/`） | 11 |
| スクリーニング preset | 16 |
| テーマ | 9 |
| Neo4j ノードタイプ | 24 |

---

## 1. 設計思想 — なぜこの形なのか

### 1.1 自然言語ファースト

**スキルはユーザーインターフェースではない。内部実装である。**

`/screen-stocks` のようなコマンド名を覚える必要はなく、「いい日本株ある？」で
スクリーニングが走る。新機能を足すときは「ユーザーがどんな言葉でそれを呼ぶか」を
`intent-routing.md` に必ず反映する。コマンドは補助手段にすぎない。

### 1.2 材料集めと解釈の分離（2層アーキテクチャ）

旧・週次レポートは Python の文字列整形で数字を並べるだけで、
**「材料 → 保有株への含意」をつなぐ解釈層が無かった**。そこで2層に割った。

```
層1  材料集め（Python・トークン0）    scripts/build_briefing_pack.py
     → output/briefing/PF_YYYYMMDD.json に全材料を束ねる

層2  synthesis（Claude・節単位）      .claude/prompts/weekly_deep.md
     → scripts/weekly_deep_driver.py が headless `claude -p` を節ごとに呼ぶ
```

**節単位である理由**: 1節=1回の `claude -p`。書けた節はファイルに落として
`state_YYYYMMDD.json` に記録するので、使用量上限に当たっても中断（exit 2）で済み、
次の起動が**続きから**再開する。人の操作は要らない。

### 1.3 取得失敗を結果と混同しない

**この設計で最も繰り返し踏んだ罠。** 実装中に7回踏んだ。

| 誤 | 正 |
|:---|:---|
| 「日程なし」 | 「日程を取得できなかった」 |
| 「約定0件 → 執行率0%」 | 「約定履歴を取得できなかった → 測定不能」 |
| 「出来高が取れない → 流動性が高い」 | 「判定不能」 |
| 「テクニカルが1つも取れない → 中立」 | 「判定不能」 |
| 「NISA使用額 = 保有取得額」 | 「推定値（reliable: false）」 |
| 「実現損益 0円」 | 「未記録（null）」 |
| 「前提が抽出できない → 分散している」 | 「unknown」 |

`available: false` は**「値が無い」ではなく「分からない」**を意味する。
`src/data/brokers/base.py` の `make_snapshot()` がこの規約の基点。

### 1.4 土曜の非対称性

土曜は **完全情報 × 執行不能** という週に一度の特異点。

- 東証の金曜引け（金 15:30）と米国の金曜引け（土 05〜06 JST）が両方確定
- 次に約定できるのは月曜 09:00 JST 以降 → 最低48時間の強制的な熟慮時間

> **このレポートは「いま買うべき銘柄」を出力してはならない。**

土曜に「A社買い推奨」と書いても執行は月曜以降で、その時点の価格は分析前提と
異なる。**推奨は執行時に必ず陳腐化しているのに、推奨という形式は心理的コミットを
生む。** 正しい出力形式は**条件付き政策**。

### 1.5 判断と執行の時間分離（政策台帳）

ユーザーが質問する瞬間は、市場が動いて感情が動いた瞬間と強く相関する
（下落時の「売るべき？」）。従来のシステムは**判断の質が最も劣化する時刻に
判断を生成する構造**を持っていた。

政策台帳は冷静な時点で「状態→応答」を確定しておき、事態成立時は政策を機械的に
参照する。出力の型が「行為の推奨」から「政策の参照」に変わる。
**新しく判断しないことが正当な出力になる。**

### 1.6 自己参照汚染の遮断（系譜会計）

レポートが Neo4j に入り、次のレポートが過去レポートを解釈に使い、その解釈がまた
保存される。時間とともにシステムの「知識」に占める自己生成物の比率が単調増加する。
**記憶が優秀であるほど、運用が長いほど悪化する。**

`src/core/provenance.py` が四系譜（一次観測 / 外部言説 / ユーザー言明 / 自己推論）を
機械判定し、深度閾値を超えたら再接地を要求する。

---

## 2. エントリポイント

### 2.1 ドメイン判定（10ドメイン）

`.claude/rules/intent-routing.md` が定義。

| ドメイン | 意図 | 代表表現 | 行き先 |
|:---|:---|:---|:---|
| **発見** | 新しい銘柄を探す | 探す、割安株、高配当株 | `/screen-stocks` |
| **分析** | 特定対象を知る | 〇〇ってどう、調べて | `build_briefing_pack.py --symbol` |
| **保有管理** | 持ち株の操作・確認 | PF、買った、ヘルスチェック | `/stock-portfolio` |
| **リスク** | 将来の評価 | 暴落、ストレス、怖い | `/stress-test` |
| **監視** | 気になる銘柄の記録 | ウォッチ、監視 | `/watchlist` |
| **記録** | 投資判断のメモ | メモ、テーゼ、学び | `/investment-note` |
| **知識** | 過去の検索 | 前回、履歴、常連銘柄 | `/graph-query` |
| **プランモード** | 計画してから実行 | プランモードで | `/plan-execute` |
| **政策** | 事前方針の参照・決定 | 撤退条件、どうするんだっけ | `scripts/manage_policy.py` |
| **メタ** | システム自体 | 何ができる、改善点 | 自己分析 |

> ⚠️ **急変時の最優先ルール**: 下落・急変を含む売買相談は、
> **分析より先に政策ドメインを引く**。

### 2.2 個別銘柄は「常に全力」

個別銘柄について聞かれたら、軽い聞き方（「これどう？」）でも**毎回パックを作って
深掘りする**。数字を並べるだけの薄い回答はしない。

```bash
python scripts/build_briefing_pack.py --symbol <SYMBOL>
# → output/briefing/<SYMBOL>_YYYYMMDD.json
# → .claude/prompts/stock_deep.md の執筆仕様で synthesis
```

例外（パック不要）: 単なる株価照会、売買記録、ウォッチ追加のみ、過去データ照会。

### 2.3 自動コンテキスト注入

スキル実行前に `scripts/get_context.py` が走り、Neo4j + TEI から過去の経緯を取得する。

| TEI | Neo4j | 動作 |
|:---|:---|:---|
| OK | OK | **毎回ベクトル検索** + シンボルベース検索 |
| NG | OK | シンボルベース検索のみ |
| — | NG | intent-routing のみで判断 |

出力1行目に鮮度ラベルが付き、LLM の行動を制御する。

| ラベル | 基準 | 行動 |
|:---|:---|:---|
| ⛔ FRESH | 24h以内 | スキル実行不要。コンテキストのみで回答 |
| ⚡ RECENT | 168h以内 | 差分モードで軽量更新 |
| 🔄 STALE | 168h超 | フル再取得 |
| 🆕 NONE | データなし | ゼロから実行 |

---

## 3. スキル層（9スキル）

`.claude/skills/<name>/SKILL.md` + `scripts/*.py`。

| スキル | 実行スクリプト | 役割 |
|:---|:---|:---|
| `screen-stocks` | `run_screen.py` | EquityQuery で60+地域から割安/成長/逆張り等をスクリーニング |
| `stock-report` | `generate_report.py` | 個別株・ETFの財務分析。バリュエーション・還元率・バリュートラップ判定 |
| `stock-portfolio` | `run_portfolio.py` | 保有管理。14サブコマンド |
| `stress-test` | `run_stress_test.py` | ショック感応度・シナリオ分析・因果連鎖・VaR |
| `market-research` | `run_research.py` | Grok API で銘柄/業界/市況/ビジネスモデルを深掘り |
| `watchlist` | `manage_watchlist.py` | ウォッチリスト CRUD |
| `investment-note` | `manage_note.py` | テーゼ・懸念・学び・日記の記録 |
| `graph-query` | `run_query.py` | ナレッジグラフへの自然言語クエリ |
| `plan-execute` | （スクリプトなし） | Orchestrator がワークフロー設計・実行・レビューを統括 |

### 3.1 stock-portfolio のサブコマンド（14）

`.claude/skills/stock-portfolio/scripts/portfolio_commands/` に1ファイル1コマンドで分割。

| コマンド | ファイル | 内容 |
|:---|:---|:---|
| `snapshot` | `snapshot.py` | 現況・損益 |
| `buy` / `sell` | `buy_sell.py` | 売買記録（`--yes` なしは確認プレビュー） |
| `list` | `list_cmd.py` | 一覧表示 |
| `analyze` | `analyze.py` | 構造分析（セクター/地域/通貨/規模の4軸HHI） |
| `health` | `health.py` | 健全性診断 + Fable5構造診断 |
| `forecast` | `forecast.py` | 期待リターン推定 |
| `rebalance` | `rebalance.py` | 配分調整案（3戦略） |
| `simulate` | `simulate.py` | 複利シミュレーション |
| `what-if` | `what_if.py` | 追加/売却/スワップの影響試算 |
| `review` | `review.py` | 売買成績・勝率 |
| `adjust` | `adjust.py` | 具体的な調整プラン（処方箋） |
| `reconcile` | `reconcile.py` | **三点照合**（模型/実在/意図） |
| `backtest` | `backtest.py` | 過去スクリーニングの成績検証 |

---

## 4. Core 層（`src/core/`）

ドメインロジック。100+モジュール。サブフォルダで責務を分ける。

### 4.1 screening/ — 銘柄探索

| モジュール | 行 | 役割 |
|:---|---:|:---|
| `screener_registry.py` | 438 | Strategy/Factory パターンでスクリーナを振り分け |
| `query_builder.py` | 344 | criteria dict → `yfinance.EquityQuery` に変換 |
| `query_screener.py` | 288 | EquityQuery ベースの本体（銘柄リスト不要） |
| `marginal.py` | 340 | **限界寄与スクリーニング**（目的関数を銘柄→PFへ反転） |
| `marginal_bridge.py` | — | 限界寄与の配線 |
| `indicators.py` | 354 | 財務指標・バリュースコア |
| `technicals.py` | 365 | 押し目判定用テクニカル |
| `alpha.py` / `alpha_screener.py` | — | 変化スコア（バリュー+変化の質+押し目） |
| `contrarian.py` / `contrarian_screener.py` | 309 | 売られすぎ+ファンダ健全 |
| `momentum.py` / `momentum_screener.py` | — | 急騰・ブレイクアウト |
| `growth_screener.py` | — | growth / high-growth / small-cap-growth |
| `pullback_screener.py` | — | 上昇トレンド中の押し目 |
| `trending_screener.py` | — | X(Twitter)話題銘柄 + ファンダ補強 |
| `value_screener.py` | — | 旧・銘柄リストベース（後方互換） |
| `filters.py` | — | フィルタ関数群 |

**限界寄与スクリーニング（提案2）の核心**:
```
限界スコア = 単独スコア × 補完係数
```
既存保有と値動きが同一な銘柄（**因子双子**、相関 > 0.85）は補完係数が 0.45 以下に
強制され、実質的な買い増しが「分散」として推奨されるのを防ぐ。

### 4.2 portfolio/ — 保有管理

| モジュール | 行 | 役割 |
|:---|---:|:---|
| `reconciliation.py` | 636 | **三点照合**。模型/実在/意図の突合 |
| `adjustment_advisor.py` | 688 | ルールベースの調整アクション生成 |
| `portfolio_simulation.py` | 640 | What-If（追加・売却・スワップ） |
| `tax.py` | 602 | **手取り翻訳層**。乗り換え損益分岐・NISA・損出し |
| `rebalancer.py` | 590 | リバランス（defensive/balanced/aggressive） |
| `runway.py` | 506 | **現金・入金・注意予算**の一級市民化 |
| `execution_audit.py` | 487 | **執行監査・決定生存率** |
| `report_diff.py` | 467 | **情報量比例レポート**の差分エンジン |
| `falsification.py` | 423 | **反証条件の点検**（信念の変化） |
| `weekly.py` | 404 | 週次レポートのデータ組み立て |
| `portfolio_query.py` | 414 | スナップショット・構造分析 |
| `portfolio_io.py` | 350 | CSV 読み書き |
| `portfolio_manager.py` | — | 管理コアロジック |
| `projection.py` | — | 短期/中期/長期の推移予測 |
| `simulator.py` | 294 | 複利シミュレーション |
| `vol_calibration.py` | — | **予測前提ボラの較正**（250日窓 vs 前提） |
| `small_cap.py` | — | 小型株分類・アロケーション判定 |
| `concentration.py` | — | 集中度（HHI） |
| `market_regime.py` | — | 市況レジーム判定 |
| `backtest.py` | — | 過去スクリーニングの検証 |
| `fx_utils.py` | — | 為替換算 |
| `portfolio_bridge.py` | — | ストレステストへの受け渡し |

**三点照合（提案1）が解く問題**:

| 層 | 出所 | 意味 |
|:---|:---|:---|
| **模型** | `config/weekly_holdings.yaml` | システムが信じている保有 |
| **実在** | 証券口座（楽天CSV / moomoo） | 残高の唯一真実 |
| **意図** | thesis + 政策 | なぜ持っているか |

検出するもの:
- **幽霊ポジション** — 模型にあるが口座に無い（存在しない資産のリスクを計算している）
- **未記録ポジション** — 口座にあるが模型に無い
- **孤児ポジション** — 保有しているが thesis も政策も無い（**なぜ持っているか未記述**）
- **循環照合** — `weekly_holdings.yaml` は楽天CSVの出力かつ週次の入力なので、
  素朴に突合すると必ず「一致」する。**新CSVは取り込む前に reconcile する**と
  独立検証になる（`circular` フラグで区別）

**手取り翻訳層（提案3）の核心**:
```
乗り換え損益分岐率 = f(含み益, 税率20.315%)
```
含み益がある売却は 20.315% のハンデを背負う。`evaluate_switch()` は
**却下しかしない**（「節税になるから売る」は書かない）。

### 4.3 risk/ — リスク評価

| モジュール | 行 | 役割 |
|:---|---:|:---|
| `forward_events.py` | 885 | **前方イベント脊椎**。翌週の確定イベント |
| `shock_sensitivity.py` | 480 | 4層フレームのショック感応度 |
| `liquidity.py` | 461 | **流動性制約**。閉じ込め資本・暴落時の買い余力 |
| `etf_lookthrough.py` | 409 | **ETFルックスルー**。中身への実質エクスポージャー |
| `correlation.py` | 399 | 相関分析・VaR |
| `model_audit.py` | 390 | **模型監査**。リスク模型を採点される仮説として扱う |
| `scenario_analysis.py` | 376 | シナリオ因果連鎖 |
| `assumptions.py` | 365 | **前提空間ダイバーシフィケーション**（前提HHI） |
| `recommender.py` | 320 | ルールベース推奨 |
| `scenario_definitions.py` | 299 | 8シナリオ定義 |

**前方イベント脊椎（提案4）が変えたこと**:

> 「週次レポートとは、その週に何が起きたかの報告である」は誤前提。
> スタンバイ運用における正しい機能は、**次の一週間への事前準備**である。
> 過去は既に価格に織り込まれており、行動可能性はゼロである。

計算するもの:
- **イベント集中度** — 翌週に決算を通過する保有の評価額比率
- **政策カバレッジの穴** — 決算を迎えるのに撤退条件が未定義の銘柄
- **配当落ちの分離** — 権利落ちの下落は損失ではない
- **`symbol_schedule_status()`** — 4状態を明示し、空リストを取得失敗と誤読させない

| status | 意味 |
|:---|:---|
| `scheduled` | 翌週に確定イベントがある |
| `none_upcoming` | 取得成功。翌週は無い（次回日付があれば添える） |
| `no_earnings` | ETF/投信。決算という概念が無い → 中身に読み替える |
| `unavailable` | **本当に取得できなかった** |

**ETFルックスルー**: レバレッジETFはスワップで複製するため開示保有が実体を反映
しない（実測で `TQQQ.top_holdings` は `IQMM` 単独）。1x proxy 経由で中身を見る。

```
実質% = PF比率 × 内部ウェイト × レバレッジ
```

実測（2026-08-02）: NVDA **16.1%**（直接保有ゼロ）、MU 11.6%、AMD 10.6%。
**このPFは「ETF 4本」ではなく実質的に半導体個別株PFだった。**

**前提空間ダイバーシフィケーション（案2）**: セクター・地域・通貨・規模のHHIは
**資産空間**の集中度にすぎない。thesis に書かれた「なぜ持つか」が複数銘柄で同一前提
（円安継続・AI設備投資）に依存していても、どの指標にも映らない。
**価格相関は前提相関の遅行指標である。**

### 4.4 research/ — 深掘り

| モジュール | 行 | 役割 |
|:---|---:|:---|
| `briefing_pack.py` | 888 | **パック生成**。全材料を1つに束ねる |
| `moomoo_insights.py` | 661 | moomoo(OpenD) から13カテゴリ収集 |
| `narrative.py` | 550 | **物語量スナップショット**（テーゼの希少性） |
| `researcher.py` | 421 | Grok API オーケストレーション |
| `portfolio_news.py` | 296 | 保有・指数のニュース監視 |
| `outcome_tracker.py` | 291 | **過去判断の答え合わせ**（学習ループ） |
| `competitors.py` | — | 競合・ベルウェザーの値動き |

**物語量（提案7）**: 「上がっているが、もう持っている理由がない」を判定する唯一の
材料。購入時比で語られる量が何倍になったかを測る。GDELT → Finnhub → yfinance の
多源フォールバックで、**同一ソースの系列同士でのみ比較する**（混ぜると無意味）。

### 4.5 health/ — 健全性診断

`health_check.py` から KIK-576 で分割。旧パスは shim で後方互換。

| モジュール | 役割 |
|:---|:---|
| `runner.py` | オーケストレータ |
| `trend.py` | SMA50/200・RSI からトレンド判定、ゴールデン/デッドクロス検出 |
| `alert.py` | 3段階（早期警告/注意/撤退）の算出 |
| `quality.py` | 変化の質 |
| `labels.py` | ラベル・判定文の生成 |
| `etf.py` | ETF固有（経費率・AUM） |
| `community.py` | コミュニティ集中度 |
| `theme.py` / `theme_balance.py` | テーマ露出・バランス |

### 4.6 policy/ — 政策台帳（Fable5 案A）

| モジュール | 役割 |
|:---|:---|
| `ledger.py` | CRUD + **実行可能性審査**（測定可能性・矛盾検出） |
| `evaluator.py` | 現在市況と全政策のトリガー距離 |
| `deviation.py` | カバレッジと**逸脱監査** |

**意思決定状態**が3種から6種に拡張される:

| intent | 意味 |
|:---|:---|
| `conditional_commit` | 条件付きコミット |
| `awaiting_trigger` | トリガー待機（監視のみ） |
| `deliberate_inaction` | **意図的不作為**（「動かない」を選ぶ宣言） |

**副作用と抑制策**:

| 副作用 | 抑制策 |
|:---|:---|
| 政策の硬直化 | 失効期限の必須化 |
| ストレス下の政策破棄 | トリガー接近・成立時**のみ**24時間の冷却期間 |
| 曖昧な政策の氾濫 | 登録時の測定可能性審査 |
| 「政策があるから考えない」 | 期限ごとの再審査で思考を再起動 |

### 4.7 decision/ — 判断パッケージ（Fable5 案B）

| モジュール | 役割 |
|:---|:---|
| `package.py` | 可知集合の**凍結**・封印・永続化 |
| `review.py` | **過程再審**と結果ログ（運と技能の分離） |

売買記録時に自動生成される。**ここで凍結しなかった判断は永久に再審不能**になる
（後から作れば必ず後知恵で汚染される）。

### 4.8 ports/ — 依存性逆転

`graph.py` / `market_data.py` / `research.py` / `storage.py`。
Protocol インタフェースで Core が Data の具象に依存しないようにする（KIK-513/516）。

### 4.9 トップレベル

| モジュール | 行 | 役割 |
|:---|---:|:---|
| `exposure.py` | 532 | **因子エクスポージャー**（5因子×3窓のOLS） |
| `return_estimate.py` | 489 | 期待リターン3シナリオ |
| `proactive_engine.py` | 436 | 蓄積知識からの次アクション提案 |
| `provenance.py` | 421 | **認識の系譜会計** |
| `technicals.py` | 361 | テクニカル指標・過熱判定 |
| `market_dashboard.py` | 310 | VIX/F&G/金利/イールドカーブ |
| `lesson_gate.py` | — | 記憶を条件付き債務として扱う検証ゲート |
| `temporal.py` | — | 時間規律と封印ハッシュ |
| `value_trap.py` | — | バリュートラップ判定 |
| `ticker_utils.py` | — | ティッカーから通貨・国を推定 |
| `action_item_detector.py` / `_bridge.py` | — | アクション検出 → Linear 連携 |
| `_thresholds.py` | — | 閾値の一元ロード |

**因子エクスポージャー（提案2）**:
```python
FACTORS = ("market", "usdjpy", "rates", "oil", "semis")
ESTIMATION_WINDOWS = (60, 120, 250)
```
アジア市場は米国因子に対して**1日ラグ**を入れる（入れないと日本株の感応度が
ほぼゼロに見える）。因子の符号が窓間で反転したら**その因子だけ**を unstable にする
（全部を unstable にすると補完係数が1.0に潰れる）。

**通貨の二重ロング検出**: 米国株と日本の輸出企業を同時に持つと
「地域は分散、為替は集中」になる。**円高シナリオでPFの大半が同時に不利になる**のに、
セクター・地域HHIはこれを全く検出しない。

---

## 5. Data 層（`src/data/`）

### 5.1 yahoo_client/ — 唯一の価格・財務の入口

> **規約: データ取得は必ず `yahoo_client` 経由。直接 yfinance を呼ばない。**

| モジュール | 役割 |
|:---|:---|
| `detail.py` | `get_stock_info()` / `get_stock_detail()`（3層キャッシュ） |
| `history.py` | 価格履歴・ニュース |
| `screen.py` | `yf.screen()` ベースのスクリーニング |
| `events.py` | **決算日・配当権利日** |
| `financials.py` | **損益計算書からの成長率導出**（比率欠損の補完） |
| `macro.py` | マクロ指標 |
| `_cache.py` / `_memory_cache.py` | ファイル + スレッドセーフLRU（TTL付き） |
| `_normalize.py` | 正規化・異常値サニタイズ |

**キャッシュ3層**: メモリLRU → ファイル(TTL 24h) → API。

**成長率導出が要る理由**: 日本株は `earningsGrowth` が欠けることが多い。
2026-08-01 のレポートは味の素について「増収が利益に落ちているか不明」と留保したが、
`income_stmt` には純利益 70.3B → 134.7B（**+91.6%**）と明記されていた。
**比率フィールドが空だっただけで、原資料は取れていた。**

さらに yfinance の比率は**四半期YoY**なので、前年同期が落ち込んでいると
+1000% 級が普通に出る（2737.T は `earningsGrowth = 10.433`）。
±300% を超えたら年度基準（`growth_annual`）を併記する。

### 5.2 brokers/ — 実在残高の抽象層

| モジュール | 役割 |
|:---|:---|
| `base.py` | 正規化スナップショット形。**`available=False` は残高UNKNOWN であって0ではない** |
| `rakuten_csv_broker.py` | 楽天CSV由来。**循環検出**（模型の生成元と同一ファイルなら `circular=True`） |
| `moomoo_broker.py` | moomoo(OpenD) 由来。`SCOPE = ["US"]` |

**既定の残高ソースは楽天CSVのみ。** moomoo は資金ゼロで US 市場の実口座権限も
無いため、毎週叩いても必ず `available=False` になる。
`MOOMOO_AS_BALANCE_SOURCE=on` で復帰可能。

### 5.3 graph_store/ — Neo4j 書き込み（dual-write）

CSV/JSON が master、Neo4j は**検索・関連付け用の view**。

| モジュール | ノード |
|:---|:---|
| `stock.py` | Stock / Screen / Report |
| `portfolio.py` | Portfolio / Trade / HealthCheck / Forecast / StressTest |
| `research.py` | Research / News / Sentiment / Catalyst / AnalystView |
| `market.py` | MarketContext / Indicator / UpcomingEvent |
| `note.py` | Note / ActionItem |
| `linker.py` | AI駆動のリンク生成 |

`NEO4J_MODE` で書き込み深度を制御: `off` / `summary` / `full`。

### 5.4 graph_query/ — Neo4j 読み出し

`nl_query.py`（419行）が自然言語 → クエリのディスパッチャ。
`community_detect.py`（486行）が共起分析による**コミュニティ検出**。

### 5.5 context/ — 自動コンテキスト注入

| モジュール | 役割 |
|:---|:---|
| `auto_context.py` | シンボル検出 + グラフ状態判定 + スキル推奨 |
| `vector_search.py` | TEI ベクトル検索とハイブリッド統合 |
| `freshness.py` | 鮮度ラベル（FRESH/RECENT/STALE/NONE） |
| `constraint_extractor.py` | **lesson から制約条件を抽出**（plan-check の入力） |
| `grok_context.py` | Grok プロンプトへの文脈注入（最大300トークン） |
| `screening_context.py` | GraphRAG のスクリーニング文脈 |
| `screen_annotator.py` | 売却・メモ文脈で結果を注釈 |
| `summary_builder.py` | ベクトル検索用の意味的サマリ |

### 5.6 その他のデータ源

| モジュール | 役割 | 有効化 |
|:---|:---|:---|
| `grok_client/` | X検索・センチメント・業界・市況・ビジネスモデル | `XAI_API_KEY` |
| `finnhub_client.py` | 米国株ニュース | `FINNHUB_API_KEY` |
| `moomoo_client.py` | OpenD ライフサイクル（自動起動→取得→終了） | `MOOMOO_ENABLED=on` + `futu-api` |
| `embedding_client.py` | TEI ベクトル | `TEI_URL` |
| `linear_client.py` | issue 連携 | `LINEAR_ENABLED=on` |
| `rakuten_csv.py` | 保有商品一覧CSV | — |
| `rakuten_trades.py` | **取引履歴CSV**（決定生存率の入力） | — |
| `rakuten_rss.py` | MS2 RSS スナップショット | — |
| `note_manager.py` | 投資メモ（JSON + Neo4j の dual-write） | — |
| `question_log.py` | **質問ストリームの第一級データ化** | — |
| `history/` | screen/report/trade/health/research の JSON 永続化 | — |
| `lesson_conflict.py` / `lesson_community.py` | lesson の矛盾検出・分類 | — |
| `user_profile.py` | ユーザー設定 | — |

---

## 6. Output 層（`src/output/`）

整形は**専用フォーマッタに分離**されている（1機能1ファイル）。

| フォーマッタ | 対象 |
|:---|:---|
| `formatter.py` | スクリーニング結果（統一レンダラ） |
| `portfolio_formatter.py` + 分割6本 | PF（analyze / forecast / health / rebalance / simulate / review） |
| `stress_formatter.py` | ストレステスト |
| `research_formatter.py` | 深掘りリサーチ |
| `weekly_formatter.py` | 週次レポート |
| `reconcile_formatter.py` | 三点照合 |
| `forward_formatter.py` | 前方イベント |
| `constraint_formatter.py` | 制約（税・現金・入金・注意） |
| `liquidity_formatter.py` | 流動性 |
| `marginal_formatter.py` | 限界寄与 |
| `weekly_diff_formatter.py` | 情報量比例 |
| `adjust_formatter.py` | 調整プラン |
| `fable5_formatter.py` | Fable5 各案の health 配線 |
| `screening_summary_formatter.py` | GraphRAG 文脈 |

### 6.1 保存＝検証の統一入口

```
save_and_sync(content, filename)
  1. output/ へ保存
  2. Obsidian vault へコピー（非破壊。同名は _v2, _v3）
  3. verify_report() で実在＋体裁を検証
```

**「完了＝実物が届き検証済み」を保証する。** 検証NGなら exit code 1。

`resync_missing()` が毎回の実行時に**過去分の実在**も確認する。
2026-08-01 のレポートは同期ログ上「成功」だったのに翌日には vault から消えて
いたため、一点検証では足りないと判明した。

vault パス（唯一の正解）:
```
C:\Users\swend\iCloudDrive\swender\投資記録\
```

---

## 7. 土曜週次レポートのパイプライン

### 7.1 固定骨格（順序を変えない）

```
0. 判定        — 情報量（静穏週かどうか）※最後に書いて先頭に置く
1. 照合        — 私の模型は現実と一致しているか
                  ※ここが通らなければ以降は条件付きで書く
2. 信念の変化  — 反証条件に触れたテーゼはあるか
                  ※価格ではなく信念の変化を最初に見る
3. 前方イベント — 翌週に何が確定して起きるか
4. 制約        — 政策・税・現金・流動性・注意
                  ※行動可能な空間を先に確定させる
5. 機会        — 制約内での保有の立ち位置
                  ※3と4を通過した後にのみ提示
6. 事前決定    — 翌週の条件付き政策 ← 土曜の唯一の「行動」
7. 監査        — 執行・模型の健全性
8. 前提と限界  — 系譜サマリ
```

**機会（買い候補）を先に出すのは、自宅が燃えているかを確認する前に
買い物に行くのと同じ。**

### 7.2 実行フロー

```
scripts/run_weekly_deep.bat
  ├ MOOMOO_ENABLED=on / PYTHONUTF8=1 を設定
  └ scripts/weekly_deep_driver.py
       ├ build_briefing_pack()          層1: 材料集め（約416秒）
       ├ build_sections()               固定骨格 + 保有ごとの節
       ├ slice_pack(section)            節が要る材料だけ渡す
       │    ※照合の状態は全節に渡す（未照合のまま確定値を語らせない）
       ├ claude -p（節ごと）            層2: synthesis
       │    └ 書けた節 → ファイル + state.json に記録
       ├ assemble()                     節を結合
       └ save_and_sync() + resync_missing()
```

**終了コード**: `0`=完了 / `2`=中断（次の起動が続きから再開） / `1`=異常。

### 7.3 スケジュール

| タスク | 頻度 | 状態 |
|:---|:---|:---|
| `StockSkills\WeeklyDeep` | 毎週土曜 07:12 | Ready |
| `StockSkills\WeeklyDeepResume` | 3時間ごと（`--resume-only`） | Ready |
| `StockSkills\WeeklyReport` | 旧・薄い版 | Disabled |

⚠️ **タスク作成時の必須設定**（既定値が全部不利で、7/25 と 8/1 の2回発火しなかった）

| 設定 | 必須値 | これが違うと |
|:---|:---|:---|
| `WakeToRun` | True | スリープ中なら起こされない |
| `StartWhenAvailable` | **True** | 逃した回を**Windowsが破棄**し、次週へ飛ぶ |
| `DisallowStartIfOnBatteries` | False | バッテリー駆動で起動しない |
| `StopIfGoingOnBatteries` | False | 実行中に電源が抜けると止まる |

**`StartWhenAvailable` が決定打。**

### 7.4 分量制御

| `information.quiet` | 書き方 |
|:---|:---|
| **真（静穏週）** | セクション0で判定、1〜4は各1〜2行、5〜8は折り畳み。**全体30行以内**。「今週は何もしないことが正しい選択です」と明言 |
| **偽（要対応週）** | 通常密度。銘柄別150〜250字。変化のない項目は折り畳む |

**静穏週に短く終えるのは失敗ではなく正しい出力。**

---

## 8. 設定ファイル（`config/`）

| ファイル | 内容 |
|:---|:---|
| `screening_presets.yaml` | 16 preset の閾値 |
| `exchanges.yaml` | 11地域 + region_groups（asean/all） |
| `themes.yaml` | 9テーマ（ai / ev / cloud-saas / cybersecurity / biotech / renewable-energy / defense / fintech / healthcare） |
| `markets.yaml` | 市場定義 |
| `competitors.yaml` | 競合・ベルウェザーのマッピング |
| `etf_lookthrough.yaml` | レバETF→1x proxy、投信→近似ETF、**テクニカル代理指数** |
| `tax.yaml` | 税率・NISA枠・手数料（`meta.verified_as_of` 未設定なら警告） |
| `cashflow.yaml` | 入金・現金の目的・週次レビュー時間 |
| `weekly_holdings.yaml` | 保有構成（楽天CSVから取り込み） |
| `rakuten.yaml` | MS2 RSS スナップショットのパス |
| `output.yaml` | vault パス・衝突時の扱い |
| `thresholds.yaml` | 各種閾値の一元管理 |
| `module_annotations.yaml` | モジュール→KIK issue のマッピング |
| `user_profile.yaml.example` | ユーザー設定のひな形 |

---

## 9. ルール（`.claude/rules/`）

| ファイル | 内容 |
|:---|:---|
| `intent-routing.md` | **自然言語インターフェース**。10ドメイン判定と全ルーティング |
| `graph-context.md` | Neo4j スキーマ + 自動コンテキスト注入 + 前提知識統合原則 |
| `weekly-report.md` | 週次レポートの2層構成・固定骨格・スケジュール要件 |
| `policy-ledger.md` | 判断と執行の時間分離 |
| `plan-check.md` | 投資判断マルチエージェント（Plan→Execute→Review） |
| `provenance.md` | 系譜会計と前提空間 |
| `portfolio.md` | PF・ストレステストの開発ルール |
| `screening.md` | スクリーニングのルール |
| `development.md` | 依存・コーディング規約・環境変数 |
| `workflow.md` | Worktree→設計→実装→テスト→レビュー→結合試験→マージ |
| `testing.md` | テスト構造・モック方法 |

### 9.1 Plan-Check の Review フェーズ（4チェッカー）

| チェッカー | 判断原則 |
|:---|:---|
| **Constraint Checker** | 各 constraint の expected_action が実行されたか |
| **Quality Checker** | 「ユーザーがこの出力を見て、迷わず行動できるか？」 |
| **Tax Checker** | 「この提案は**税引後**でも改善するか？」 |
| **Risk Checker** | 「実行して想定外の損失が発生しないか？」 |

差し戻しは最大2回。3回目は WARN 付きで出力（無限ループ防止）。

---

## 10. サブエージェント（`.claude/agents/`）

| エージェント | model | 役割 |
|:---|:---|:---|
| `screener` | haiku | 機械的なスクリーニング・データ収集。判断はしない |
| `analyst` | sonnet | 数値と過去コンテキストの解釈・レポート整形 |
| `judge` | inherit（親=最上位） | 投資判断の最終決定 + Plan-Check Review 相当の検証 |

⚠️ **コンテキスト衛生**: サブエージェントは独立コンテキストで起動し、
`CLAUDE.md` を自動では読み込まない。破壊的操作や保有前提が絡む指示を渡すときは
必要な制約を明記する。

---

## 11. データストア（`data/`）

| ディレクトリ | 内容 | gitignore |
|:---|:---|:---|
| `cache/` | 銘柄ごとJSONキャッシュ（TTL 24h）+ ETF構成 + **マクロイベント退避** | ✓ |
| `history/` | screen / report / trade / health / research / stress_test / market_context | 一部 |
| `notes/` | 投資メモ | ✓ |
| `policies/` | 政策台帳 | ✓ |
| `decisions/` | 判断パッケージ | ✓ |
| `narrative/` | 物語量スナップショット | — |
| `weekly_snapshots/` | 週次差分の基準点 | — |
| `model_scorecard/` | 模型監査の採点 | — |
| `watchlists/` | ウォッチリスト | ✓ |
| `screening_results/` | スクリーニング結果 | ✓ |
| `models/` | TEI 埋め込みモデル | ✓ |

---

## 12. 外部依存と縮退（graceful degradation）

**全ての外部依存は独立に try/except で守られ、落ちても本体は必ず返る。**

| 依存 | 有効化 | 落ちたときの挙動 |
|:---|:---|:---|
| yfinance | — | 銘柄ごとに `error` を入れて続行 |
| Neo4j | 接続可能なら自動（`NEO4J_MODE`） | 「コンテキストなし」→ intent-routing のみ |
| TEI | `TEI_URL`（既定 :8081） | ベクトル検索スキップ → シンボル検索のみ |
| Grok | `XAI_API_KEY` | `EMPTY_*` を返す |
| Finnhub | `FINNHUB_API_KEY` | ニュース表示なし |
| moomoo | `MOOMOO_ENABLED=on` + **`futu-api`** | **マクロは退避キャッシュで代替** |
| Linear | `LINEAR_ENABLED=on` | issue 作成をスキップ |
| Obsidian vault | `config/output.yaml` | `output/` のみで完了 |
| 楽天CSV | — | 「取得できなかった」と報告（**0件とは言わない**） |

### 12.1 moomoo の位置づけ（重要）

**moomoo はニュース・マクロ材料専用。** 残高照合には使わない。

取得できるもの（US LV3 実測・13カテゴリ）:
`fed_watch` / `economic_events` / `earnings` / `dividends` / `rating_changes` /
`ark` / `heat_map` / `rise_fall` / `pre_market` / `after_hours` / `overnight` /
`news` / `per_stock`

🔒 取得できないもの: **日本株は全権限なし**（yahoo に任せる）、米指数・米オプション。

**`futu-api` が未導入だと、設定が全部正しくても13カテゴリ全部が静かに落ちる。**
2026-08-01 の「moomoo無効」警告の実際の原因はこれ一点だった。

### 12.2 マクロイベントの退避

`_macro_events()` が moomoo 単一依存だったため、moomoo が一時的に落ちた週は
**FOMC も経済指標も黙ってカレンダーから消えていた**。

取得できた週に `data/cache/macro_events.json` へ退避し、落ちた週はそれを使う。
使ったことは `source: "moomoo(cached)"` と `cached_age_hours` で示す
（**鮮度を伏せたまま最新であるかのように見せない**）。

---

## 13. 自動化レイヤー

| レイヤー | 内容 |
|:---|:---|
| **PostToolUse hook** | `.py` 編集時に `pytest` + ドキュメント陳腐化チェック |
| **PreToolUse hook** | `scripts/hooks/guard_protected.py` が保護領域への破壊的コマンドをブロック |
| **pre-commit hook** | src/ 変更時に `generate_docs.py all` を自動実行 |
| **GitHub Actions** | `.github/workflows/tests.yml` で push/PR 時に pytest |
| **タスクスケジューラ** | 土曜 07:12 の無人実行 + 3時間ごとの再開 |

### 13.1 ドキュメント自動生成

`scripts/generate_docs.py` がソースから再生成する。

| ターゲット | 対象 | 内容 |
|:---|:---|:---|
| `api-reference` | `docs/api-reference.md` | AST 解析で public 関数・クラスを抽出 |
| `architecture` | `CLAUDE.md` | マーカー間のレイヤー概要 |
| `test-count` | `development.md` | `pytest --co -q` でテスト数 |
| `skill-catalog` | `docs/skill-catalog.md` | SKILL.md frontmatter から |
| `data-models-verify` | `docs/data-models.md` | fixture との整合性検証 |

**手動更新が必要**: `intent-routing.md`、各 `SKILL.md`、`rules/*.md`、`README.md`。

---

## 14. テスト（4,584件 / 183ファイル）

| ディレクトリ | 件数 | 対象 |
|:---|---:|:---|
| `tests/core/` | 92ファイル | ドメインロジック |
| `tests/data/` | 51ファイル | 取得・永続化 |
| `tests/output/` | 22ファイル | 整形 |
| `tests/integration/` | — | 結合 |
| `tests/skills/` | — | スキルスクリプト |
| `tests/hooks/` | — | フック |
| `tests/scripts/` | — | CLI |
| `tests/synthesis/` | 36件 | **Claude が書く文章**の評価軸（改善1）。API は叩かない |
| `tests/chaos/` | 23件 | **わざと壊して気づくか試す**（改善7）。既定でスキップ |

### 14.1 conftest の自動モック

`_block_external_io` が autouse で外部I/Oを全テストで遮断する。
`@pytest.mark.no_auto_mock` でオプトアウト可。

- Neo4j → `NEO4J_MODE=off` + driver を None
- TEI → `is_available()` を False
- Grok → `XAI_API_KEY` を削除
- Finnhub → `FINNHUB_API_KEY` を削除
- **moomoo → `MOOMOO_ENABLED=off`**（`.env` に on を入れた結果、テストが
  OpenD を起動して最大90秒待ちハングするようになったため）
- メモリキャッシュ → テスト間でクリア

### 14.2 テストが縛っている「設計の性質」

単なるカバレッジではなく、**破ると設計が死ぬ性質**を明示的に縛っている。

| テストファイル | 縛っている性質 |
|:---|:---|
| `test_schedule_status.py` | 取得成功×予定なしを「取得できなかった」と言わない |
| `test_weekly_gap_fixes.py` | 単一ソース依存で材料が消えない |
| `test_macro_cache.py` | moomoo が落ちた週に FOMC を消さない |
| `test_vol_calibration.py` | 窓の違う量を比較しない・置き換えでなく縮小推定 |
| `test_financials_growth.py` | 前期赤字で成長率を出さない・既存値を上書きしない |
| `test_falsification_suggestions.py` | 提案を登録済み条件として扱わない |
| `test_etf_lookthrough.py` | ETFの「決算が無い」と「取得失敗」を混同しない |
| `test_import_scripts_encoding.py` | cp932 環境で `¥` を出して落ちない |
| `test_policy_rate_gate.py` | 金利ゲートが**売却側を止めない**・取得失敗で止めない |
| `test_conflicting_assumptions.py` | 前提の衝突を検出し、**同方向の二重ロングは誤検出しない** |
| `test_critic_calibration.py` | 未検証を「外れ」と数えない・少数の的中で実力を判定しない |

### 14.3 synthesis 層の評価軸（改善1）

`tests/synthesis/assertions.py` が **§16 の8原則を文章のレベルで**縛る。
Python 層のテストが4,584件あるのに対し、synthesis 層は0件だった。

| 検査 | 原則 |
|:---|:---|
| `no_unavailable_as_zero` | §16-1 取得失敗を「0件」「問題なし」と書かない |
| `circular_disclosed` | §16-3 循環照合を「一致」と呼ばない |
| `no_buy_recommendation` | §16-5 条件節を伴わない買い推奨を出さない |
| `quiet_week_length` | §7.4 静穏週は30行以内 |
| `orphan_flagged` | §17.2 孤児ポジションを名指しする |
| `section_order` | §7.1 固定骨格（機会が照合・制約より前に出ない） |
| `growth_window_labeled` | §16-2 四半期YoYのスパイクに年度基準を併記 |
| `cache_age_disclosed` | §12.2 退避キャッシュの鮮度を伏せない |

`pass` / `fail` / **`skip`** の3値を返す。skip を pass と混ぜると通過率が嘘になる
——これは §16-1 を harness 自身に適用したもの。

実際の synthesis 出力の評価は `scripts/eval_synthesis.py`（API を叩く・週1回）。

### 14.4 カオステスト（改善7）

`scripts/run_chaos.py` で月1回。**わざと壊して、システムが気づくか試す。**

| 壊し方 | 期待される検出 |
|:---|:---|
| 模型を3ヶ月前の版にする | 幽霊ポジションとして検出 |
| 楽天CSVを模型の生成元と同一にする | `circular=true` +「独立検証ではない」 |
| 楽天CSVを0件にする | 「保有なし」ではなく「取得できなかった」 |
| moomoo を落とす | 退避キャッシュに切替 + `cached_age_hours` 明示 |
| 全項目 `available=false` | 「問題なし」ではなく「判定不能」 |
| 決算日を空リストにする | `no_earnings` / `unavailable` を区別 |
| 同期後に vault から消す | `resync_missing()` が翌回に検出 |

§16-8「単一の取得元に依存しない」は、8/2 に直した穴9件のうち6件が同じ形だった。
再発したらここで捕まる。**ただし攻撃は7種類しかなく、通過は「穴が無い証拠」ではない。**

---

## 15. 開発ワークフロー

```
1. Worktree 作成    git worktree add -b feature/kik-{NNN}-{desc} ~/stock-skills-kik{NNN} main
2. 設計             EnterPlanMode で調査 → 方針策定 → 承認
3. 実装             PostToolUse hook が自動でテスト実行
4. 単体テスト       pytest tests/ -q 全件PASS
5. コードレビュー   Teams（arch / logic / test の3レビュア並列）
6. 結合試験         Teams（screener / report / portfolio / stress の4テスタ並列）
7. ドキュメント更新 自動生成 + 手動チェックリスト
8. マージ           git merge --no-ff → push → worktree 削除
```

### ファイル構成ガイドライン

| 対象 | 上限 |
|:---|:---|
| プロダクションコード | 400行推奨 / 500行で分割検討 |
| テスト | 600行推奨 |
| スクリプト | 300行推奨 |

分割時は旧パスに `sys.modules` リダイレクトの shim を残し、`DeprecationWarning` を付ける。

---

## 16. この設計で確立した原則

**破ると設計が死ぬもの。**

1. **取得失敗を結果と混同しない。**
   「取れなかった」を「0件」「問題なし」「執行率0%」「流動性が高い」と書いた
   瞬間に嘘になる。実装中に7回踏んだ。

2. **窓の違う量を比較しない。**
   20日実測ボラと数年スパンの構造的前提は別の量。比較すると誤警報が出て、
   **その騒ぎの裏に本物の乖離が埋もれる。**

3. **循環照合を「一致」と呼ばない。**
   `weekly_holdings.yaml` は楽天CSVの出力かつ週次の入力。
   **新CSVは取り込む前に reconcile する**と独立検証になる。

4. **税は判断を却下する側にのみ使う。**
   「節税になるから売る」は書かない。売却提案には**入金代替案**を併記する。

5. **土曜は「何を買うか」を答える日ではない。**
   出力形式は条件付き政策。

6. **提案と登録を分ける。**
   機械がテーゼ本文から条件を断定して登録してはいけない。

7. **単位を揃える。**
   因子の週次変化に日次の `percent_change` を流用しない。

8. **単一の取得元に依存しない。**
   2026-08-02 に直した穴9件のうち**6件が「単一の取得元に依存していて、その
   取得元が実態と食い違っていた」**という同じ形だった。個別のバグではなく設計の癖。

---

## 17. 既知の限界

### 17.1 時間でしか埋まらないもの

| 項目 | 必要期間 | 現状 |
|:---|:---|:---|
| 累積差分（4週・13週） | 4週以上 | 蓄積中 |
| 模型監査の採点 | 26週 | 2週 |
| 物語混雑度の分析 | 基準点の蓄積 | 記録は稼働中 |

**これらの空欄を「異常なし」と読んではいけない。**

### 17.2 ユーザーの判断が要るもの

**2026-08-05 更新** — 下の3件は解消済み（`scripts/seed_policies.py`）。

| 項目 | 状態 |
|:---|:---|
| ~~孤児ポジション6件（評価額の79.1%）~~ | ✅ **0件**。政策15本を登録し、全保有に thesis か政策がある |
| ~~QCOM の撤退ライン $158 の反証条件登録~~ | ✅ thesis に `price <= 158` を登録。$172.12（リスクライン・政策）と**別レイヤー**で併存 |
| ~~`config/tax.yaml` の `meta.verified_as_of`~~ | ✅ 2026-08-05 に本人確認済み |
| `config/cashflow.yaml` の現金の目的・週次レビュー時間 | 未設定 |
| 楽天の取引履歴CSV（1回落とせば決定生存率が測れる） | 未取得 |
| `config/synthesis_models.yaml` の実測（改善2） | `measured_as_of` 空。スイープ未実行 |
| `data/critics/` の採点蓄積（改善5） | 台帳形式のみ。判定への使用は保留中 |

**QCOM の $158 と $172.12 は「同じ銘柄に2つの真実」ではなく、二層である**（ユーザー判断）:
$172.12 = リスクライン（弾再生順序の変更のみ・損切りはしない）、
$158 = 反証条件（テーゼが間違っていた証拠・売却の引き金ではない）。

**副作用**: 政策を登録したので `deviation.py`（逸脱監査）が鳴り始める。
最初の数週間はノイズが多いはずで、閾値の調整期間が要る。
「政策から逸脱した」の大半は、政策が現実に合っていないという意味でもある。

### 17.3 構造的な限界

- **祝日を考慮できない** — `prior_business_day()` は土日のみ。権利付最終日が
  祝日でずれる可能性は出力側で明示する
- **moomoo は日本株の権限が無い** — yahoo に任せる
- **投信は基準価額の時系列が取れない** — 連動指数で代理（為替分のずれが残る）
- **レバレッジETFの開示保有は実体を反映しない** — 1x proxy 経由でしか見られない
- **平時相関は暴落時に1へ収束する** — 「相関0.11だから分散効果あり」は平時の話
