# Stock Skills 構造リファレンス（深掘り編）

[STRUCTURE.md](STRUCTURE.md) の続き。**関数レベル・データフロー・不変条件**まで降りる。
API シグネチャそのものは [api-reference.md](api-reference.md)（自動生成）にあるので、
ここでは **「なぜその関数がその形をしているか」** を書く。

実測日: 2026-08-02。公開関数・定数は AST で抽出した実物。

---

## 1. ブリーフィングパックの完全な構造

週次レポートも個別分析も、**すべてこのパックを経由する**。
Python が材料を集め（トークン0）、Claude が解釈だけを担う分界点。

### 1.1 生成

```bash
python scripts/build_briefing_pack.py            # 保有全体 → output/briefing/PF_YYYYMMDD.json
python scripts/build_briefing_pack.py --symbol X # 個別    → output/briefing/X_YYYYMMDD.json
```

オプション: `--no-moomoo` / `--no-context`（外部依存を切る）。
**どれが落ちてもパックは必ず返る。**

### 1.2 トップレベルキー（保有全体モード）

| キー | 生成元 | 内容 |
|:---|:---|:---|
| `pack_version` | 定数 | スキーマ版（executor の互換チェック用） |
| `meta` | — | 生成時刻・為替・保有データ源・**warnings**・`timings_sec` |
| `reconciliation` | `portfolio/reconciliation.py` | 三点照合の結果 |
| `falsification` | `portfolio/falsification.py` | 反証条件の点検 |
| `forward` | `risk/forward_events.py` | 前方イベント一式（下記 1.3） |
| `constraints` | `tax.py` + `runway.py` + `liquidity.py` | 制約（税・現金・流動性・注意） |
| `execution_audit` | `portfolio/execution_audit.py` | 決定生存率・執行ショートフォール |
| `model_audit` | `risk/model_audit.py` | 模型の予測 vs 実績 |
| `week_diff` | `portfolio/report_diff.py` | 前週差分 |
| `cumulative_diff` | 同上 | 4週・13週の累積（緩慢な漂流の検出） |
| `information` | 同上 | **静穏週かどうかの判定**（分量制御の入力） |
| `narrative` | `research/narrative.py` | 物語量スナップショット・混雑度 |
| `portfolio` | `portfolio/weekly.py` | 総資産・損益のサマリ |
| `holdings` | 同上 | 保有ごとの全項目（下記 1.4） |
| `indices` | `research/portfolio_news.py` | 主要指数の水準 |
| `holding_news` / `market_news` | Finnhub + yahoo | ニュース |
| `moomoo` | `research/moomoo_insights.py` | 13カテゴリ |
| `forward_schedule` | `briefing_pack._forward_schedule()` | 日程を1本の時系列に |
| `schedule_status` | `forward_events.symbol_schedule_status()` | **銘柄ごとの日程の状態** |
| `vol_calibration` | `portfolio/vol_calibration.py` | 前提ボラの較正 |
| `projection` | `portfolio/projection.py` | 短期/中期/長期の推移予測 |
| `scenarios` | `portfolio/weekly.py` | -10/-20/-35% の下落シナリオ |
| `positions_assumptions` | 同上 | 予測前提（較正後） |
| `monthly_contribution` | `config/cashflow.yaml` | 月次入金 |
| `prior_context` | `data/context/auto_context.py` | 過去テーゼ・懸念・lesson |

### 1.3 `forward` のサブ構造

```
forward
├ calendar              翌週の確定イベント（events / folded / unavailable_symbols）
├ concentration         イベント集中度（評価額加重）
├ policy_gaps           決算を迎えるのに政策が無い銘柄
├ triggers              政策トリガーの成立/接近
├ dividend_drops        配当落ちの分離
├ monday_outlook        月曜寄付の織り込み（先物 vs 東証終値）
├ schedule_changes      前週からの日程変更
├ lookthrough           ETF中身への実質エクスポージャー
├ lookthrough_events    中身の企業の翌週決算
├ schedule_status       銘柄ごとの4状態
├ actionable            要対応の抽出（information 判定へ渡す）
└ errors                個別に失敗した材料の理由
```

### 1.4 `holdings[]` の各要素

```
name, symbol, account, shares, cost_price, price, price_source, currency,
value_jpy, pl_jpy, pl_pct, weight_pct, week_change_pct, leverage,
fundamentals { per, pbr, roe, revenue_growth, earnings_growth,
               growth_derived, growth_annual, growth_period_warning, ... },
technicals   { rsi14, percent_b, range_52w_position, sma200_deviation_pct,
               volatility_pct, heat_state, heat_label,
               is_proxy, proxy_symbol },   ← 投信は指数代理
wow_delta    { prior_date, fields{ price/per/pbr/... : {prior, now, diff, pct} } },
competitors, note, error
```

**`price_source` が必ず入る**理由: 楽天CSV由来か yfinance 由来かで鮮度が違う。
どちらの値を見ているか分からないまま数字を語らせない。

### 1.5 節ごとのスライス（`slice_pack`）

