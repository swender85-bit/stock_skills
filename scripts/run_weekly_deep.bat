@echo off
REM ===================================================================
REM  Weekly deep portfolio report - unattended headless run.
REM
REM  weekly_deep_driver.py does:
REM    1) build the briefing pack (Python, zero tokens)
REM    2) write each section via headless claude, checkpointed
REM    3) sync to the Obsidian vault once every section is done
REM
REM  Exit code 2 = interrupted (usage limit etc). State is kept, so the
REM  next launch resumes where it stopped. No human action needed.
REM
REM  ------------------------------------------------------------------
REM  ASCII ONLY. DO NOT PUT JAPANESE OR BACKTICKS IN THIS FILE.
REM
REM  cmd.exe parses .bat files using the OEM code page (cp932 here),
REM  NOT UTF-8. Multi-byte comment text corrupts the line structure and
REM  text that was meant to be a comment leaks out and gets EXECUTED.
REM
REM  This actually happened: a REM line mentioning the claude CLI inside
REM  backticks was executed as a real command. The CLI answered
REM      error: unknown option
REM  and the batch died with exit 1 before writing a single log line.
REM  The weekly report never ran automatically because of this.
REM  tests/test_bat_encoding.py now guards it.
REM  ------------------------------------------------------------------
REM
REM  Register (no admin required):
REM    schtasks /Create /TN "StockSkills\WeeklyDeep" /SC WEEKLY /D SAT /ST 07:12 /TR "C:\Users\swend\stock_skills\scripts\run_weekly_deep.bat" /F
REM    schtasks /Create /TN "StockSkills\WeeklyDeepResume" /SC HOURLY /MO 3 /ST 09:12 /TR "C:\Users\swend\stock_skills\scripts\run_weekly_deep.bat" /F
REM
REM  After creating a task you MUST also set WakeToRun and
REM  StartWhenAvailable (see .claude/rules/weekly-report.md), otherwise a
REM  missed run is discarded by Windows and never retried.
REM
REM  Manual test:  schtasks /Run /TN "StockSkills\WeeklyDeep"
REM  Log:          output\weekly_deep.log  (appended every run)
REM ===================================================================

setlocal

chcp 65001 >nul

set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

REM Enable unattended moomoo (OpenD) insight collection
set MOOMOO_ENABLED=on

REM Writing model (opus / sonnet / fable). Driver default is opus.
if "%WEEKLY_DEEP_MODEL%"=="" set WEEKLY_DEEP_MODEL=opus

set REPO=C:\Users\swend\stock_skills
set LOG=%REPO%\output\weekly_deep.log

cd /d "%REPO%" || exit /b 1
if not exist "%REPO%\output" mkdir "%REPO%\output"

echo. >> "%LOG%"
echo ================================================== >> "%LOG%"
echo [%date% %time%] weekly deep start (model=%WEEKLY_DEEP_MODEL%) >> "%LOG%"

python "%REPO%\scripts\weekly_deep_driver.py" %* >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%

if %RC%==0 (
    echo [%date% %time%] done >> "%LOG%"
) else if %RC%==2 (
    echo [%date% %time%] interrupted - next launch resumes from here >> "%LOG%"
) else (
    echo [%date% %time%] failed exit=%RC% >> "%LOG%"
)

endlocal & exit /b %RC%
