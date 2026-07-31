---
paths:
  - "src/core/screening/**"
  - ".claude/skills/screen-stocks/**"
  - "config/screening_presets.yaml"
---

# スクリーニング開発ルール

## 限界寄与スクリーニング (土曜設計書 提案2)

**既定で、スクリーニング結果に「保有考慮後」の限界スコアが併記される。**

    限界スコア = 単独スコア × 補完係数

保有者にとっての正しい問いは「この銘柄は良いか」ではなく
**「私が既に持っているものに何を足すか」**である。90点の優良株が既存保有と
同じ因子を持つなら追加してもPFは改善せず、65点でも欠けた因子を埋めるなら価値が高い。

- `src/core/exposure.py` — 5因子（market / usdjpy / rates / oil / semis）を
  日次リターンの重回帰で自前構築。60/120/250日で並走させ**因子単位**で
  不安定フラグを立てる。日本株は米国因子に1日ラグを掛ける（時差）
- `src/core/screening/marginal.py` — 補完係数・限界スコア・ルックスルー
- `src/core/screening/marginal_bridge.py` — screen-stocks への配線
- `--standalone` で従来動作（単独スコアのみ）に戻せる。**非破壊**

### 歯止め（これを外すと恣意的な装置になる）

1. 単独スコアに下限（既定60点）。**分散のための分散で低品質銘柄を買わせない**
2. 因子推定が不安定な因子は寄与を割り引く（因子単位。全体を一律に潰さない）
3. R² が低い銘柄は全因子を割り引く（βの符号自体がノイズになるため）
4. 補完係数は 0.3〜1.6 に制限。因子推定の誤差が順位を支配しないようにする
5. 因子双子（相関0.85超）は係数を強制的に 0.45 以下に落とす

### 測れないことを「問題なし」と書かない

因子・保有・ETF構成のいずれかが取れない場合は `available=False` を返し、
「偏りが無い」「分散している」とは言わない。**測れていないと書く。**

> 新スクリーニングプリセット追加の具体的な手順（ファイル一覧・コードテンプレート・テスト例）は [docs/patterns.md](../../docs/patterns.md) の「パターン1」を参照。

## 5つのスクリーナーエンジン

- **QueryScreener（デフォルト）**: `build_query()` → `screen_stocks()` [EquityQuery bulk API] → `_normalize_quote()` → `calculate_value_score()` → ソート
- **ValueScreener（Legacy）**: 銘柄リスト方式。`get_stock_info()` → `apply_filters()` → `calculate_value_score()`。japan/us/asean のみ
- **PullbackScreener**: 3段パイプライン。EquityQuery → `detect_pullback_in_uptrend()` → value_score。"full"（完全一致）と"partial"（bounce_score>=30）の2種
- **AlphaScreener**: 4段パイプライン。EquityQuery(割安足切り) → `compute_change_score()` → 押し目判定 → 2軸スコアリング
- **MomentumScreener** (KIK-506): 2段パイプライン。EquityQuery → `detect_momentum_surge()` → surge_score ランキング。"stable"（継続上昇, 50MA +10-15%）と"surge"（急騰, 50MA +15%+）の2サブモード

## バリュースコア配分

PER(25) + PBR(25) + 配当利回り(20) + ROE(15) + 売上成長率(15) = 100点

## EquityQuery ルール

- フィールド名は yfinance 準拠（`trailingPE`, `priceToBook`, `dividendYield` 等）
- プリセットは `config/screening_presets.yaml` で定義。criteria の閾値を YAML で管理

## yahoo_client データ取得

- `get_stock_info(symbol)`: `ticker.info` のみ。キャッシュ `{symbol}.json` (24h TTL)
- `get_stock_detail(symbol)`: info + price_history + balance_sheet + cashflow + income_stmt。キャッシュ `{symbol}_detail.json`
- `screen_stocks(query)`: EquityQuery ベースのバルクスクリーニング（キャッシュなし）
- `get_price_history(symbol, period)`: OHLCV DataFrame（キャッシュなし、デフォルト1年分）

## 異常値ガード

`_sanitize_anomalies()` で以下をサニタイズ:
- 配当利回り > 15% → None
- PBR < 0.1 or PBR > 100 → None
- PER < 0 or PER > 500 → None
- ROE > 200% → None

## コミュニティグルーピング (KIK-549)

スクリーニング結果の「📊 グラフコンテキスト」セクション（Neo4j接続時のみ）にコミュニティ別銘柄グルーピングが表示される。

- `screening_context.py`: `symbol_communities` キーで各銘柄のコミュニティ所属を取得
- `screening_summary_formatter.py`: コミュニティ名 × メンバー数で表示（例: 「Technology x AI: A、B（2銘柄）」）
- LLMはこのグルーピングを解釈し「半導体関連3銘柄が上位」等のサマリーを生成
- 活用: 類似銘柄の比較分析、既保有銘柄との重複確認、分散度判断