パック全体を毎回渡すとトークンが爆発するので、節が要る材料だけ切り出す。

| 節 kind | 渡す材料 |
|:---|:---|
| `verdict` | week_diff, cumulative_diff, reconciliation, falsification, forward.actionable |
| `reconcile` | reconciliation |
| `belief` | falsification, holdings_overview |
| `forward` | forward, moomoo, indices, holdings_overview |
| `constraints` | constraints, positions_assumptions, vol_calibration, holdings_overview |
| `holding` | holding_rows, aggregate, indices, news, **schedule_status[sym]**, next_week_events, crowding, prior_context |
| `heat` | holdings_technicals, indices |
| `decide` | forward, constraints, falsification |
| `audit` | cumulative_diff, week_diff.folded_count, execution_audit, model_audit, prior_context |
| `limits` | vol_calibration ほか |

**全節に必ず渡すもの**: `meta` / `portfolio` / `reconciliation_status` / `information`。

> 照合の状態を全節に渡すのは、**未照合のまま数値を確定値として語らせない**ため。
> 幽霊ポジションがあると「存在しない資産のリスクを計算している状態」になる。

---

## 2. 三点照合の内部（`portfolio/reconciliation.py`)

### 2.1 公開関数

```python
normalize_symbol(sym)        # 7203.T と 7203 と US.AAPL を同じものにする
normalize_name(name)
position_key(row)            # ★ 口座を含めない
market_of(symbol)
aggregate(rows)
classify_quantity_diff(...)  # match / below_threshold / corporate_action /
                             # unrecorded_trade / unknown
reconcile(model, broker)
orphan_burden(...)           # 孤児ポジションの評価額比
apply_corporate_actions(...) # 分割のみ
run_reconciliation(...)      # 統合エントリ
days_since(...)
```

### 2.2 `position_key` が口座を含めない理由

味の素を「特定口座 400株」と「NISA 39株」で持っている場合、口座を鍵に入れると
**同じ銘柄が2つの別ポジションになり、片方が幽霊として検出される**。
銘柄単位に畳んで数量を合算する。

### 2.3 `classify_quantity_diff` の5分類

数量差を見つけたとき、**原因を推定してから**分類する。全部「不一致」にすると
株式分割のたびに誤検出が出て、本当の取り込み漏れが埋もれる。

| 分類 | 条件 |
|:---|:---|
| `match` | 完全一致 |
| `below_threshold` | `DEFAULT_SHARE_TOLERANCE` 以内（端株・再投資） |
| `corporate_action` | 比率が `_SPLIT_RATIOS` のいずれかに `_RATIO_TOLERANCE` で一致 |
| `unrecorded_trade` | 差分が売買で説明できる |
| `unknown` | **原因不明。最優先で人が見る** |

### 2.4 循環照合の検出

```
config/weekly_holdings.yaml  ←── import_rakuten_csv.py ←── 楽天CSV
        │
        └──→ 週次レポートの入力
```

模型は楽天CSVから作られ、その模型を楽天CSVと突合すると**必ず一致する**。
`rakuten_csv_broker.py` が「照合に使うCSV」と「模型の生成元CSV」のファイル名を
比較し、同一なら `circular=True` を立てる。

```
status = "circular"   ← "ok" ではない
independently_verified = False
```

**独立検証にする運用**: 新しいCSVを落としたら、**取り込む前に** reconcile を実行する。
2026-07-30 / 07-31 / 08-02 の3回でこれを実施し、いずれも独立検証が成立した
（9銘柄一致・差分0・幽霊なし）。

### 2.5 スコープ外を幽霊にしない

moomoo の `SCOPE = ["US"]` は「米国株しか見ていない」という意味。
日本株が moomoo に無いのは当然なので、**スコープ外の銘柄は `unverified`** にする。
`unavailable`（＝残高不明）と `ghost`（＝口座に無い）を混同すると、
日本株が全部幽霊になる。

---

## 3. 前方イベントの内部（`risk/forward_events.py`）

### 3.1 公開関数

```python
next_week_range(as_of)          # 土曜から見た次の月〜金
prior_business_day(d)           # 土日のみ考慮（祝日は不可 → 出力で明示）
build_calendar(holdings, ...)   # 翌週の確定イベント集合
symbol_schedule_status(...)     # ★ 4状態
event_concentration(calendar)   # 評価額加重の集中度
policy_coverage_gaps(calendar)  # 決算×政策なし
approaching_triggers(...)       # 政策トリガーの距離
dividend_drop_adjustments(...)  # 配当落ちの分離
monday_outlook(indices, moomoo) # 先物 vs 東証終値
detect_schedule_changes(cur, prior)
save_macro_cache / load_macro_cache   # ★ moomoo 落ち対策
build_forward_section(...)      # 統合エントリ
```

### 3.2 定数

