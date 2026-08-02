# 週次ポートフォリオ分析レポート

## 2層アーキテクチャ（材料集め + Claude synthesis）

旧レポート（`weekly_report.py`）は Python の文字列整形で数字を並べるだけで、
**「材料 → 保有株への含意」をつなぐ解釈層が無かった**。深掘り版は2層に分ける。

```
層1  ブリーフィングパック生成（Python・トークン0）
     scripts/build_briefing_pack.py → output/briefing/PF_YYYYMMDD.json
     保有・前回差分・指数・ニュース・moomoo・競合・過去テーゼ/懸念/lesson を全部束ねる

層2  Claude synthesis（headless `claude -p`・節単位チェックポイント）
     scripts/weekly_deep_driver.py + .claude/prompts/weekly_deep.md
```

**節単位である理由**: 1節=1回の `claude -p`。書けた節はファイルに落として
`state_YYYYMMDD.json` に記録するので、使用量上限に当たっても中断（exit 2）で済み、
次の起動が**続きから**再開する。人の操作は要らない。

## 固定骨格（土曜設計書 第3章）

**節の順序は意思決定論的に固定されており、変更しない。**

```
0. 判定        — 情報量（静穏週かどうか）※最後に書いて先頭に置く
1. 照合        — 私の模型は現実と一致しているか（提案1）
                  ※ここが通らなければ以降は条件付きで書く
2. 信念の変化  — 反証条件に触れたテーゼはあるか（提案8）
                  ※価格ではなく信念の変化を最初に見る
3. 前方イベント — 翌週に何が確定して起きるか（提案4）
                  ※政策カバレッジの穴をここで検出
4. 制約        — 政策・税・現金・流動性・注意（提案3/6/9）
                  ※行動可能な空間を先に確定させる
5. 機会        — 制約内での保有の立ち位置（銘柄別・過熱横断）
                  ※3と4を通過した後にのみ提示
6. 事前決定    — 翌週の条件付き政策 ← **土曜の唯一の「行動」**
7. 監査        — 執行・模型の健全性（静穏時は折り畳み）
8. 前提と限界  — 系譜サマリ
```

機会（買い候補）を先に出すのは、**自宅が燃えているかを確認する前に買い物に行くのと同じ**。

各節には**その節が必要とする材料だけ**を渡す（`slice_pack`）ので、密度を落とさずトークンを抑える。
ただし**照合の状態は全節に渡す**（未照合のまま数値を確定値として語らせないため）。

### 第0原則: 土曜は「何を買うか」を答える日ではない

土曜は **完全情報 × 執行不能** という週に一度の特異点である。
土曜に「A社買い推奨」と出しても執行は月曜以降で、その時点の価格は分析前提と異なる。
**推奨は執行時に必ず陳腐化しているのに、推奨という形式は心理的コミットを生む。**

したがって正しい出力形式は**条件付き政策**:
「A社を、月曜寄付で PBR 0.8倍以下かつ日経が -2% 以内なら資金の3%まで取得する。
条件を外れたら見送り、次の土曜に再評価する」

### 分量は情報量に比例させる（提案8）

`information.quiet` が真（静穏週）なら、銘柄別の節を展開せず**全体30行以内**で終える。
それは失敗ではなく**正しい出力**である。折り畳みは削除ではない（件数を示して残す）。

## スケジュール

**毎週土曜 07:12 JST** に Windows タスクスケジューラが自動実行する。

```
タスク名: StockSkills\WeeklyDeep         （本番・土曜 07:12）
          StockSkills\WeeklyDeepResume   （3時間ごと・--resume-only）
実体:     scripts\run_weekly_deep.bat → scripts\weekly_deep_driver.py
ログ:     output\weekly_deep.log（追記）
出力:     C:\Users\swend\iCloudDrive\swender\投資記録\週次PF分析_YYYYMMDD.md
旧版:     StockSkills\WeeklyReport（薄い版・フォールバックとして当面併存）
```

`WeeklyDeepResume` は **未完了の途中状態があるときだけ**動く（`--resume-only`）。
無ければ即終了するので、平日に勝手にレポートを書き始めることはない。
中断が日をまたいでも `--resume-within-days`（既定3日）以内なら同じレポートを書き継ぐ。

土曜朝にしてあるのは、**日本株の金曜引けと米国株の金曜引け（＝土曜朝5〜6時JST）の
両方が確定した後**だから。

手動実行:

```bash
python scripts/weekly_deep_driver.py                  # 生成→執筆→vault同期
python scripts/weekly_deep_driver.py --dry-run        # 保存せず標準出力
python scripts/weekly_deep_driver.py --max-sections 3 # 3節だけ書いて中断（動作確認用）
python scripts/weekly_deep_driver.py --restart        # 途中状態を捨てて最初から
python scripts/weekly_report.py                       # 旧・薄い版
```

