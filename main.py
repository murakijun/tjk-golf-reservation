#!/usr/bin/env python3
"""
TJK 成田ビューゴルフコース 自動予約システム
使い方: python3 main.py [--debug]

デバッグモード: python3 main.py --debug
  → ログインのみ実行し予約は送信しない（フォーム確認用）
"""

import asyncio
import logging
import sys
import time
import subprocess
import json
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

BASE_URL        = "https://www.tjk.gr.jp"
LOGIN_URL       = f"{BASE_URL}/login/"
LOGIN_ACTION    = f"{BASE_URL}/assist/ready/dologin"
GOLF_AUTH_URL   = f"{BASE_URL}/auth/pri-narita"    # ゴルフ予約システム（ログイン必須）
SCREENSHOTS_DIR = Path(__file__).parent / "screenshots"

# ─────────────────────────────────────────
# タイマーユーティリティ
# ─────────────────────────────────────────

class Timer:
    """処理時間計測クラス"""
    def __init__(self, log: logging.Logger):
        self.log = log
        self._records: list[tuple[str, float]] = []
        self._start = time.perf_counter()

    def lap(self, label: str) -> float:
        elapsed = time.perf_counter() - self._start
        self._records.append((label, elapsed))
        return elapsed

    def mark(self, label: str):
        t = self.lap(label)
        self.log.info(f"  ⏱  [{label}] {t:.3f}s")

    def summary(self):
        self.log.info("=" * 50)
        self.log.info("📊 処理時間サマリー")
        self.log.info("=" * 50)
        prev = 0.0
        for label, t in self._records:
            diff = t - prev
            self.log.info(f"  {label:<30} {diff:6.3f}s  (累計 {t:.3f}s)")
            prev = t
        total = self._records[-1][1] if self._records else 0
        self.log.info(f"  {'合計':<30} {total:.3f}s")
        self.log.info("=" * 50)

        # JSON保存
        report = {
            "timestamp": datetime.now().isoformat(),
            "records": [{"step": l, "elapsed_s": round(t, 4)} for l, t in self._records],
            "total_s": round(total, 4),
        }
        report_path = Path(__file__).parent / "timing_report.json"
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        self.log.info(f"📄 タイミングレポート保存: {report_path}")


# ─────────────────────────────────────────
# 設定読み込み
# ─────────────────────────────────────────

def load_config() -> dict:
    config_path = Path(__file__).parent / "config.yaml"
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def setup_logging(cfg: dict) -> logging.Logger:
    log_cfg = cfg.get("logging", {})
    level = getattr(logging, log_cfg.get("level", "INFO"))
    log_file = Path(__file__).parent / log_cfg.get("file", "reservation.log")

    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)

    logger = logging.getLogger("TJK")
    logger.setLevel(level)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


# ─────────────────────────────────────────
# macOS 通知
# ─────────────────────────────────────────

def notify(title: str, message: str, sound: bool = True):
    try:
        script = f'display notification "{message}" with title "{title}"'
        if sound:
            script += ' sound name "default"'
        subprocess.run(["osascript", "-e", script], check=False)
    except Exception:
        pass


def screenshot(page, name: str):
    SCREENSHOTS_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%H%M%S")
    path = SCREENSHOTS_DIR / f"{ts}_{name}.png"
    return page.screenshot(path=str(path))


# ─────────────────────────────────────────
# 予約開始時刻まで待機
# ─────────────────────────────────────────

def wait_until_start(cfg: dict, log: logging.Logger):
    rs = cfg["reservation_start"]
    start_dt = datetime.fromisoformat(f"{rs['date']}T{rs['time']}")
    lead = int(rs.get("lead_seconds", 3))
    wake_dt = start_dt - timedelta(seconds=lead)

    now = datetime.now()
    if now >= start_dt:
        log.info("予約開始時刻は過去のため、すぐに実行します")
        return

    wait_sec = (wake_dt - now).total_seconds()
    log.info(f"予約開始時刻: {start_dt.strftime('%Y/%m/%d %H:%M:%S')}")
    log.info(f"  → ブラウザ起動: {wake_dt.strftime('%H:%M:%S')} ({lead}秒前)")
    log.info(f"  → 待機時間: {int(wait_sec//3600)}時間 {int((wait_sec%3600)//60)}分 {int(wait_sec%60)}秒")

    # 長時間待機（60秒ごとに残り時間を出力）
    while True:
        remaining = (wake_dt - datetime.now()).total_seconds()
        if remaining <= 1:
            break
        sleep_time = min(remaining - 1, 60)
        if sleep_time > 0:
            if remaining > 120:
                log.info(f"  残り {int(remaining//60)} 分...")
            time.sleep(sleep_time)

    # 精密待機（0.1ms精度）
    while datetime.now() < wake_dt:
        time.sleep(0.0001)

    log.info("🚀 起動！ブラウザを開始します")


