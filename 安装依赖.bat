@echo off
chcp 65001 >nul
cd /d %~dp0
echo [1/2] ??????...
if not exist .venv (python -m venv .venv)
echo [2/2] ????(???2-5??)...
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
echo ????? ??.bat ?????
pause