```python
CONCENTRATION_WARN_PCT   = 15.0
CONCENTRATION_DANGER_PCT = 25.0
DEFAULT_MIN_WEIGHT_PCT   = 1.0     # これ未満のイベントは折り畳む
NIKKEI_FUTURES_TICKERS   = ("NIY=F", "NKD=F")   # 円建て優先
MACRO_CACHE_MAX_AGE_HOURS = 24 * 21
DEFAULT_HORIZON_DAYS     = 120     # 「次回決算」として出す上限
SCHEDULE_STATES = ("scheduled", "none_upcoming", "no_earnings", "unavailable")
```

### 3.3 なぜ `symbol_schedule_status` が必要だったか

2026-08-01 のレポートは7箇所以上で「日程が取得できなかった」と書いた。
しかし実際には:

| 銘柄 | レポートの記述 | 真実 |
|:---|:---|:---|
| QCOM | 「取得できず。8月上旬に決算の可能性 → 月曜にIR確認せよ」 | **次回決算 10/30。翌週は無関係で宿題自体が不要** |
| MDT | 「8月下旬が定例だが未確定」 | **9/1（あと30日）** |
| SOXL/TECL/TQQQ | 「PFの56.9%の日程が不明」 | **ETFに決算は無い。中身は AMD 8/5・実質10.6%** |

原因は2つ:
1. `_forward_schedule()` が **moomoo 専用**で、既定無効なので常に空リストを返していた。
   **取得していなかったのではなく、取得済みの yfinance の日程を見ていなかった。**
2. 各銘柄に渡していたのが日程の**リスト**だけで、空が「失敗」か「予定なし」かを
   区別する情報がどこにも無かった。

**空であることは状態ではない。** 状態を明示的に持たせた。

### 3.4 日本株の権利付最終日

配当落ち日そのものより、**買うならその日まで**という権利付最終日の方が行動に関係する。
`_is_jp(symbol)` が真なら `prior_business_day(ex_date)` に読み替え、
`holiday_caveat: True` を立てて「祝日でずれる可能性あり」と出力させる。

### 3.5 イベントの重複と加重

同一銘柄を複数口座で持つと、素朴に回すとイベントが二重に出て、
**比率も片方の口座分だけ**になる（味の素が 1.0% と表示され、実際は 11.4% だった）。
`_aggregate_holdings()` が銘柄単位に畳んで評価額比を合算する。

---

## 4. 手取り翻訳層（`portfolio/tax.py`）

### 4.1 公開関数

```python
load_tax_config / reset_cache
account_kind(...)         # 特定 / NISA
is_tax_free(...)
capital_gains_tax(...)    # 20.315%
sell_proceeds(...)
trading_fee(...) / fx_spread_cost(...)
switching_hurdle(...)     # ★ 乗り換え損益分岐率
evaluate_switch(...)      # ★ 却下しかしない
loss_harvest_value(...)
nisa_state(...) / nisa_used_from_holdings(...)
build_tax_state(...)
nisa_suitability(...)
```

### 4.2 `switching_hurdle` — この層の核心

含み益のある銘柄を売って別の銘柄に乗り換えるとき、**税で目減りした資金**で
買い直すことになる。したがって新しい銘柄は、元の銘柄を上回るだけでは足りない。

```
必要な期待優位 = f(含み益率, 税率 20.315%)
```

`evaluate_switch()` は**却下しかしない**設計。「期待優位 < 損益分岐」なら FAIL。
**買いを推奨する側には決して使わない**（税を理由に売買を正当化させない）。

### 4.3 「0円」を書かない箇所

| 項目 | 取れないとき | 理由 |
|:---|:---|:---|
| `realized_gain_ytd_jpy` | **`None`**（0ではない） | 0円だと「通算相手がいる」と誤読される |
| `loss_harvest_value` | `available: False` | 相殺対象が無いのに「約0円の税が消えます」は無意味 |
| `nisa_used_estimate` | `reliable: False` | 取得日を持たないので**当年に使った枠ではない** |

`config/tax.yaml` の `meta.verified_as_of` が空なら
**「税制設定が人の目で未確認」**という警告が毎回出る。

---

## 5. 資金ランウェイと注意予算（`portfolio/runway.py`）

### 5.1 公開関数

```python
weekly_investable(...)   # 25パーセンタイル（保守的）
runway(...)
weeks_until(...)
funding_options(...)     # ★ 4択
cash_purpose_check(...)
attention_budget(...)    # ★ 注意予算
to_graph_safe(...)       # 絶対額を落として比率だけ残す
```

### 5.2 `funding_options` の4択

**蓄積期のリバランスは売買ではなく入金で行うのが原則。**
資金が要るとき、選択肢は「売る」だけではない。

| 選択肢 | 内容 |
|:---|:---|
| (a) `sell` | 売却して作る |
| (b) `cash` | 手元現金 |
| (c) `wait` | N週後の入金を待つ |
| (d) `resize` | 買う規模を縮める |

**現実性ゲート**: `weeks > horizon * 2` なら `realistic=False`（rank 5）。
「43週待て」を最良案として出さないため。