# ─────────────────────────────────────────
# ログイン
# ─────────────────────────────────────────

async def login(page, cfg: dict, log: logging.Logger, timer: Timer) -> bool:
    login_cfg = cfg["login"]
    timeout = cfg.get("browser", {}).get("timeout", 30000)

    log.info(f"ログインページへ移動: {LOGIN_URL}")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=timeout)
    timer.mark("ログインページ読み込み")

    await screenshot(page, "01_login_page")

    # ユーザー名 (name="account")
    try:
        await page.wait_for_selector('input[name="account"]', timeout=5000)
        await page.fill('input[name="account"]', login_cfg["username"])
    except Exception as e:
        log.error(f"ユーザー名フィールドが見つかりません: {e}")
        await screenshot(page, "err_no_username")
        return False

    # パスワード (name="password", type=text)
    try:
        await page.fill('input[name="password"]', login_cfg["password"])
    except Exception as e:
        log.error(f"パスワードフィールドが見つかりません: {e}")
        await screenshot(page, "err_no_password")
        return False

    timer.mark("フォーム入力完了")

    # ログインボタン (input[type="submit"])
    await page.click('input[type="submit"]')
    timer.mark("ログインボタンクリック")

    # ログイン後のページを待機
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=timeout)
        await asyncio.sleep(0.5)
    except PlaywrightTimeout:
        pass

    current_url = page.url
    log.info(f"ログイン後URL: {current_url}")
    timer.mark("ログイン完了")

    await screenshot(page, "02_after_login")

    # ログイン成否判定
    content = await page.content()
    if "ログアウト" in content or "マイページ" in content or "auth" in current_url:
        log.info("✅ ログイン成功")
        return True
    elif "パスワードが違います" in content or "ユーザー名が違います" in content or "/login/" in current_url:
        log.error("❌ 認証エラー：ユーザー名またはパスワードを確認してください")
        await screenshot(page, "err_login_failed")
        return False
    else:
        log.warning(f"⚠️  ログイン状態不明 (URL: {current_url})")
        return True  # セッションクッキーがあれば続行を試みる


# ─────────────────────────────────────────
# ゴルフ予約システムへ移動
# ─────────────────────────────────────────

async def navigate_to_golf(page, cfg: dict, log: logging.Logger, timer: Timer) -> bool:
    timeout = cfg.get("browser", {}).get("timeout", 30000)

    log.info(f"ゴルフ予約システムへ移動: {GOLF_AUTH_URL}")
    await page.goto(GOLF_AUTH_URL, wait_until="domcontentloaded", timeout=timeout)
    await asyncio.sleep(1)
    timer.mark("ゴルフ予約ページ読み込み")

    await screenshot(page, "03_golf_reservation_page")

    current_url = page.url
    log.info(f"現在のURL: {current_url}")

    # 「同意する」ボタンがあれば同意
    agree_selectors = [
        'button:has-text("同意する")',
        'input[value="同意する"]',
        'a:has-text("同意する")',
    ]
    for sel in agree_selectors:
        try:
            elem = await page.query_selector(sel)
            if elem and await elem.is_visible():
                log.info("個人情報同意ボタンをクリック")
                await elem.click()
                await page.wait_for_load_state("domcontentloaded", timeout=timeout)
                timer.mark("個人情報同意")
                await screenshot(page, "04_after_agree")
                break
        except Exception:
            continue

    # ページ内容を確認
    content = await page.content()
    body_text = await page.inner_text("body")
    log.info(f"ページタイトル: {await page.title()}")

    # フォーム要素を調査してログに出力
    elements = await page.evaluate("""() => {
        return Array.from(document.querySelectorAll('input,select,textarea,button')).map(el => ({
            tag: el.tagName, type: el.type||'', name: el.name||'',
            id: el.id||'', value: el.value?.substring(0,30)||''
        }));
    }""")
    log.info(f"フォーム要素数: {len(elements)}")
    for el in elements[:20]:
        log.info(f"  {el['tag']} type={el['type']} name={el['name']} id={el['id']}")

    # 保存
    debug_path = Path(__file__).parent / "debug_golf_page.txt"
    with open(debug_path, "w", encoding="utf-8") as f:
        f.write(f"URL: {current_url}\n")
        f.write(f"Title: {await page.title()}\n\n")
        f.write(body_text[:5000])
    log.info(f"ページ内容を保存: {debug_path}")

    return True


# ─────────────────────────────────────────
# 予約フォーム入力 + 送信
# ─────────────────────────────────────────

