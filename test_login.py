#!/usr/bin/env python3
"""
ログイン動作テスト（認証情報が正しいか確認するためのスクリプト）
使い方:
  python3 test_login.py --user 会員番号 --pass パスワード
"""
import asyncio, sys, time, argparse
from playwright.async_api import async_playwright

BASE_URL  = "https://www.tjk.gr.jp"
LOGIN_URL = f"{BASE_URL}/login/"

async def test_login(username: str, password: str):
    t0 = time.perf_counter()
    print(f"\n=== TJK ログインテスト ===")
    print(f"ユーザー名: {username}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=200)
        page = await browser.new_page(locale="ja-JP")

        print(f"[{time.perf_counter()-t0:.2f}s] ログインページを開いています...")
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
        print(f"[{time.perf_counter()-t0:.2f}s] ページ読み込み完了: {page.url}")

        await page.fill('input[name="account"]', username)
        await page.fill('input[name="password"]', password)
        print(f"[{time.perf_counter()-t0:.2f}s] 認証情報を入力しました")

        await page.click('input[type="submit"]')
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
        print(f"[{time.perf_counter()-t0:.2f}s] ログイン後URL: {page.url}")

        body = await page.inner_text("body")
        if "ログアウト" in body or "マイページ" in body:
            print("✅ ログイン成功！")
        elif "パスワード" in body and "違" in body:
            print("❌ 認証エラー：パスワードが違います")
        elif "/login/" in page.url:
            print("❌ ログイン失敗（ログインページに留まっています）")
        else:
            print(f"⚠️  結果不明: {page.url}")

        # ゴルフ予約ページへ移動テスト
        print(f"\n[{time.perf_counter()-t0:.2f}s] ゴルフ予約ページへ移動テスト...")
        await page.goto(f"{BASE_URL}/auth/pri-narita", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(1)
        print(f"[{time.perf_counter()-t0:.2f}s] URL: {page.url}")
        print(f"タイトル: {await page.title()}")

        body2 = await page.inner_text("body")
        print("\nページ内容（最初の500文字）:")
        print(body2[:500])

        print(f"\n合計時間: {time.perf_counter()-t0:.3f}s")
        print("\nEnterキーでブラウザを閉じます...")
        input()
        await browser.close()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", required=True, help="TJKユーザー名（会員番号）")
    parser.add_argument("--pass", dest="password", required=True, help="パスワード")
    args = parser.parse_args()
    asyncio.run(test_login(args.user, args.password))

if __name__ == "__main__":
    main()
