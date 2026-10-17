@echo off
chcp 65001 >nul
cd /d "%~dp0"
where pythonw >nul 2>nul
if not errorlevel 1 (
  start "" pythonw douyin_app.py
  exit /b 0
)
python douyin_app.py
pause