async def fill_and_submit(page, cfg: dict, log: logging.Logger, timer: Timer,
                          debug_mode: bool = False) -> bool:
    res = cfg["reservation"]
    timeout = cfg.get("browser", {}).get("timeout", 30000)

    log.info("予約フォームを入力します")
    log.info(f"  プレー日: {res['play_date']}")
    log.info(f"  希望時刻: {res['play_time']}")
    log.info(f"  人数: {res['num_players']}名")

    # ─── 日付選択 ───
    date_filled = False
    for sel in ['input[name*="date"]', 'input[type="date"]', '#playDate',
                'select[name*="date"]', 'input[name*="play"]']:
        try:
            elem = await page.query_selector(sel)
            if elem and await elem.is_visible():
                tag = await elem.evaluate("e => e.tagName.toLowerCase()")
                if tag == "select":
                    await page.select_option(sel, res["play_date"])
                else:
                    await page.fill(sel, res["play_date"])
                log.info(f"  プレー日設定: {res['play_date']} ({sel})")
                date_filled = True
                break
        except Exception:
            continue

    # ─── 時刻選択 ───
    for sel in ['select[name*="time"]', 'select[name*="start"]', '#startTime',
                'input[name*="time"]']:
        try:
            elem = await page.query_selector(sel)
            if elem and await elem.is_visible():
                tag = await elem.evaluate("e => e.tagName.toLowerCase()")
                if tag == "select":
                    # セレクトボックスの選択肢に近いものを選ぶ
                    options = await elem.evaluate("""e => Array.from(e.options).map(o => ({
                        value: o.value, text: o.text
                    }))""")
                    log.info(f"  時刻の選択肢: {options}")
                    await page.select_option(sel, res["play_time"])
                else:
                    await page.fill(sel, res["play_time"])
                log.info(f"  時刻設定: {res['play_time']}")
                break
        except Exception:
            continue

    # ─── 人数選択 ───
    for sel in ['select[name*="player"]', 'select[name*="num"]',
                'select[name*="count"]', 'select[name*="人数"]']:
        try:
            elem = await page.query_selector(sel)
            if elem and await elem.is_visible():
                await page.select_option(sel, str(res["num_players"]))
                log.info(f"  人数設定: {res['num_players']}名")
                break
        except Exception:
            continue

    timer.mark("フォーム入力完了")
    await screenshot(page, "05_form_filled")

    if debug_mode:
        log.info("🔍 デバッグモード: 送信をスキップします")
        return True

    # ─── 空き状況検索（まず空きを確認） ───
    search_selectors = [
        'button:has-text("検索")',
        'input[value="検索"]',
        'button:has-text("照会")',
        'button[type="submit"]',
        'input[type="submit"]',
    ]
    for sel in search_selectors:
        try:
            elem = await page.query_selector(sel)
            if elem and await elem.is_visible():
                log.info(f"検索ボタンをクリック: {sel}")
                await elem.click()
                await page.wait_for_load_state("domcontentloaded", timeout=timeout)
                timer.mark("空き状況検索")
                await screenshot(page, "06_search_result")
                break
        except Exception:
            continue

    # ─── 希望スロットを選択 ───
    target_time = res["play_time"]
    slot_selectors = [
        f'a:has-text("{target_time}")',
        f'button:has-text("{target_time}")',
        f'input[value*="{target_time}"]',
        'a:has-text("予約")',
        '.available:first-child',
        '.open:first-child',
    ]
    slot_clicked = False
    for sel in slot_selectors:
        try:
            elem = await page.query_selector(sel)
            if elem and await elem.is_visible():
                log.info(f"スロット選択: {sel}")
                await elem.click()
                await page.wait_for_load_state("domcontentloaded", timeout=timeout)
                timer.mark("スロット選択")
                await screenshot(page, "07_slot_selected")
                slot_clicked = True
                break
        except Exception:
            continue

    if not slot_clicked:
        log.warning("⚠️  スロット選択ボタンが見つかりません → スクリーンショットを確認してください")

    # ─── 最終確認・送信 ───
    confirm_selectors = [
        'button:has-text("予約する")',
        'button:has-text("申込む")',
        'button:has-text("確定")',
        'input[value="予約する"]',
        'input[value="申込む"]',
        'button[type="submit"]',
        'input[type="submit"]',
    ]
    for sel in confirm_selectors:
        try:
            elem = await page.query_selector(sel)
            if elem and await elem.is_visible():
                log.info(f"予約送信ボタンをクリック: {sel}")
                await elem.click()
                await page.wait_for_load_state("domcontentloaded", timeout=timeout)
                timer.mark("予約送信")
                await screenshot(page, "08_reservation_result")
                break
        except Exception:
            continue

    # ─── 結果判定 ───
    content = await page.content()
    body_text = await page.inner_text("body")

    success_kw = ["完了", "受付", "予約番号", "申込番号", "ありがとう", "受付番号"]
    fail_kw    = ["エラー", "失敗", "満席", "取れません", "空きがありません", "error"]

    if any(k in body_text for k in success_kw):
        log.info("🎉 予約完了！")
        # 予約番号を抽出して表示
        for line in body_text.split("\n"):
            if any(k in line for k in ["予約番号", "申込番号", "受付番号"]):
                log.info(f"  → {line.strip()}")
        return True
    elif any(k in body_text for k in fail_kw):
        log.error("❌ 予約失敗")
        return False
    else:
        log.warning(f"⚠️  結果不明 → screenshots/ フォルダを確認してください")
        return False


