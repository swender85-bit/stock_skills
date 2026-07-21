@echo off
REM ===================================================================
REM  週次ポートフォリオ分析レポート — Windows タスクスケジューラ用ランナー
REM
REM  毎週土曜の朝に実行し、その週の最終終値を反映したレポートを
REM  Obsidian vault (投資記録) に出力する。
REM
REM  登録コマンド（管理者不要）:
REM    schtasks /Create /TN "StockSkills\WeeklyReport" /SC WEEKLY /D SAT 
REM             /ST 07:12 /TR "C:\Users\swend\stock_skills\scripts\run_weekly_report.bat" /F
REM
REM  手動テスト:
REM    schtasks /Run /TN "StockSkills\WeeklyReport"
REM
REM  ログ: output\weekly_report.log （毎回追記）
REM ===================================================================

setlocal

REM このバッチ自身が UTF-8 で書かれているため、コンソールも UTF-8 にする。
REM これを入れないと echo の日本語がログ内で文字化けする。
chcp 65001 >nul

REM 日本語・絵文字が cp932 で落ちないようにする
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

set REPO=C:\Users\swend\stock_skills
set LOG=%REPO%\output\weekly_report.log

cd /d "%REPO%" || exit /b 1
if not exist "%REPO%\output" mkdir "%REPO%\output"

echo. >> "%LOG%"
echo ================================================== >> "%LOG%"
echo [%date% %time%] 週次レポート開始 >> "%LOG%"

python "%REPO%\scripts\weekly_report.py" >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%

if %RC%==0 (
    echo [%date% %time%] 完了 >> "%LOG%"
) else (
    echo [%date% %time%] 失敗 exit=%RC% >> "%LOG%"
)

endlocal & exit /b %RC%
