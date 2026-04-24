@echo off
chcp 65001 >nul
title 台股量化選股系統
echo ======================================================
echo    正在啟動 台股量化選股系統 研究介面...
echo ======================================================
cd /d "%~dp0"

rem 優先使用 Python 3.11（專案套件安裝於此版本）
set "PY311=C:\Users\skker\AppData\Local\Programs\Python\Python311\python.exe"

if exist "%PY311%" (
    "%PY311%" -m streamlit run app.py
) else (
    rem 後備：使用系統預設 Python
    py -3.11 -m streamlit run app.py 2>nul || python -m streamlit run app.py
)

echo.
echo 系統已關閉。按任意鍵離開...
pause >nul
