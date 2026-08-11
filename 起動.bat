@echo off
chcp 65001 >nul
echo ======================================
echo   TJK ゴルフ自動予約アプリ
echo   ブラウザが自動で開きます...
echo   終了するにはこのウィンドウを閉じてください
echo ======================================
echo.

cd /d "%~dp0"

:: Python 確認
python --version >nul 2>&1
if errorlevel 1 (
    echo [エラー] Python が見つかりません。
    echo setup.bat を先に実行してください。
    pause
    exit /b 1
)

:: Playwright 確認
python -c "import playwright" >nul 2>&1
if errorlevel 1 (
    echo [エラー] セットアップが完了していません。
    echo setup.bat を先に実行してください。
    pause
    exit /b 1
)

python app.py
pause
