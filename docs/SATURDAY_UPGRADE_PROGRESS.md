# 土曜週次レポート連携 — 実装進捗

出典: `STOCK_SKILLS_土曜週次レポート連携設計書_Opus5.md`（S1〜S8）
着手: 2026-07-29 / バックアップコミット: **`8ce46da`**
ロールバック: `git reset --hard 8ce46da`

方針（ユーザー承認済み）:
- 実装範囲: **S1→S4 を完全実装**（中途半端にしない）。余力があれば S5 以降
- 実在残高: **moomoo 自動 ＋ 楽天CSV**
- 週次: **weekly_deep を7セクション固定骨格に作り変える**（旧版はフォールバックで残す）
- git: **段階ごとに commit ＋ push**（テスト全緑のときだけ push）

---

## S1 — 提案1（三点照合）＋ 提案7（スナップショット記録のみ） ✅ 完了

### 新規ファイル

| ファイル | 役割 |
|:---|:---|
| `src/data/brokers/base.py` | ブローカースナップショットの正規形。`available` / `as_of` / `scope` を必ず持つ |
| `src/data/brokers/rakuten_csv_broker.py` | 楽天CSV由来の実在残高。CSV原本を優先し、無ければYAMLへ縮退 |
| `src/data/brokers/moomoo_broker.py` | moomoo(OpenD) の米国株残高・約定履歴。scope は US のみ |
| `src/data/brokers/__init__.py` | `collect_snapshots()` / `merged_scope()` / `is_reconcilable()` |
| `src/core/portfolio/reconciliation.py` | 照合エンジン本体。幽霊/未記録/孤児/CA分類 |
| `src/output/reconcile_formatter.py` | 第1セクションの出力整形 |
| `src/core/research/narrative.py` | 物語量スナップショット記録（GDELT/Finnhub/yfinance 多重化） |
| `.claude/skills/stock-portfolio/scripts/portfolio_commands/reconcile.py` | `reconcile` サブコマンド |
| `tests/core/test_reconciliation.py` (28) / `tests/data/test_brokers.py` (16) / `tests/core/test_narrative.py` (17) | 受け入れ基準のテスト |

### 変更ファイル
- `run_portfolio.py` — `reconcile` サブコマンド追加
- `src/core/research/briefing_pack.py` — パックに `reconciliation` / `narrative` を追加
- `scripts/build_briefing_pack.py` — `--no-reconcile` / `--no-narrative`
- `.gitignore` — `data/narrative/` `data/reconciliation/`

### 設計判断（設計書から踏み込んだ点）

**1. 循環照合の検出（設計書に無い・実装して初めて判明）**

`config/weekly_holdings.yaml` は楽天CSVの取り込み出力であり、同時に週次レポートの入力（模型）でもある。
つまり素朴に実装すると「模型 vs その模型を作った元CSV」を突き合わせて必ず一致し、
**照合が常に成功する嘘の装置**になる。

対策: 読んだCSVが `source.file` と同一なら `circular=True` を立て、照合ステータスを
`ok` ではなく **`circular`（独立検証なし）** にする。`independently_verified=False` を
パックに載せ、週次レポート冒頭で警告する。実残高を本当に確かめるには新しいCSVの保存が要る。

**2. scope 外を幽霊にしない**

moomoo は US LV3 で米国株しか見えない。素朴に実装すると日本株・投信が全部
「口座に不在＝幽霊」になる。取得**成功した**ソースの scope の和集合だけを見て、
その外側は `unverified`（残高不明）に落とす。失敗したソースの scope は数えない。

**3. 自動補正は分割/併合だけ**

売買の記録漏れを自動で辻褄合わせすると「どの取引を記録し忘れるか」という
記録習慣の弱点が見えなくなる。`apply_corporate_actions()` は `auto_fixable` な
コーポレートアクションのみを模型に反映する。

**4. 物語量ソースの多重化（設計書は GDELT 単独を想定）**

GDELT はこの回線から 429 が連続する（実測）。単一ソースに賭けると記録が途切れ、
**混雑度の分母が永久に欠ける**。GDELT → Finnhub → yfinance の順に試し、
取れたものを `volume_source` 付きで記録する。

重要: `crowding()` は**同一ソース同士でしか比を作らない**。GDELT の全世界記事数と
Finnhub の企業ニュース件数は母集団が違い、比にすると無意味な数字が出る。
さらに GDELT 連続失敗時はサーキットブレーカーで以後スキップし、週次バッチが止まらないようにする。

### 現状の実測結果（本人PF）

