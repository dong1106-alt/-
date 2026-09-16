@echo off
chcp 65001 >nul
cd /d %~dp0
set PYTHONUTF8=1
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe scripts\quality_gate.py --full
) else (
  python scripts\quality_gate.py --full
)
pause

