@echo off
chcp 65001 >nul
echo ======================================
echo   TJK 自動予約システム セットアップ
echo ======================================
echo.

:: Python 確認
python --version >nul 2>&1
if errorlevel 1 (
    echo [エラー] Python が見つかりません。
    echo https://www.python.org/downloads/ からインストールしてください。
    echo インストール時に "Add Python to PATH" にチェックを入れてください。
    pause
    exit /b 1
)
echo [OK] Python が見つかりました
python --version

echo.
echo [1/2] パッケージをインストール中...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [エラー] パッケージのインストールに失敗しました
    pause
    exit /b 1
)

echo.
echo [2/2] Playwright ブラウザをインストール中...
python -m playwright install chromium
if errorlevel 1 (
    echo [エラー] ブラウザのインストールに失敗しました
    pause
    exit /b 1
)

echo.
echo ======================================
echo   セットアップ完了！
echo.
echo   次のステップ:
echo   「起動.bat」をダブルクリックして
echo   アプリを起動してください。
echo ======================================
pause
