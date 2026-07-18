# OUTPUT_MAP — スキル → 生成物 → 保存先（現状把握 / Phase 0）

作成日: 2026-07-18 / 改良指示書 v1.0 Phase 0 の成果物

## 結論サマリー

- 各スキルの出力は `src/output/*_formatter.py` が **Markdown 文字列を組み立て、`scripts/run_*.py` が標準出力（stdout）に print する** 方式。
- **ファイルとして Obsidian / iCloud に書き込むコードは存在しない。** 保存は Claude（アシスタント）が CLAUDE.md のルールに従って手動で行っている（＝「現状 output/ 止まり」ですらなく「stdout 止まり + 手動保存」）。
- → 指示書 Phase 2 の「output/ に生成 → Obsidian へコピー」は **新規実装が必要**。

## スキル → 生成物 → 保存先 対応表

| スキル | 生成ロジック | 整形モジュール | 出力先（現状） |
|:---|:---|:---|:---|
| screen-stocks | run_screen.py | screening_summary_formatter.py | stdout のみ |
| stock-report | generate_report.py | formatter.py / research_formatter.py | stdout のみ |
| market-research | run_research.py | research_formatter.py | stdout のみ |
| stock-portfolio | run_portfolio.py | portfolio_formatter / analyze / health / forecast / rebalance / simulate / review / adjust _formatter.py | stdout のみ |
| stress-test | run_stress_test.py | stress_formatter.py | stdout のみ |
| watchlist | manage_watchlist.py | （直接整形） | data/watchlists/（マスター） |
| investment-note | manage_note.py | （直接整形） | data/notes/（マスター） |
| graph-query | run_query.py | （直接整形） | stdout のみ |
| plan-execute | （オーケストレーション） | 各スキルに委譲 | stdout のみ |

## Obsidian / iCloud 保存の現状

- CLAUDE.md に「分析結果は投資記録フォルダに保存」ルールあり（2つの保存先が併記されており不整合 → 下記）。
- 実際の保存は **Claude が Write ツールで手動実行**。コードによる自動同期は無し。
- **保存先パスの不整合（要確認）**:
  - CLAUDE.md「Obsidian Vault」節: `C:\Users\swend\iCloudDrive\swender`（投資記録フォルダ）
  - CLAUDE.md「Obsidian自動保存ルール」節: `C:\Users\swend\iCloudDrive\Obsidian\swender\投資記録\`
  - メモ（memory）: 「iCloudDrive\swender が正、Obsidian\swender ではない」
  - → 正しい保存先の確定が必要（Phase 2 の前提）。

## 環境の実態（指示書の想定との差分）

| 項目 | 指示書 v1.0 の想定 | SKILL.md 内の記述 | **実際** |
|:---|:---|:---|:---|
| 作業パス | `/workspaces/stock_skills`（Linux devcontainer） | `/Users/kikuchihiroyuki/stock-skills`（Mac） | **`C:\Users\swend\stock_skills`（Windows）** |
| OS | Linux 前提 | Mac 前提 | **Windows 11** |
| Python | python3 | python3 | python / python3 both = **3.14.5** |
| シェル | bash | bash | **PowerShell 主 + Git Bash 併用** |

### 既存 settings.json の問題

- `.claude/settings.json` の PostToolUse フックは `cd /Users/kikuchihiroyuki/stock-skills`（Mac パス）を含む。
- **このパスは Windows 上に存在しないため、現状フックは無言で失敗している**（＝ .py 編集時の自動 pytest / docs チェックは今この環境では効いていない）。
- → Phase 1 のフック追加時に、この既存フックも Windows 対応に直す必要がある。

## 現状の設定ファイル

- `.claude/settings.json`: env（AGENT_TEAMS）+ PostToolUse フック（Mac パスで無効）
- `.claude/settings.local.json`: neo4j MCP 有効化
- `.mcp.json`: neo4j（bolt://localhost:7687）
- `.env.example`: あり（API キー一覧の雛形）
- `.claude/agents/`: **無し**（Phase 5 で新設）
- `.github/workflows/`: **無し**（Phase 3-B で新設）
- `scripts/hooks/pre-commit`: あり

## CLAUDE.md ダイエットについて（Phase 5）

- 現状 **116 行**。指示書 Phase 5 の目標「200 行以内」は **既に達成済み**。
- → 大規模なダイエットは不要。保有 PF 情報などスキル動作に必須の情報は残す。
