import asyncio, json, time
from playwright.async_api import async_playwright

async def inspect_narita():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(locale="ja-JP")
        # domcontentloadedで待機（networkidleだとタイムアウトの可能性）
        await page.goto("https://www.tjk.gr.jp/services/facilities/narita",
                        wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)

        links = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('a[href]')).map(a => ({
                text: a.textContent.trim().replace(/\\s+/g,' ').substring(0,80),
                href: a.href
            })).filter(a => a.text.length > 0);
        }""")

        body = (await page.inner_text("body"))[:4000]
        await browser.close()
        return links, body

links, body = asyncio.run(inspect_narita())
print("=== 成田施設ページ リンク ===")
for l in links:
    print(f"  [{l['text']}] {l['href']}")
print()
print("=== ページテキスト（抜粋）===")
print(body[:2000])