```
判定: 🟡 独立検証なし
口座: 9銘柄 / 模型: 9銘柄 / 一致 9 / 差分 0
⚠️ 孤児ポジション 6件 — 味の素・ニトリ・SOXL・TECL・TQQQ・FANG+
```

→ **保有の大半に thesis も政策も無い。** これは設計書が「どのシステムも検出しない」と
書いた通りの状態。S4 で政策カバレッジの穴として毎週突きつける。

### やることリスト（ユーザー側の手作業）
- [ ] 楽天証券から**新しいCSV**を保存 → `python scripts/import_rakuten_csv.py`
      （これをやるまで照合は `circular` のまま＝独立検証にならない）
- [ ] 孤児6件に thesis か政策を付ける（`/investment-note save --type thesis` か
      `python scripts/manage_policy.py add`）
- [ ] moomoo で残高照会したい場合は `MOOMOO_ENABLED=on`。取引照会に
      unlock が要る環境では `MOOMOO_TRADE_PWD_MD5` を環境変数で設定

---

## S2 — 提案8（情報量比例・差分レポート） ✅ 完了

### 新規ファイル

| ファイル | 役割 |
|:---|:---|
| `src/core/portfolio/report_diff.py` | 週次スナップショット・前週差分・4週/13週累積差分・情報量判定 |
| `src/core/portfolio/falsification.py` | thesis の反証条件を点検（「信念の変化」を見る層） |
| `src/output/weekly_diff_formatter.py` | 判定ブロック・信念セクション・折り畳み表示 |
| `tests/core/test_report_diff.py` (21) / `tests/core/test_falsification.py` (24) | 受け入れ基準のテスト |

### 変更ファイル
- `src/data/note_manager.py` — `save_note(..., falsification=...)`（thesis 限定）
- `manage_note.py` — `--falsification` 追加。**測定不能な条件は保存前に拒否**
- `briefing_pack.py` — `falsification` / `week_diff` / `cumulative_diff` / `information`
- `.gitignore` — `data/weekly_snapshots/`

### 設計判断

**1. 反証条件は測定可能でなければ受け付けない**

「業績が悪化したら」は保存時に弾く。点検できない条件を許すと、書いた気になるだけで
週次の点検対象が増えない。使える指標は政策台帳の `MEASURABLE_METRICS` を継承し、
テーゼ検証向けに `revenue_growth` / `roe` / `earnings_growth` 等を追加した
（政策側の集合は変えていない＝非破壊）。

**2. 複数条件は OR**

「全部壊れないと反証を認めない」は、間違いを認めない構造そのもの。1つでも成立したら反証。

**3. 「指標が取れなかった」を「抵触なし」と言わない**

`unknown` を独立の状態として持ち、レポートで「未点検」と明示する。

**4. 静穏週の判定を厳しくした（設計書から踏み込んだ点）**

素朴に実装すると、照合が `circular`（独立検証なし）でも孤児が6件あっても
「静穏週・何もしなくてよい」と出てしまう。以下を要対応に数えるようにした:

- 照合が `unreconciled` / `circular`（独立検証できていない）
- 孤児ポジションの存在
- 反証条件が未定義 / 点検不能な thesis

**5. 緩慢な変化の第二の網**

前週比だけだと「毎週2%ずつ下落」が週次閾値5%に永久に引っかからない。
4週/13週の累積差分を併走させ、`slow_drift` として別枠で出す。
N週前が無いときは直近で代用せず「蓄積中」と言う（代用すると検出が壊れる）。

### 現状の実測結果（本人PF）

```
■ 今週の判定：要対応週（要対応 3件）
  1. 【信念】反証条件が未定義の thesis 2件（MDT / QCOM）
  2. 【照合】残高の独立検証ができていません
  3. 【照合】孤児ポジション 6件（評価額の 77.6%）
```

→ **評価額の 77.6% が「なぜ持っているか未記述」。** 設計書が予告した通りの状態。

### やることリスト（ユーザー側）
- [ ] MDT / QCOM の thesis に反証条件を追加
      例: `manage_note.py save --symbol QCOM --type thesis --content "..." --falsification "revenue_growth < 0"`
- [ ] 孤児6件（味の素・ニトリ・SOXL・TECL・TQQQ・FANG+）に thesis か政策を付ける


## S3 — 提案3（手取り）＋ 提案9（現金・入金） ⬜ 未着手

## S4 — 提案4（前方イベント）＋ 政策台帳の合流 ⬜ 未着手

## S5以降 ⬜ 未着手
