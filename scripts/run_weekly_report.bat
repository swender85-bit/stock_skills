@echo off
REM ===================================================================
REM  Weekly portfolio report (legacy thin version) - task runner.
REM
REM  Runs Saturday morning and writes a report reflecting the week's
REM  final closes into the Obsidian vault.
REM
REM  ASCII ONLY. DO NOT PUT JAPANESE OR BACKTICKS IN THIS FILE.
REM  cmd.exe parses .bat using the OEM code page (cp932 here), not UTF-8.
REM  Multi-byte comment text corrupts the line structure and comment text
REM  can leak out and get EXECUTED. See run_weekly_deep.bat for the real
REM  incident this caused. tests/test_bat_encoding.py guards it.
REM
REM  Register (no admin required):
REM    schtasks /Create /TN "StockSkills\WeeklyReport" /SC WEEKLY /D SAT /ST 07:12 /TR "C:\Users\swend\stock_skills\scripts\run_weekly_report.bat" /F
REM
REM  Manual test:  schtasks /Run /TN "StockSkills\WeeklyReport"
REM  Log:          output\weekly_report.log  (appended every run)
REM ===================================================================

setlocal

REM The Python side emits UTF-8, so switch the console to UTF-8 too.
chcp 65001 >nul

REM Keep Japanese and emoji from dying under cp932 on the Python side.
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

REM Enable unattended moomoo (OpenD) insight collection
set MOOMOO_ENABLED=on

set REPO=C:\Users\swend\stock_skills
set LOG=%REPO%\output\weekly_report.log

cd /d "%REPO%" || exit /b 1
if not exist "%REPO%\output" mkdir "%REPO%\output"

echo. >> "%LOG%"
echo ================================================== >> "%LOG%"
echo [%date% %time%] weekly report start >> "%LOG%"

python "%REPO%\scripts\weekly_report.py" >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%

if %RC%==0 (
    echo [%date% %time%] done >> "%LOG%"
) else (
    echo [%date% %time%] failed exit=%RC% >> "%LOG%"
)

endlocal & exit /b %RC%