**売却の順位付け**: `rank = 6 if thesis_alive else (2 if thesis_alive is False else 4)`。
テーゼが生きている銘柄の売却を最下位にするのは、
**「売る理由が無い」と言いながら売却を勧める**矛盾を防ぐため。

### 5.3 注意予算

> 銘柄数の上限は分散の理論値ではなく、**維持できる注意量**で決まる。

保有数 × 1銘柄あたりのレビュー時間 が週次レビュー時間を超えていないか。
孤児ポジション（thesis が無く、そもそも何を見ればいいか分からない銘柄）は
実質監視数から差し引く。

---

## 6. 流動性制約（`risk/liquidity.py`）

### 6.1 公開関数と定数

```python
DEFAULT_PARTICIPATION_RATE = 0.10   # 1日の出来高の何割まで捌けるか
TIER_LABELS = { immediate / several_days / trapped / unknown }

average_volume / fetch_average_volume
days_to_liquidate(...)
liquidity_profile(...)
portfolio_liquidity(...)          # ★ unknown を immediate と混ぜない
format_days(...)                  # 「即日」と書く。「0.0日」と書かない
check_recommendation_feasibility(...)
crash_buying_power(...)           # ★ 暴落時に買う金があるか
account_asymmetry(...)            # ★ NISA の下方非対称
build_liquidity_section(...)
```

### 6.2 4層と `unknown` の分離

| 層 | 意味 |
|:---|:---|
| `immediate` | 即日解消できる |
| `several_days` | 数日かかる |
| `trapped` | **閉じ込め資本** |
| `unknown` | **出来高が取れず判定不能** |

`unmeasurable_pct` を別に持ち、ティッカーの無い投信などを
「流動性が高い」に混ぜない。**「出来高が取れないから判定不能」を
「流動性が高い」と書いた瞬間に嘘になる。**

### 6.3 `crash_buying_power`

> 暴落時に買う金が無ければ、「暴落時に買う」は計画ではない。

`config/cashflow.yaml` の現金と入金から、下落局面で実際に投入できる額を出す。
2026-08-01 時点の実測では現金 9,927円 で、**目的付け以前にそもそも原資が無い**。

### 6.4 `account_asymmetry`

NISA 内の損失は**損益通算できない**。暴落時、特定口座には約20%の税務救済が
あるが NISA 分には無い。この非対称は平常時には見えない。

---

## 7. 因子エクスポージャー（`core/exposure.py`）

### 7.1 公開関数と定数

```python
FACTORS = ("market", "usdjpy", "rates", "oil", "semis")
ESTIMATION_WINDOWS = (60, 120, 250)
MIN_SAMPLES, TWIN_CORRELATION = 0.85, LOW_R2 = 0.20

daily_returns / fetch_returns
weekly_factor_moves(...)     # ★ 週次に揃える
build_factor_returns(...)
needs_lag(symbol)            # ★ アジア市場は米国因子に1日ラグ
estimate_exposure(...) / estimate_many(...)
portfolio_exposure(...)      # レバレッジを掛ける
describe_tilt(...)           # ★ 通貨の二重ロングを名指しする
correlation(...) / find_factor_twins(...)
stress_correlation(...) / worst_days(...)
```

### 7.2 `needs_lag` — 単位ではなく時差の問題

日本株の終値（15:30 JST）は、その日の米国市場（同日 22:30 JST 開始）を
**まだ知らない**。ラグを入れないと日本株の米国因子感応度がほぼゼロに見える。
アジア市場の銘柄には米国因子を1日シフトして当てる。

### 7.3 `_stability` を因子ごとにした理由

窓（60/120/250日）の間で係数の符号が反転したら不安定と見なす。
当初これを**銘柄単位**で判定していたため、1因子でも反転すると全因子が
unstable になり、補完係数が 1.0 に潰れていた（NVDA 91 → 85.3）。

因子ごとに `flipped` を返すようにして正常化（NVDA 91 → 74.7）。

### 7.4 通貨の二重ロング

米国株と日本の輸出企業を同時に持つと「地域は分散、為替は集中」になる。
**円高シナリオで PF の大半が同時に不利になる**のに、
セクターHHI・地域HHI はこれを**全く検出しない**。

`describe_tilt()` がこれを名指しする。

---

## 8. 限界寄与スクリーニング（`screening/marginal.py`）

```python
DEFAULT_MIN_STANDALONE = 60.0   # 単独スコアの足切り
COMPLEMENT_MIN, COMPLEMENT_MAX = 0.30, 1.60
TWIN_CORRELATION = 0.85
UNSTABLE_FACTOR_WEIGHT = 0.35   # 不安定な因子1つあたりの減衰

complement_factor(..., unstable_factors=None)
marginal_score(...)
rank_candidates(...)
lookthrough_exposure(...)
```

### 8.1 目的関数の反転

従来のスクリーニングは**銘柄単体の良さ**を最大化していた。
しかし保有者にとっての正解は「PF全体をどれだけ良くするか」。

```
限界スコア = 単独スコア × 補完係数
```

