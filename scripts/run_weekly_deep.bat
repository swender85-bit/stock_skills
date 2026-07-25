@echo off
REM ===================================================================
REM  週次PF深掘りレポート — 完全無人ヘッドレス実行（層1 Python + 層2 Claude）
REM
REM  scripts\weekly_deep_driver.py が
REM    1) ブリーフィングパックを生成し（Python・トークン0）
REM    2) headless `claude -p` で節ごとに執筆し（チェックポイント）
REM    3) 全節そろったら Obsidian vault へ同期する
REM
REM  終了コード 2 = 使用量上限などで中断。state が残るので、次回起動が
REM  続きから再開する（人の操作は不要）。
REM
REM  登録コマンド（管理者不要）:
REM    本番:
REM      schtasks /Create /TN "StockSkills\WeeklyDeep" /SC WEEKLY /D SAT ^
REM               /ST 07:12 /TR "C:\Users\swend\stock_skills\scripts\run_weekly_deep.bat" /F
REM    再開（中断していたときだけ動く。完了済みなら即終了する）:
REM      schtasks /Create /TN "StockSkills\WeeklyDeepResume" /SC HOURLY /MO 3 ^
REM               /ST 09:12 /TR "C:\Users\swend\stock_skills\scripts\run_weekly_deep.bat" /F
REM
REM  手動テスト:
REM    schtasks /Run /TN "StockSkills\WeeklyDeep"
REM
REM  ログ: output\weekly_deep.log （毎回追記）
REM ===================================================================

setlocal

chcp 65001 >nul

set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

REM moomoo(OpenD) 無人インサイト取得を有効化
set MOOMOO_ENABLED=on

REM 執筆モデル（opus / sonnet / fable。未指定なら driver 既定の opus）
if "%WEEKLY_DEEP_MODEL%"=="" set WEEKLY_DEEP_MODEL=opus

set REPO=C:\Users\swend\stock_skills
set LOG=%REPO%\output\weekly_deep.log

cd /d "%REPO%" || exit /b 1
if not exist "%REPO%\output" mkdir "%REPO%\output"

echo. >> "%LOG%"
echo ================================================== >> "%LOG%"
echo [%date% %time%] 週次深掘り開始 (model=%WEEKLY_DEEP_MODEL%) >> "%LOG%"

python "%REPO%\scripts\weekly_deep_driver.py" %* >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%

if %RC%==0 (
    echo [%date% %time%] 完了 >> "%LOG%"
) else if %RC%==2 (
    echo [%date% %time%] 中断（上限/失敗）— 次回起動が続きから再開します >> "%LOG%"
) else (
    echo [%date% %time%] 失敗 exit=%RC% >> "%LOG%"
)

endlocal & exit /b %RC%
