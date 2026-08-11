#!/bin/bash
# TJK予約システム セットアップスクリプト

echo "======================================"
echo "TJK自動予約システム セットアップ"
echo "======================================"

# Python バージョン確認
if ! command -v python3 &> /dev/null; then
    echo "❌ Python3 が見つかりません。https://www.python.org/ からインストールしてください"
    exit 1
fi
echo "✅ Python3: $(python3 --version)"

# pip で依存パッケージをインストール
echo ""
echo "📦 パッケージをインストール中..."
pip3 install -r requirements.txt

# Playwright のブラウザをインストール
echo ""
echo "🌐 Playwrightのブラウザをインストール中..."
python3 -m playwright install chromium

echo ""
echo "======================================"
echo "✅ セットアップ完了！"
echo ""
echo "次のステップ:"
echo "  1. config.yaml を開いて設定を編集してください"
echo "  2. ユーザー名・パスワードを入力"
echo "  3. 予約開始日時・プレー希望日時を設定"
echo "  4. python3 main.py で実行"
echo "======================================"
