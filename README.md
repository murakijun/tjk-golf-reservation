# TJK 成田ビューゴルフコース 自動予約システム

TJK（東京都情報サービス産業健康保険組合）の成田ビューゴルフコース向け自動予約ツールです。
指定した時刻になると自動でログインし、フォームを入力して予約を試みます。

## ファイル構成

```
TJK/
├── main.py              # メイン予約スクリプト
├── test_login.py        # ログイン動作テスト
├── inspect_site.py      # サイト構造調査ツール（インタラクティブ）
├── inspect_headless.py  # サイト構造調査ツール（ヘッドレス）
├── config.yaml.example  # 設定ファイルのサンプル
├── requirements.txt     # Python依存パッケージ
├── setup.sh             # セットアップスクリプト
└── README.md            # このファイル
```

> **注意**: `config.yaml`（認証情報）はGitで管理されません。

## セットアップ

```bash
# 1. パッケージインストール
./setup.sh

# または手動で:
pip3 install -r requirements.txt
python3 -m playwright install chromium

# 2. 設定ファイルを作成
cp config.yaml.example config.yaml
# エディタで config.yaml を開いて認証情報を入力
```

## 使い方

### ログインテスト（まずここから）

```bash
python3 test_login.py --user 会員番号 --pass パスワード
```

ブラウザが開き、ログイン → ゴルフ予約ページへの遷移を確認できます。

### デバッグモード（予約送信なし）

```bash
python3 main.py --debug
```

フォームの入力まで行い、送信はスキップします。画面構造の確認に使います。

### 本番実行（自動予約）

```bash
python3 main.py
```

`config.yaml` の `reservation_start` で指定した時刻まで待機し、
時刻になると自動でログイン → 予約フォーム入力 → 送信します。

## 動作の流れ

```
起動
  ↓
予約開始時刻の lead_seconds 秒前にブラウザ起動
  ↓
TJK ログイン (https://www.tjk.gr.jp/login/)
  ↓
予約開始時刻まで待機（ログイン状態を維持）
  ↓
ゴルフ予約ページへ移動 (https://www.tjk.gr.jp/auth/pri-narita)
  ↓
フォーム入力（日付・時刻・人数）→ 送信
  ↓
失敗した場合は最大 max_attempts 回リトライ
  ↓
macOS通知で結果を報告
```

## 処理時間の確認

実行後に `timing_report.json` が生成されます。

```json
{
  "timestamp": "2026-08-15T10:00:03.412",
  "records": [
    {"step": "ブラウザ起動", "elapsed_s": 0.823},
    {"step": "ログインページ読み込み", "elapsed_s": 2.104},
    ...
  ],
  "total_s": 4.231
}
```

## 他のPCからの利用

```bash
# クローン
git clone https://github.com/murakijun/tjk-golf-reservation.git
cd tjk-golf-reservation

# セットアップ
./setup.sh
cp config.yaml.example config.yaml
# config.yaml に認証情報を入力

# 実行
python3 main.py
```

## 注意事項

- `config.yaml` には会員番号・パスワードを記載するため、**Gitにコミットしないでください**（`.gitignore` で除外済み）
- 予約が取れなかった場合は `screenshots/` フォルダのスクリーンショットを確認してください
- 連続リトライ回数が多い場合はサーバーに負荷をかけないよう `retry.interval_seconds` を調整してください