因子双子（相関 > 0.85）は補完係数が **0.45 以下に強制**される。
業種コードが違っても値動きが同一なら、それは実質的な買い増しであり
分散は改善しない。**セクターHHI が下がることを根拠にしてはならない。**

---

## 9. 執行監査（`portfolio/execution_audit.py`）

```python
DEFAULT_MATCH_WINDOW_DAYS, QTY_TOLERANCE_RATIO

match_decisions(decisions, executions)   # ★ 無理に紐付けない
survival_rate(match, executions_available)   # ★ 第2引数が本質
infer_unexecuted_reasons(...)
execution_shortfall(...)    # 符号を正規化（買いと売りが相殺しない）
split_performance(...)
build_execution_audit(...)
```

### 9.1 `executions_available` が本質

約定履歴を**取得できなかった**とき、素朴に書くと「決定生存率 0.0%」と出る。
これは「決めたのに一度も実行しなかった」という**嘘の成績**である。

```python
survival_rate(match, executions_available=False)
# → {"available": False, "reason": "約定履歴を取得できていない"}
```

テスト `test_survival_refuses_to_compute_without_execution_history` が縛っている。

### 9.2 語り口もコードに埋め込む

未執行が見つかったとき、コードが返すメッセージ:

> これはあなたの怠慢ではなく、**提案の形式が実行可能でなかった**証拠として扱います。

執行できなかった原因を人格ではなく**提案の設計**に帰す。

### 9.3 取得元の変更（2026-08-02）

以前は `moomoo_broker.fetch_executions()` **だけ**を見ていた。
しかし moomoo 口座には資金が無く、実際の売買は全て楽天にある。
**取得先が実態と食い違っていたので、原理的に永久に測定できなかった。**

```
楽天取引履歴CSV（第一）→ moomoo（補助）→ 両方の理由を連結して返す
```

---

## 10. 反証条件（`portfolio/falsification.py`）

```python
parse_condition / parse_conditions        # 曖昧な記述を拒否
market_state_from_holding(...)
suggest_conditions_from_content(...)      # ★ 本文から撤退ラインを抽出
evaluate_suggestions(...)                 # ★ 既に割れているか
check_thesis(thesis, state)
check_all(...)
```

### 10.1 OR セマンティクス

複数の反証条件のうち**1つでも成立すればテーゼは反証**とみなす。

> 「全部壊れないと認めない」は、間違いを認めない構造そのもの。

### 10.2 4状態 + `missing`

| state | 意味 |
|:---|:---|
| `met` | 反証条件が成立した |
| `near` | 接近中 |
| `far` | 抵触していない |
| `unknown` | **指標が取れず点検できなかった**（「問題なし」ではない） |
| `undefined` / `invalid` | 反証条件が無い / 壊れている → `missing` |

`checked` に `missing` を含めない。含めると「N件点検した」が嘘になる。

### 10.3 本文からの抽出（2026-08-02 追加）

QCOM のテーゼには「撤退ライン 約$158」と**本文に書かれていた**が、
反証条件として登録されていなかった。7/29 終値は $155.68 で**既に割れていたのに
検出されなかった**。レポートが出したのは「反証条件が未定義です」という
一般的な促しだけだった。

**書いてあるのに拾えないのは、促しの問題ではなく抽出の問題である。**

抽出パターン（数値が書かれているものだけ）:

| 本文 | 抽出結果 |
|:---|:---|
| 撤退ライン 約$158 を割ったら見直す | `price <= 158` |
| 損切り 2,400円 を下回ったら撤退 | `price <= 2400` |
| -25% で撤退する | `drawdown_pct <= -25` |
| 撤退は -15% を目安に | `drawdown_pct <= -15` |
| **調子が悪ければ売る** | **抽出しない**（測定不能） |

価格パターンは**直前のマイナス記号と直後の `%` を除外**する。
除外しないと「撤退は -15% を目安に」から `price <= 15` という
無意味な条件が出る。

**提案するだけで自動登録はしない。** `has_falsification` は False のまま、
`falsified` も False のまま。条件の確定は判断であり、
本文の言い回しから機械が断定してよいものではない。

---

## 11. 情報量比例レポート（`portfolio/report_diff.py`）

```python
DEFAULT_SNAPSHOT_DIR, SNAPSHOT_VERSION
CUMULATIVE_WINDOWS = (4, 13)    # 週
CUMULATIVE_MULTIPLIER

build_snapshot / save_snapshot / load_snapshots
prior_snapshot(...)     # ★ 無ければ None（別の週で代用しない）
diff_snapshots(...)
cumulative_diff(...)    # 緩慢な漂流の検出
assess_information(...) # ★ 静穏週かどうか
```

### 11.1 `prior_snapshot` が代用しない理由

前週のスナップショットが無いとき、2週前で代用すると
**「1週間の変化」として2週間分の変化を見せる**ことになる。
None を返して「比較できない」と書かせる。

### 11.2 累積差分が要る理由

毎週 -1.5% ずつ下がる銘柄は、週次では毎回「小さな変化」として折り畳まれる。
しかし13週で -18% になる。**週次の閾値を通過し続ける緩慢な漂流**を
4週・13週の窓で拾う。

