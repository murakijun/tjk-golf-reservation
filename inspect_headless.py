#!/usr/bin/env python3
"""サイト構造をヘッドレスで調査してJSONに出力"""
import asyncio, json, time
from playwright.async_api import async_playwright

BASE_URL = "https://www.tjk.gr.jp"

async def inspect():
    result = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(locale="ja-JP")

        t0 = time.time()
        await page.goto(f"{BASE_URL}/auth", wait_until="networkidle", timeout=30000)
        result["load_time_ms"] = round((time.time() - t0) * 1000)

        result["url"] = page.url
        result["title"] = await page.title()

        # 全 input/select/button
        result["form_elements"] = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('input,select,textarea,button')).map(el => ({
                tag: el.tagName, type: el.type||'', name: el.name||'',
                id: el.id||'', placeholder: el.placeholder||'',
                className: el.className.substring(0,60), text: el.innerText?.substring(0,40)||''
            }));
        }""")

        # フォームのaction
        result["forms"] = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('form')).map(f => ({
                action: f.action, method: f.method, id: f.id
            }));
        }""")

        # リンク（予約関連）
        result["links"] = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('a[href]')).map(a => ({
                text: a.textContent.trim().replace(/\\s+/g,' ').substring(0,50),
                href: a.href
            })).filter(a => a.text.length > 0);
        }""")

        # ページ全文
        result["body_text"] = (await page.inner_text("body"))[:3000]

        await browser.close()

    with open("/Users/murakijun/Webアプリ/TJK/inspect_result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print("完了")
    return result

r = asyncio.run(inspect())
print(f"ページタイトル: {r['title']}")
print(f"URL: {r['url']}")
print(f"ロード時間: {r['load_time_ms']}ms")
print(f"フォーム要素数: {len(r['form_elements'])}")
print("\n--- フォーム要素 ---")
for el in r['form_elements']:
    print(f"  {el['tag']} type={el['type']} name={el['name']} id={el['id']} placeholder={el['placeholder']}")
print("\n--- フォーム action ---")
for f in r['forms']:
    print(f"  {f}")
print("\n--- 予約関連リンク ---")
for lnk in r['links']:
    if any(k in lnk['text'] for k in ['予約','申込','ゴルフ','スポーツ','施設','ログイン']):
        print(f"  [{lnk['text']}] {lnk['href']}")
