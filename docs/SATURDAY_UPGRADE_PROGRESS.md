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

## S2 — 提案8（情報量比例・差分レポート） 🔄 着手

## S3 — 提案3（手取り）＋ 提案9（現金・入金） ⬜ 未着手

## S4 — 提案4（前方イベント）＋ 政策台帳の合流 ⬜ 未着手

## S5以降 ⬜ 未着手