### 11.3 静穏週の判定

`information.quiet` が真ならレポートは**全体30行以内**で終える。
**それは失敗ではなく正しい出力。**

ただし `_falsification_gaps()` により、**反証条件が未定義の thesis があると
静穏週にはならない**。「変化が無い」ことと「点検できていない」ことは違う。

---

## 12. 模型監査（`risk/model_audit.py`）

```python
MIN_WEEKS = 26
SIGNIFICANCE_P

predict_return(...)
record_week(...)          # ★ 不完全な組でも記録する
load_scorecard / score_model(...)
_binomial_tail_p(...)     # scipy なしの二項検定
suggest_missing_factor(...)   # ★ 仮説の提示のみ。自動追加しない
propagate_confidence(...)
build_model_audit(...)
```

### 12.1 リスク模型を「毎週採点される仮説」にする

因子モデルが予測した週次リターンと、実際のリターンを毎週記録する。
26週たまったら二項検定で「この模型は当たっているか」を判定する。

**26週未満なら判定を保留する。** 少ないサンプルで「当たっている」と言わない。

### 12.2 `suggest_missing_factor` は自動追加しない

予測が系統的に外れているとき、欠けている因子の候補を**提示する**。
しかし自動でモデルに追加はしない。**モデルの変更は判断であり、
外れたという事実だけから機械が決めてよいものではない。**

### 12.3 単位を揃える（実装中に踏んだ罠）

指数ウォッチの `percent_change` は**日次**、実現リターンは**週次**。
そのまま比較していたため予測が常に不能になっていた。
`weekly_factor_moves()` が直近5日を複利で合成する。

---

## 13. 系譜会計（`core/provenance.py`）

```python
PROVENANCE_TYPES = (PRIMARY, EXTERNAL, USER, SELF, LEGACY)
DEPTH_LIMIT_HOLDING = 3
DEPTH_LIMIT_WATCH   = 4

classify_source(...)      # ★ 取得元ドメインで機械判定（自己申告を認めない）
build_claim / link_derivation
trace_to_primary(...)
provenance_summary(...)
needs_regrounding(...) / filter_usable(...)
reground(...) / regrounding_queue(...)
save_claim / load_claims
claims_from_decision_package(...)
```

### 13.1 四系譜

| 系譜 | 深度 | 例 |
|:---|---:|:---|
| `primary_observation` | 0 | 価格・EDINET/TDnet/EDGAR 原文・IR原文 |
| `external_discourse` | 1 | ニュース・アナリスト見解・**yfinance等の加工済み指標** |
| `user_statement` | 1 | テーゼ・投資メモ |
| `self_inference` | 祖先の最大+1 | **システム自身の解釈 ← 汚染源** |
| `legacy` | 1 | 案C導入前の既存ノード（遡及型付けはしない） |

**型付けは取得元ドメインで機械判定し、自己申告を認めない。**
又聞きニュースを一次情報と偽装されると深度会計そのものが無意味になる。
出所不明は一次に格上げせず外部言説にする。

### 13.2 深度閾値と再接地

| 対象 | 閾値 | 挙動 |
|:---|---:|:---|
| 保有銘柄 | 深度3以上 | 再接地なしに新しい解釈へ使えない |
| ウォッチ | 深度4以上 | 同上（緩和） |

**再接地の錨は一次観測でなければならない。** ニュースへの接地は再接地ではない。

再接地は「現在アクティブな解釈に使われる主張のみ」を対象にする**怠惰評価**。
全 legacy 資産の再導出という非現実的なコストを避けるため。

### 13.3 レポートへの常設

```
本解釈の根拠: 一次観測62%／外部言説21%／自己推論17%（最大深度2）
```

自己推論が過半、または深度が閾値超えなら:

```
⚠️ 本見解は当システムの過去解釈への依存が強く、独立根拠が薄い。
```

---

## 14. 政策台帳（`policy/`）

```python
OPERATORS, DEFAULT_COOLING_HOURS = 24

validate_trigger(...)   # ★ 測定可能性と矛盾を審査
build_policy(...)
is_expired(...)
revise_policy(...)      # ★ トリガー接近中は冷却期間
expire_policy(...)
save_policy / load_policy / list_policies
```

### 14.1 登録時の審査

| 拒否するもの | 理由 |
|:---|:---|
| 「なんか下がったら」 | 測定不能 |
| `x <= -25` かつ `x >= -5` | 同一指標の矛盾条件 |
| 失効期限なし | **無期限政策は硬直化する** |

使える指標: `price` / `price_change_pct` / `drawdown_pct` / `rsi` / `per` / `pbr` /
`dividend_yield` / `operating_cf` / `operating_margin` / `position_weight_pct` / `days_held`

### 14.2 冷却期間の非対称性

**平時の改訂は自由。トリガー接近・成立中の改訂だけ24時間の冷却期間がかかる。**

意図的な非対称で、平時の柔軟性（環境変化への追随）と
有事の拘束（狼狽による政策破棄の防止）を分離する。

