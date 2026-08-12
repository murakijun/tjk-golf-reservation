#!/usr/bin/env python3
"""
直接デバッグ実行スクリプト
ブラウザを開いてログインページまで移動し、
エラーがあれば詳細を表示します。
認証情報は config.yaml から読み込みます。
"""
import asyncio, sys, time, traceback
from pathlib import Path
import yaml
from playwright.async_api import async_playwright

GOLF_LOGIN_URL = "https://www5.tjk.gr.jp/TJK/login_wing.php"
SCREENSHOTS    = Path(__file__).parent / "screenshots"
SCREENSHOTS.mkdir(exist_ok=True)

def load_config():
    p = Path(__file__).parent / "config.yaml"
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)

async def run():
    cfg = load_config()
    uid = cfg["login"]["username"].replace(" ", "")
    pwd = cfg["login"]["password"]

    if uid == "your_username":
        print("❌ config.yaml のユーザーIDを設定してください")
        sys.exit(1)

    # ユーザーID分割
    if "-" in uid:
        id1, id2 = uid.split("-", 1)
    else:
        id1, id2 = uid[:4], uid[4:]
    id1 = id1.zfill(4)
    id2 = id2.zfill(6)

    print(f"\n=== TJK ログインデバッグ ===")
    print(f"  ログインURL: {GOLF_LOGIN_URL}")
    print(f"  ユーザーID : {id1}-{id2}")
    print(f"  パスワード : {'*' * len(pwd)}\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=300)
        page    = await browser.new_page(locale="ja-JP")

        # ── Step 1: ページ読み込み ──
        print("[1/5] ログインページを開きます...")
        t0 = time.perf_counter()
        try:
            await page.goto(GOLF_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
            print(f"  ✅ 読み込み完了 ({time.perf_counter()-t0:.2f}s): {page.url}")
        except Exception as e:
            print(f"  ❌ ページ読み込みエラー: {e}")
            traceback.print_exc()
            await browser.close()
            return

        shot = SCREENSHOTS / "debug_01_login.png"
        await page.screenshot(path=str(shot))
        print(f"  📸 {shot}")

        # ── Step 2: フィールド確認 ──
        print("\n[2/5] フォーム要素を確認...")
        elements = await page.evaluate("""() =>
            Array.from(document.querySelectorAll("input,select")).map(e => ({
                name: e.name, type: e.type, id: e.id,
                visible: e.offsetParent !== null
            }))
        """)
        for el in elements:
            if el["name"]:
                print(f"  input: name={el['name']} type={el['type']} visible={el['visible']}")

        # ── Step 3: フィールド入力 ──
        print("\n[3/5] ユーザーIDとパスワードを入力...")
        try:
            await page.wait_for_selector('input[name="login_id1"]', timeout=5000)
            await page.fill('input[name="login_id1"]', id1)
            await page.fill('input[name="login_id2"]', id2)
            await page.fill('input[name="pass"]', pwd)
            print(f"  ✅ 入力完了: {id1}-{id2}")
        except Exception as e:
            print(f"  ❌ 入力エラー: {e}")
            traceback.print_exc()
            await browser.close()
            return

        shot2 = SCREENSHOTS / "debug_02_filled.png"
        await page.screenshot(path=str(shot2))
        print(f"  📸 {shot2}")

        # ── Step 4: ログイン送信 ──
        print("\n[4/5] ログイン送信 (check() を呼び出し)...")
        try:
            # check()が呼ばれると内部でdocument.fm.submit()が実行される
            # ナビゲーションを待ちながら実行
            async with page.expect_navigation(timeout=15000):
                await page.evaluate("check()")
            print(f"  ✅ 送信完了: {page.url}")
        except Exception as e:
            print(f"  ⚠️  navigate待ちエラー（続行）: {e}")
            # タイムアウトしても続行（ページが既に遷移している場合がある）
            await asyncio.sleep(1)
            print(f"  現在のURL: {page.url}")

        await asyncio.sleep(1)
        shot3 = SCREENSHOTS / "debug_03_after_login.png"
        await page.screenshot(path=str(shot3))
        print(f"  📸 {shot3}")

        # ── Step 5: ログイン結果確認 ──
        print("\n[5/5] ログイン結果を確認...")
        body = await page.inner_text("body")
        url  = page.url

        error_kw   = ["パスワードが違", "ＩＤが違", "IDが違", "ログインできません", "認証エラー", "入力してください"]
        success_kw = ["ログアウト", "メニュー", "予約", "マイページ", "TOP"]

        if any(k in body for k in error_kw):
            print(f"  ❌ ログイン失敗 — エラーメッセージを検出")
            # エラーメッセージを抽出
            for line in body.split("\n"):
                for k in error_kw:
                    if k in line:
                        print(f"     → {line.strip()}")
        elif "login_wing.php" in url:
            print(f"  ⚠️  ログインページに留まっています → 認証情報を確認してください")
            print(f"  ページ内容（最初の300文字）:")
            print(f"  {body[:300]}")
        elif any(k in body for k in success_kw):
            print(f"  ✅ ログイン成功！")
            print(f"  遷移先: {url}")
            print(f"  ページ内容（最初の400文字）:")
            print(f"  {body[:400]}")
        else:
            print(f"  ⚠️  結果不明")
            print(f"  URL: {url}")
            print(f"  ページ内容（最初の400文字）:")
            print(f"  {body[:400]}")

        print(f"\n合計時間: {time.perf_counter()-t0:.2f}s")
        print(f"\nスクリーンショット保存先: {SCREENSHOTS}")
        print("Enterキーでブラウザを閉じます...")
        input()
        await browser.close()

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(run())