### ⚠️ スケジュール設定の必須要件（2026-08-01 に2回連続で未実行だった）

作成直後のタスクは既定値が全部不利で、**7/25 と 8/1 の2回とも発火しなかった**。
`LastRunTime` が `1999/11/30`（＝一度も実行なし）だったことで発覚した。

| 設定 | 必須値 | これが違うと |
|:---|:---|:---|
| `WakeToRun` | **True** | 7:12 にスリープ中なら起こされず発火しない |
| `StartWhenAvailable` | **True** | 逃した回を**Windowsが破棄**し、NextRunTime が翌週へ飛ぶ |
| `DisallowStartIfOnBatteries` | **False** | バッテリー駆動だと起動しない |
| `StopIfGoingOnBatteries` | **False** | 実行中に電源が抜けると止まる |

**`StartWhenAvailable` が決定打。** これが False だと、一度逃した回は自動では
二度と実行されず、`NextRunTime` だけが次週に進む（今回そうなった）。

```powershell
$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun `
     -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
     -ExecutionTimeLimit (New-TimeSpan -Hours 72)
$s.MultipleInstances = 'IgnoreNew'
Set-ScheduledTask -TaskName 'WeeklyDeep' -TaskPath '\StockSkills\' -Settings $s
```

**手動実行と `WeeklyDeepResume` の競合に注意。** Resume は3時間ごとに起動して
未完了 state を書き継ぐ。手で `weekly_deep_driver.py` を回している間は
`Disable-ScheduledTask -TaskName 'WeeklyDeepResume'` で止め、
**終わったら必ず `Enable-ScheduledTask` で戻す**。

タスクの状態確認:

```powershell
Get-ScheduledTaskInfo -TaskName 'WeeklyDeep' -TaskPath '\StockSkills\'
schtasks /Run /TN "StockSkills\WeeklyDeep"   # 即時実行