### 14.3 逸脱の2種類

| 種別 | 意味 |
|:---|:---|
| `acted_without_trigger` | トリガー未成立なのに政策と反する売買をした |
| `ignored_trigger` | トリガー成立済みなのに政策の応答を実行していない |

**逸脱は「過程」の誤りなので `origin=process` の lesson になり、
plan-check の制約になる資格を持つ。** 結果由来の lesson とは扱いが違う。

---

## 15. 判断パッケージ（`decision/`）

```python
KNOWABLE_KINDS

classify_by_disclosure_time(...)   # ★ 開示時刻で「知り得たか」を分ける
build_package(...)
save_package / load_package / list_packages
verify_package(...)                # 封印ハッシュの検証
```

### 15.1 可知集合の凍結

売買を記録した瞬間に、**その時点で知り得た情報の集合**を凍結する。

```
used              … 実際に使った情報
available_unused  … 知り得たのに使わなかった情報
unknowable        … 当時は開示されていなかった情報
```

**ここで凍結しなかった判断は永久に再審不能になる。**
後から作れば必ず後知恵で汚染される。

### 15.2 過程再審の順序制約

`process_review()` は**結果を見る前に**実行しなければならない。
順序違反は例外で拒否される。結果を知ってから過程を評価すると、
運と技能を分離できない。

---

## 16. データフローの実例：週次レポート1回分

```
土曜 07:12  タスクスケジューラ
  │
  ├─ run_weekly_deep.bat
  │    MOOMOO_ENABLED=on / PYTHONUTF8=1 / WEEKLY_DEEP_MODEL=opus
  │
  └─ weekly_deep_driver.py
       │
       ├─[層1] build_briefing_pack()                     約416秒
       │   ├ load_holdings_config()      config/weekly_holdings.yaml
       │   ├ fetch_prices()              yahoo_client（3層キャッシュ）
       │   ├ build_report_data()         損益・比率・テクニカル
       │   │    └ calibrate_positions()  ★ 前提ボラを実測で較正
       │   ├ run_reconciliation()        楽天CSV と突合 → 循環判定
       │   ├ check_all()                 反証条件の点検
       │   ├ build_forward_section()     翌週イベント + ルックスルー
       │   │    ├ get_events_for()       yfinance 決算日・配当日
       │   │    ├ _macro_events()        moomoo → 落ちたら退避キャッシュ
       │   │    ├ build_lookthrough()    ETF中身（1x proxy 経由）
       │   │    └ symbol_schedule_status()  ★ 4状態
       │   ├ build_tax_state() / funding_options() / build_liquidity_section()
       │   ├ build_execution_audit()     楽天取引履歴CSV → moomoo
       │   ├ build_model_audit()         26週たまるまで保留
       │   ├ diff_snapshots() / cumulative_diff() / assess_information()
       │   ├ capture_many()              物語量（180秒の時間予算）
       │   ├ collect_weekly_insights()   moomoo 13カテゴリ
       │   └ get_context()               Neo4j + TEI から過去テーゼ
       │        → output/briefing/PF_YYYYMMDD.json
       │
       ├─[層2] 節ごとに claude -p
       │   for section in build_sections(pack):
       │       material = slice_pack(pack, section)
       │       text = claude_p(spec=weekly_deep.md, material)
       │       write(output/weekly_deep/YYYYMMDD/<section>.md)
       │       state["done"].append(section)      ← チェックポイント
       │   ※ 使用量上限 → exit 2。次の起動が続きから
       │
       ├─ assemble()                     節を固定順で結合
       ├─ resync_missing()               ★ 過去分が vault から消えていないか
       └─ save_and_sync()                output/ → vault → 実在検証
            → C:\Users\swend\iCloudDrive\swender\投資記録\週次PF分析_YYYYMMDD.md
```

---

## 17. 縮退の実例：何が落ちると何が消えるか

| 落ちるもの | 消える節 | 代替 |
|:---|:---|:---|
| yfinance | 価格・テクニカル・決算日 | **代替なし**（本体が成立しない） |
| moomoo | 経済指標・FedWatch | **退避キャッシュ（21日）** |
| moomoo + キャッシュ切れ | マクロイベント | 「取得できなかった」と明示 |
| Neo4j | 過去テーゼ・懸念・lesson | intent-routing のみで判断 |
| TEI | ベクトル検索 | シンボルベース検索のみ |
| Grok | 定性リサーチ | 数値分析のみ |
| Finnhub | 米国株ニュース | yahoo のニュース |
| 楽天CSV | 三点照合 | 「照合不能」と明示（**一致とは書かない**） |
| 楽天取引履歴 | 決定生存率 | 「測定できていない」と明示 |
| vault | 同期 | `output/` のみで完了 |
| `claude` CLI | 層2 全体 | exit 1（層1のパックは残る） |

**どれが落ちても、落ちたことが出力に残る。** これが縮退の条件。

---

## 18. 命名と配置の規約

