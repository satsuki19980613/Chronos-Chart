@echo off
chcp 65001 >nul
setlocal
rem ============================================================
rem  Chronos Chart 起動用バッチ
rem    ダブルクリック            : アプリを起動（コンソールは閉じる）
rem    start.bat --debug          : コンソールと開発者ツール付きで起動
rem ============================================================
cd /d "%~dp0"

rem ---- 使用する Python を決める（.venv があれば優先） ----
if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
    set "PYW=.venv\Scripts\pythonw.exe"
    goto :check_version
)
where python >nul 2>nul
if errorlevel 1 (
    echo [エラー] Python が見つかりません。Python 3.10 以上をインストールしてください。
    echo         https://www.python.org/downloads/
    pause
    exit /b 1
)
set "PY=python"
set "PYW=pythonw"
where pythonw >nul 2>nul
if errorlevel 1 set "PYW=python"

:check_version
"%PY%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
    echo [エラー] Python 3.10 以上が必要です。
    "%PY%" --version
    pause
    exit /b 1
)

rem ---- 必要なライブラリがなければインストール ----
"%PY%" -c "import webview, yfinance, pandas, numpy" >nul 2>nul
if errorlevel 1 (
    echo 必要なライブラリをインストールしています...
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [エラー] ライブラリのインストールに失敗しました。
        pause
        exit /b 1
    )
)

rem ---- 起動 ----
if /i "%~1"=="--debug" (
    "%PY%" main.py --debug
    pause
    exit /b 0
)
start "" "%PYW%" main.py
exit /b 0