# 深掘り版が安定したら旧版を止める（元に戻すのは Enable-ScheduledTask）
Disable-ScheduledTask -TaskName 'WeeklyReport' -TaskPath '\StockSkills\'
```

## 個別銘柄も同じ2層で「常に全力」

銘柄について聞かれたら、軽い聞き方でも毎回パックを作って深掘りする。

```bash
python scripts/build_briefing_pack.py --symbol SOXL   # → .claude/prompts/stock_deep.md で執筆
```

詳細は `.claude/rules/intent-routing.md` の「分析ドメイン」冒頭を参照。

## 保有・株価データの取得

**証券口座への自動ログインはしない。** 無人実行のために資格情報を保存するのは
リスクが高く、2FA も突破できず、規約上も危うい。

代わりに、あなたがログイン済みの楽天 MarketSpeed II が RSS 関数で Excel に
書き出した値を読む。資格情報はリポジトリに一切置かない。

優先順位:

1. **楽天 MS2 RSS スナップショット**（`config/rakuten.yaml` の `snapshot_path`）
2. `config/weekly_holdings.yaml` — **推奨は楽天の保有商品一覧CSVから取り込む**

### 推奨: 保有商品一覧CSVの取り込み

楽天証券Web → マイメニュー → 保有商品一覧（すべて） → CSVで保存 →

```bash
python scripts/import_rakuten_csv.py            # Downloads の最新を自動検出
python scripts/import_rakuten_csv.py --dry-run  # 差分だけ確認
```

**MS2 RSS より優先してこちらを使う理由:** `RssPositionList` は楽天公式ヘルプ上
**国内株式のみ**が対象で、外国株式・投資信託用の関数は提供されていない。
このPFは評価額の約8割が米国株と投信なので、RSS では本体をカバーできない。
CSVなら国内株・米国株・投信・外貨預り金・為替が1ファイルに揃い、
Excel も常駐アプリも要らない。

### 取引履歴（決定生存率の入力）

保有CSVとは別に、**取引履歴CSV**も落とすと「判断したことを実際に実行できたか」
が測れるようになる。

```bash
python scripts/import_rakuten_trades.py --audit
```

楽天証券Web → マイメニュー → 取引履歴 → 期間指定 → CSVで保存。

以前は約定履歴の取得元が moomoo だけだったが、この運用では moomoo 口座に
資金が無く実際の売買は全て楽天にあるため、**取得先が実態と食い違っていて
決定生存率は原理的に測定できなかった**（2026-08-01 のレポートが
「90日間測定不能」と書いていたのはこれが理由）。

CSVが無いときは「約定0件」ではなく**「取得できなかった」**と報告される。
混同すると「執行率0%」という嘘の成績が出る。

**構成と価格の分離:** CSVを取り込むのは**保有構成が変わったときだけ**でよい。
日々の株価はレポート生成のたび yfinance が取り直す。取り込みから30日を超えると
レポート冒頭に「この間に売買していれば反映されていません」と警告が出る。

MS2 RSS を使う場合、国内株の現在値は RssMarket で引けるが米国株は取れない。
その場合**数量・取得単価だけ楽天を正とし、株価は yfinance で補完する**。
レポートの `price_source` 列にどちらから来た値かが必ず出る。
テンプレートは `python scripts/make_rakuten_template.py` で生成できる。

RSS が読めなかった場合、レポート冒頭に「実口座で売買した分は反映されていません」
という警告が出る。この警告が出ていたら数字を信用しすぎないこと。

スナップショットが古い（既定 72時間超）場合も警告が出る。先週の終値のつもりで
先々週を見る事故を防ぐため。

## レポートの内容

| セクション | 内容 | 材料 |
|:---|:---|:---|
| 0 判定 | 静穏週 / 要対応週 と要対応リスト | `information` |
| 1 照合 | 幽霊・未記録・孤児・原因不明差分 | `reconciliation` |
| 2 信念の変化 | 反証条件の成立/接近/未点検/未定義 | `falsification` |
| 3 前方イベント | 翌週の決算・権利付最終日・マクロ、イベント集中度、**政策カバレッジの穴**、配当落ちの分離、月曜寄付の織り込み | `forward` |
| 4 制約 | 手取り状態・NISA残枠・資金ランウェイ・含み損の税務価値・注意予算 | `constraints` |
| 5 機会 | 銘柄別の立ち位置（値動き/前週差分/翌週日程/競合/物語混雑度）+ 過熱横断 | `holdings`, `narrative` |
| 6 事前決定 | 翌週の条件付き政策（登録コマンド付き） | 本文 + `forward` |
| 7 監査 | 累積差分（4週/13週）・執行/模型の健全性 | `cumulative_diff` |
| 8 前提と限界 | 予測前提・税務免責・系譜サマリ | 本文 |

補助材料: 推移予測（短期1ヶ月/中期6ヶ月/長期3年のレンジ + 置いた前提）、
下落シナリオ（原資産 -10/-20/-35% 時のPF評価額・レバレッジ反映）、
moomoo インサイト（FedWatch・経済/決算/配当カレンダー・アナリスト/インサイダー/
資金流向・市場ブレッドス・ARK・ニュース。OpenD経由・opt-in）。

## moomoo インサイト（OpenD / 無人自動取得）

`MOOMOO_ENABLED=on` のとき、レポート末尾に moomoo(OpenD) 由来のインサイト節が付く。
実装は `src/core/research/moomoo_insights.py`、配線は `scripts/weekly_report.py` の
`build_moomoo_section()`。

**無人ライフサイクル:** `src/data/moomoo_client.py` の `ensure_opend()` が、OpenD が
起動していなければ `OpenD.exe` を自動起動→ログイン(ポート開)を待機→取得後に
**自分が起動したものだけ**終了させる。既にユーザーが OpenD を開いている場合は
落とさない。ヘッドレス自動ログインには `OpenD.xml` の login_account + login_pwd_md5 が
必要（`scripts/set_moomoo_login.py` で1回設定。デバイス認証はGUIログイン時に済ませておく）。

**実測で確定した制約（US LV3 権限）:** 日本株は全て権限なし（yahoo に任せる）。
米指数・米オプションも権限なしでオプションIVは不可。取れないものは黙って省く
（graceful degradation）。ニュースが1件も取れないことは「材料なし」ではなく
「取得できなかった」として扱う。

## 予測モデルの前提（重要）

推移予測は**予言ではない**。置いた前提から導いた統計的レンジ（中央80%区間）。

レバレッジETFは **原資産** の前提（`UNDERLYING_ASSUMPTIONS`）を入力とし、
倍率の効果を `L·μ − (L·σ)²/2` で導出する。ボラティリティ・ドラッグはこの式に
織り込み済みで、別途引くと二重計上になる。

**同じ3xでも原資産のボラティリティで期待値がまるで違う:**

| 銘柄 | 原資産 | 原資産σ | 実効σ | 年率ドラッグ |
|:---|:---|---:|---:|---:|
| SOXL | 半導体指数 | 35% | 105% | 36.7% |
| TECL | 米テクノロジー | 26% | 78% | 20.3% |
| TQQQ | ナスダック100 | 22% | 66% | 14.5% |

前提はレポート内の表で全て開示される。前提が外れればレンジも外れる。

## 過熱 / 売られすぎ 判定

単一指標では断定しない。**RSI・%B・52週レンジ位置・200日線乖離の多数決**で、
2つ以上が同方向を指したときだけ「買われすぎ / 売られすぎ」と判定する。

算出できる指標が1つも無い場合は「中立」ではなく **「判定不能」** を返す。
過熱していないのに「正常」と誤読させないため。

## 保有内容が変わったら

`config/weekly_holdings.yaml` を更新する（MS2 RSS を使っている場合は不要 —
Excel 側が正になる）。CLAUDE.md の基準スナップショットは改変しない。