### 18.1 レイヤー間の依存方向

```
Skills → Core → Data
           ↓
        Output
```

- **Core は Data の具象に依存しない**（`ports/` の Protocol 経由）
- **Output は Core の結果を受け取るだけ**（計算しない）
- **Data は Core を import しない**

### 18.2 新モジュールの配置

| 種類 | 置き場所 |
|:---|:---|
| ドメインロジック | `src/core/{screening,portfolio,risk,research,health}/` |
| データ取得/保存 | `src/data/{yahoo_client,graph_store,graph_query,history,context}/` |
| 出力整形 | `src/output/` |
| 汎用ユーティリティ | `src/core/common.py` |
| テスト | `tests/{core,data,output}/`（src と1:1対応） |

### 18.3 分割の基準

- 1ファイルに3つ以上の独立した責務 → 分割
- 500行超 → 分割を検討
- 2つ以上のスキルから参照される共通ロジック → `src/core/` に昇格

分割時は旧パスに `sys.modules` リダイレクトの shim を残し、
`DeprecationWarning` を付ける。実例: `health_check.py` → `health/` (KIK-576)、
`history/save.py` → `save_*.py` (KIK-578)。

### 18.4 `HAS_MODULE` パターン

スクリプト層（`run_*.py`）は `try/except ImportError` で各モジュールの存在を確認し、
`HAS_*` フラグで縮退する。共通フラグ（`HAS_HISTORY_STORE` /
`HAS_GRAPH_QUERY` / `HAS_GRAPH_STORE`）は `scripts/common.py` に一元管理。

---

## 19. 実測で確定した外部APIの制約

推測ではなく**実際に叩いて分かったこと**。同じ調査を繰り返さないための記録。

| API | 制約 |
|:---|:---|
| moomoo US LV3 | **日本株は全権限なし**。米指数・米オプションも権限なし（IV不可） |
| moomoo 残高 | `No available real accounts with US market authority` → 残高照合に使えない |
| yfinance ETF | **レバレッジETFの `top_holdings` は無意味**（TQQQ は `IQMM` 単独） |
| yfinance 日本株 | `earningsGrowth` が欠けることが多い → income_stmt から導出 |
| yfinance 比率 | `earningsGrowth`/`revenueGrowth` は**四半期YoY**（2737.T は +1043%） |
| yfinance 投信 | ティッカーが無く基準価額の時系列を取れない → 連動指数で代理 |
| yfinance 先物 | `NIY=F`（円建て）/ `NKD=F`（ドル建て）は取得可能 |
| Finnhub 無料枠 | **指数クオート・日本株ニュース非対応** |
| GDELT | HTTP 429 が頻発 → 5.5秒スロットル + サーキットブレーカ |
| `numberOfAnalystOpinions` | `get_stock_info` には無く `get_stock_detail` にのみ存在 |

---

## 20. 用語集

| 用語 | 意味 |
|:---|:---|
| **三点照合** | 模型（システムの信じる保有）/ 実在（口座）/ 意図（thesis）の突合 |
| **循環照合** | 模型の生成元と同じデータで照合すること。必ず一致するので検証にならない |
| **幽霊ポジション** | 模型にあるが口座に無い。存在しない資産のリスクを計算している状態 |
| **孤児ポジション** | 保有しているが thesis も政策も無い。**なぜ持っているか未記述** |
| **反証条件** | 「これが起きたらこのテーゼは間違い」と事前に書いた測定可能な条件 |
| **乗り換え損益分岐率** | 含み益のある銘柄を売って買い直すとき、税で目減りする分のハンデ |
| **限界スコア** | 単独スコア × 補完係数。PF全体への寄与で測る |
| **因子双子** | 相関 0.85 超。業種が違っても値動きが同一なら実質的な買い増し |
| **通貨の二重ロング** | 米国株＋日本の輸出企業。地域は分散、為替は集中 |
| **ETFルックスルー** | ETF の中身の企業への実質エクスポージャー（PF比率×内部ウェイト×レバレッジ） |
| **閉じ込め資本** | 出来高が薄く、想定期間内に解消できない保有 |
| **決定生存率** | 判断したことのうち、実際に約定まで到達した割合 |
| **執行ショートフォール** | 提案時価格と実際の約定価格の差 |
| **物語量 / 混雑度** | その銘柄が語られている量。購入時比で何倍か |
| **前提HHI** | thesis に書かれた前提の集中度。資産空間ではなく信念空間の分散 |
| **系譜 / 深度** | 主張の出所（一次観測〜自己推論）と、自己推論の連鎖の深さ |
| **再接地** | 深度が閾値を超えた主張を、一次観測に当て直すこと |
| **可知集合** | 判断時点で知り得た情報の集合（used / available_unused / unknowable） |
| **意図的不作為** | 「動かない」を明示的な意思決定として記録すること |
| **静穏週** | 有意な変化が無い週。**30行以内で終えるのが正しい出力** |
| **注意予算** | 維持できるレビュー時間。銘柄数の上限を決めるのは分散理論ではなくこれ |
