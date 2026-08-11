#!/usr/bin/env python3
"""
TJKサイト調査ツール
実際にブラウザでサイトを開いて、フォームの構造を調べます。
初回実行時に必ずこのスクリプトを実行して、サイト構造を確認してください。
"""

import asyncio
import json
from playwright.async_api import async_playwright

BASE_URL = "https://www.tjk.gr.jp"


async def inspect():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=500)
        page = await browser.new_page(locale="ja-JP")

        print("\n=== TJK サイト構造調査 ===\n")

        # 1. トップページ
        print("1. トップページを確認します...")
        await page.goto(BASE_URL, wait_until="networkidle")
        await page.screenshot(path="inspect_top.png")

        # 2. ログインページ
        print("2. ログインページのフォーム要素を取得します...")
        await page.goto(f"{BASE_URL}/auth", wait_until="networkidle")
        await page.screenshot(path="inspect_login.png")

        # フォーム要素を全て取得
        form_elements = await page.evaluate("""() => {
            const inputs = Array.from(document.querySelectorAll('input, select, textarea, button'));
            return inputs.map(el => ({
                tag: el.tagName,
                type: el.type || '',
                name: el.name || '',
                id: el.id || '',
                placeholder: el.placeholder || '',
                className: el.className || '',
                value: el.type === 'password' ? '***' : (el.value || ''),
            }));
        }""")

        print("\n--- ログインフォームの要素 ---")
        for el in form_elements:
            print(json.dumps(el, ensure_ascii=False, indent=2))

        # フォームのaction URLを取得
        forms = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('form')).map(f => ({
                action: f.action,
                method: f.method,
                id: f.id,
                className: f.className,
            }));
        }""")
        print("\n--- フォーム情報 ---")
        for f in forms:
            print(json.dumps(f, ensure_ascii=False, indent=2))

        # 3. リンク一覧
        print("\n3. ページ内のリンクを取得します...")
        links = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('a[href]')).map(a => ({
                text: a.textContent.trim().replace(/\\s+/g, ' '),
                href: a.href,
            })).filter(a => a.text.length > 0);
        }""")

        print("\n--- ページ内リンク ---")
        for link in links:
            print(f"  [{link['text']}] → {link['href']}")

        # 結果をJSONファイルに保存
        with open("inspect_result.json", "w", encoding="utf-8") as f:
            json.dump({
                "login_form_elements": form_elements,
                "forms": forms,
                "links": links,
            }, f, ensure_ascii=False, indent=2)

        print("\n✅ 調査完了。結果を inspect_result.json に保存しました")
        print("   スクリーンショット: inspect_top.png, inspect_login.png")
        print("\nブラウザはそのまま開いています。手動で操作して構造を確認してください。")
        print("Enterキーで終了します...")
        input()

        await browser.close()


if __name__ == "__main__":
    asyncio.run(inspect())