# ─────────────────────────────────────────
# メイン予約ループ（リトライ付き）
# ─────────────────────────────────────────

async def reservation_loop(page, cfg: dict, log: logging.Logger, timer: Timer,
                           debug_mode: bool = False) -> bool:
    retry_cfg = cfg.get("retry", {})
    max_attempts = retry_cfg.get("max_attempts", 10)
    interval    = retry_cfg.get("interval_seconds", 0.5)

    # ─── 予約開始時刻まで（ログイン済みで）待機 ───
    rs = cfg["reservation_start"]
    start_dt = datetime.fromisoformat(f"{rs['date']}T{rs['time']}")
    now = datetime.now()

    if now < start_dt:
        remaining = (start_dt - now).total_seconds()
        log.info(f"予約開始まで {remaining:.1f}秒... ログイン状態を維持します")
        while datetime.now() < start_dt:
            await asyncio.sleep(0.05)
        log.info("⏰ 予約開始時刻になりました！")
        timer.mark("予約開始時刻到達")

    log.info(f"予約を開始します（最大 {max_attempts} 回試行）")
    success = False

    for attempt in range(1, max_attempts + 1):
        log.info(f"--- 試行 {attempt}/{max_attempts} ---")
        t_attempt = time.perf_counter()

        try:
            nav_ok = await navigate_to_golf(page, cfg, log, timer)
            if not nav_ok:
                continue
            result = await fill_and_submit(page, cfg, log, timer, debug_mode)
            elapsed = time.perf_counter() - t_attempt
            log.info(f"試行 {attempt} 所要時間: {elapsed:.3f}s")

            if result:
                success = True
                break

        except Exception as e:
            log.error(f"例外発生: {e}", exc_info=True)
            await screenshot(page, f"err_attempt_{attempt}")

        if attempt < max_attempts:
            log.info(f"{interval}秒後にリトライ...")
            await asyncio.sleep(interval)

    return success


# ─────────────────────────────────────────
# エントリポイント
# ─────────────────────────────────────────

async def run(cfg: dict, log: logging.Logger, debug_mode: bool = False):
    timer = Timer(log)
    browser_cfg = cfg.get("browser", {})
    notif_cfg   = cfg.get("notification", {})

    SCREENSHOTS_DIR.mkdir(exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=browser_cfg.get("headless", False),
            slow_mo=browser_cfg.get("slow_mo", 50),
        )
        context = await browser.new_context(
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        timer.mark("ブラウザ起動")

        # ─── ログイン ───
        logged_in = await login(page, cfg, log, timer)
        if not logged_in:
            log.error("ログインできませんでした。終了します")
            notify("TJK予約", "ログイン失敗", notif_cfg.get("sound", True))
            await browser.close()
            timer.summary()
            return

        # ─── 予約実行 ───
        success = await reservation_loop(page, cfg, log, timer, debug_mode)

        # ─── 通知 ───
        if notif_cfg.get("enabled"):
            if success:
                notify("TJK予約", "予約が完了しました！", notif_cfg.get("sound", True))
            else:
                notify("TJK予約", "予約に失敗しました", notif_cfg.get("sound", True))

        timer.summary()

        if debug_mode or not browser_cfg.get("headless", False):
            log.info("Enterキーでブラウザを閉じます...")
            input()

        await browser.close()


def main():
    debug_mode = "--debug" in sys.argv

    cfg = load_config()
    log = setup_logging(cfg)

    log.info("=" * 50)
    log.info("TJK 成田ビューゴルフコース 自動予約システム")
    if debug_mode:
        log.info("  [デバッグモード] 予約送信はスキップします")
    log.info("=" * 50)

    if cfg["login"]["username"] == "your_username":
        log.error("config.yaml のユーザー名・パスワードを設定してください")
        sys.exit(1)

    # 待機（デバッグモードは即時実行）
    if not debug_mode:
        wait_until_start(cfg, log)

    asyncio.run(run(cfg, log, debug_mode))


if __name__ == "__main__":
    main()
