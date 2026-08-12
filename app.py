#!/usr/bin/env python3
"""
TJK 成田ビューゴルフコース 自動予約 Web アプリ
起動: python3 app.py
ブラウザ: http://localhost:5050
"""

import asyncio
import json
import platform
import queue
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, send_from_directory

# ─── Playwright 予約ロジックをインポート ───
import sys
sys.path.insert(0, str(Path(__file__).parent))

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

BASE_URL      = "https://www.tjk.gr.jp"
LOGIN_URL     = f"{BASE_URL}/login/"
GOLF_AUTH_URL = f"{BASE_URL}/auth/pri-narita"
SCREENSHOTS   = Path(__file__).parent / "screenshots"
SCREENSHOTS.mkdir(exist_ok=True)

# ─────────────────────────────────────────
# グローバル状態
# ─────────────────────────────────────────

log_queue: queue.Queue = queue.Queue()

state = {
    "status":  "idle",     # idle / waiting / running / success / failed / cancelled
    "message": "待機中",
    "started_at": None,
    "attempt": 0,
    "max_attempts": 10,
    "screenshots": [],
}

_stop_event = threading.Event()
_runner_thread = None  # type: threading.Thread | None

# ─────────────────────────────────────────
# Flask アプリ
# ─────────────────────────────────────────

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/screenshots/<path:filename>")
def serve_screenshot(filename):
    return send_from_directory(SCREENSHOTS, filename)


# ─── SSE ストリーム ───
@app.route("/stream")
def stream():
    def generate():
        while True:
            try:
                msg = log_queue.get(timeout=20)
                yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
            except queue.Empty:
                yield f"data: {json.dumps({'type':'ping'})}\n\n"
    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─── 状態取得 ───
@app.route("/api/state")
def get_state():
    return jsonify(state)


# ─── 予約開始 ───
@app.route("/api/start", methods=["POST"])
def start():
    global _runner_thread, _stop_event

    if state["status"] in ("waiting", "running"):
        return jsonify({"ok": False, "error": "既に実行中です"}), 400

    data = request.json or {}

    cfg = {
        "login": {
            "username": data.get("username", ""),
            "password": data.get("password", ""),
        },
        "reservation_start": {
            "date": data.get("start_date", ""),
            "time": data.get("start_time", "10:00") + ":00",
            "lead_seconds": int(data.get("lead_seconds", 3)),
        },
        "reservation": {
            "play_date":   data.get("play_date", ""),
            "play_time":   data.get("play_time", ""),
            "num_players": int(data.get("num_players", 4)),
        },
        "browser": {
            "headless": data.get("headless", False),
            "slow_mo":  50,
            "timeout":  30000,
        },
        "retry": {
            "max_attempts":    int(data.get("max_attempts", 10)),
            "interval_seconds": float(data.get("interval_seconds", 0.5)),
        },
    }

    debug_mode = bool(data.get("debug_mode", False))

    _stop_event = threading.Event()
    state.update({
        "status":      "waiting",
        "message":     "テスト実行中..." if debug_mode else "予約開始時刻まで待機中...",
        "started_at":  datetime.now().isoformat(),
        "attempt":     0,
        "max_attempts": cfg["retry"]["max_attempts"],
        "screenshots": [],
        "debug_mode":  debug_mode,
    })
    _push_log("info", "🔍 テスト実行（送信なし）" if debug_mode else "🚀 予約システムを起動しました")

    def runner():
        # Windows では ProactorEventLoop が必要（スレッド内 asyncio 用）
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_reservation_main(cfg, _stop_event, debug_mode))
        except Exception as e:
            _push_log("error", f"致命的エラー: {e}")
            state["status"]  = "failed"
            state["message"] = "エラーが発生しました"
        finally:
            loop.close()

    _runner_thread = threading.Thread(target=runner, daemon=True)
    _runner_thread.start()

    return jsonify({"ok": True})


# ─── 停止 ───
@app.route("/api/stop", methods=["POST"])
def stop():
    _stop_event.set()
    state["status"]  = "cancelled"
    state["message"] = "キャンセルしました"
    _push_log("warn", "⛔ 予約をキャンセルしました")
    return jsonify({"ok": True})


# ─────────────────────────────────────────
# ログ送信ヘルパー
# ─────────────────────────────────────────

def _push_log(level: str, message: str, extra=None):
    payload = {
        "type":      "log",
        "level":     level,
        "message":   message,
        "time":      datetime.now().strftime("%H:%M:%S.%f")[:-3],
    }
    if extra:
        payload.update(extra)
    log_queue.put(payload)


def _push_screenshot(filename: str):
    state["screenshots"].append(filename)
    log_queue.put({
        "type":     "screenshot",
        "filename": filename,
        "time":     datetime.now().strftime("%H:%M:%S"),
    })


def _push_state():
    log_queue.put({"type": "state", "state": dict(state)})


# ─────────────────────────────────────────
# Playwright 予約処理
# ─────────────────────────────────────────

async def _take_screenshot(page, name: str):
    ts   = datetime.now().strftime("%H%M%S")
    fname = f"{ts}_{name}.png"
    fpath = SCREENSHOTS / fname
    await page.screenshot(path=str(fpath))
    _push_screenshot(fname)
    return fname


async def _reservation_main(cfg: dict, stop_ev: threading.Event, debug_mode: bool = False):
    t_total = time.perf_counter()

    # ─── 待機 ───
    rs       = cfg["reservation_start"]
    start_dt = datetime.fromisoformat(f"{rs['date']}T{rs['time']}")
    lead     = int(rs.get("lead_seconds", 3))
    wake_dt  = start_dt - timedelta(seconds=lead)
    now      = datetime.now()

    if now < wake_dt and not debug_mode:
        remaining = (wake_dt - now).total_seconds()
        _push_log("info", f"⏰ 予約開始: {start_dt.strftime('%m/%d %H:%M:%S')}  "
                          f"（残り約{int(remaining//60)}分{int(remaining%60)}秒）")
        state["status"]  = "waiting"
        state["message"] = f"予約開始まで待機中（{start_dt.strftime('%H:%M:%S')}）"
        _push_state()

        while datetime.now() < wake_dt:
            if stop_ev.is_set():
                return
            remaining = (wake_dt - datetime.now()).total_seconds()
            if remaining > 60:
                await asyncio.sleep(30)
                rem2 = (wake_dt - datetime.now()).total_seconds()
                _push_log("info", f"  残り {int(rem2//60)}分{int(rem2%60)}秒...")
            else:
                await asyncio.sleep(0.05)

    if stop_ev.is_set():
        return

    _push_log("info", "🌐 ブラウザを起動します")
    state["status"]  = "running"
    state["message"] = "ブラウザ起動中..."
    _push_state()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=cfg["browser"].get("headless", False),
            slow_mo=cfg["browser"].get("slow_mo", 50),
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

        try:
            # ─── ログイン ───
            _push_log("info", "🔐 ログイン中...")
            state["message"] = "ログイン中..."
            _push_state()

            t0 = time.perf_counter()
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
            _push_log("info", f"  ページ読み込み: {time.perf_counter()-t0:.2f}s")

            await _take_screenshot(page, "01_login")

            await page.fill('input[name="account"]', cfg["login"]["username"])
            await page.fill('input[name="password"]', cfg["login"]["password"])
            await page.click('input[type="submit"]')
            await page.wait_for_load_state("domcontentloaded", timeout=30000)
            await asyncio.sleep(0.5)

            login_url = page.url
            body      = await page.inner_text("body")
            await _take_screenshot(page, "02_after_login")

            if "パスワード" in body and "違" in body:
                _push_log("error", "❌ ログインエラー：会員番号またはパスワードが違います")
                state["status"]  = "failed"
                state["message"] = "ログイン失敗"
                _push_state()
                await browser.close()
                return

            _push_log("info", f"✅ ログイン成功（{time.perf_counter()-t_total:.2f}s）")
            state["message"] = "ログイン済み・予約開始待ち"
            _push_state()

            # ─── 予約開始時刻まで（ログイン状態で）待機 ───
            if datetime.now() < start_dt:
                remain = (start_dt - datetime.now()).total_seconds()
                _push_log("info", f"  予約開始まで {remain:.1f}秒...")
                while datetime.now() < start_dt:
                    if stop_ev.is_set():
                        await browser.close()
                        return
                    await asyncio.sleep(0.02)
                _push_log("info", "⏰ 予約開始時刻になりました！")

            # ─── 予約ループ ───
            max_att  = cfg["retry"]["max_attempts"]
            interval = cfg["retry"]["interval_seconds"]
            success  = False

            for attempt in range(1, max_att + 1):
                if stop_ev.is_set():
                    break

                state["attempt"] = attempt
                _push_log("info", f"─── 試行 {attempt}/{max_att} ───")
                state["message"] = f"予約試行中 ({attempt}/{max_att})"
                _push_state()

                t_att = time.perf_counter()

                try:
                    # ゴルフ予約ページへ
                    await page.goto(GOLF_AUTH_URL, wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(0.8)
                    await _take_screenshot(page, f"03_golf_{attempt}")

                    # 「同意する」があれば同意
                    for sel in ['button:has-text("同意する")', 'input[value="同意する"]',
                                'a:has-text("同意する")']:
                        try:
                            el = await page.query_selector(sel)
                            if el and await el.is_visible():
                                _push_log("info", "  個人情報に同意します")
                                await el.click()
                                await page.wait_for_load_state("domcontentloaded", timeout=10000)
                                break
                        except Exception:
                            continue

                    # フォーム要素を調査してログ
                    elements = await page.evaluate("""() =>
                        Array.from(document.querySelectorAll('input,select,textarea')).map(e => ({
                            tag: e.tagName, type: e.type||'', name: e.name||'', id: e.id||''
                        }))
                    """)
                    _push_log("info", f"  フォーム要素数: {len(elements)}")

                    # 日付入力
                    res = cfg["reservation"]
                    for sel in ['input[name*="date"]', 'input[type="date"]',
                                '#playDate', 'select[name*="date"]']:
                        try:
                            el = await page.query_selector(sel)
                            if el and await el.is_visible():
                                tag = await el.evaluate("e => e.tagName.toLowerCase()")
                                if tag == "select":
                                    await page.select_option(sel, res["play_date"])
                                else:
                                    await page.fill(sel, res["play_date"])
                                _push_log("info", f"  日付: {res['play_date']}")
                                break
                        except Exception:
                            continue

                    # 時刻入力
                    for sel in ['select[name*="time"]', 'select[name*="start"]',
                                'input[name*="time"]', '#startTime']:
                        try:
                            el = await page.query_selector(sel)
                            if el and await el.is_visible():
                                tag = await el.evaluate("e => e.tagName.toLowerCase()")
                                if tag == "select":
                                    await page.select_option(sel, res["play_time"])
                                else:
                                    await page.fill(sel, res["play_time"])
                                _push_log("info", f"  時刻: {res['play_time']}")
                                break
                        except Exception:
                            continue

                    # 人数
                    for sel in ['select[name*="player"]', 'select[name*="num"]',
                                'select[name*="count"]']:
                        try:
                            el = await page.query_selector(sel)
                            if el and await el.is_visible():
                                await page.select_option(sel, str(res["num_players"]))
                                _push_log("info", f"  人数: {res['num_players']}名")
                                break
                        except Exception:
                            continue

                    await _take_screenshot(page, f"04_form_{attempt}")

                    # テストモードはここで終了（送信しない）
                    if debug_mode:
                        body_preview = (await page.inner_text("body"))[:800]
                        _push_log("info", "─── テスト実行完了 ───")
                        _push_log("info", "✅ ログイン・ページ遷移・フォーム確認まで正常に動作しました")
                        _push_log("info", "📋 予約ページの内容（抜粋）:")
                        for line in body_preview.split("\n"):
                            if line.strip():
                                _push_log("info", f"   {line.strip()}")
                        state["status"]  = "success"
                        state["message"] = "テスト完了 ✅ 本番実行の準備ができています"
                        _push_state()
                        await asyncio.sleep(5)   # ブラウザを5秒表示してから閉じる
                        await browser.close()
                        return

                    # 検索ボタン
                    for sel in ['button:has-text("検索")', 'input[value="検索"]',
                                'button:has-text("照会")', 'button[type="submit"]',
                                'input[type="submit"]']:
                        try:
                            el = await page.query_selector(sel)
                            if el and await el.is_visible():
                                _push_log("info", f"  検索ボタンをクリック")
                                await el.click()
                                await page.wait_for_load_state("domcontentloaded", timeout=15000)
                                break
                        except Exception:
                            continue

                    await _take_screenshot(page, f"05_result_{attempt}")

                    # 空きスロット選択
                    play_time = res["play_time"]
                    for sel in [f'a:has-text("{play_time}")', f'button:has-text("{play_time}")',
                                'a:has-text("予約")', '.available a', '.open a']:
                        try:
                            el = await page.query_selector(sel)
                            if el and await el.is_visible():
                                _push_log("info", f"  スロット選択")
                                await el.click()
                                await page.wait_for_load_state("domcontentloaded", timeout=15000)
                                break
                        except Exception:
                            continue

                    # 予約ボタン
                    for sel in ['button:has-text("予約する")', 'button:has-text("申込む")',
                                'input[value="予約する"]', 'button:has-text("確定")',
                                'button[type="submit"]', 'input[type="submit"]']:
                        try:
                            el = await page.query_selector(sel)
                            if el and await el.is_visible():
                                _push_log("info", f"  予約ボタンをクリック")
                                await el.click()
                                await page.wait_for_load_state("domcontentloaded", timeout=15000)
                                break
                        except Exception:
                            continue

                    await _take_screenshot(page, f"06_final_{attempt}")

                    # 結果判定
                    body2    = await page.inner_text("body")
                    ok_kw    = ["完了", "受付", "予約番号", "申込番号", "ありがとう", "受付番号"]
                    fail_kw  = ["エラー", "失敗", "満席", "空きがありません"]
                    elapsed  = time.perf_counter() - t_att

                    if any(k in body2 for k in ok_kw):
                        _push_log("success", f"🎉 予約完了！（{elapsed:.2f}s）")
                        for line in body2.split("\n"):
                            if any(k in line for k in ["予約番号", "申込番号", "受付番号"]):
                                _push_log("success", f"  {line.strip()}")
                        success = True
                        break
                    elif any(k in body2 for k in fail_kw):
                        _push_log("warn", f"  空きなし/エラー → リトライします（{elapsed:.2f}s）")
                    else:
                        _push_log("warn", f"  結果不明 → スクリーンショットを確認してください（{elapsed:.2f}s）")

                except Exception as e:
                    _push_log("error", f"  例外: {e}")
                    await _take_screenshot(page, f"err_{attempt}")

                if attempt < max_att and not stop_ev.is_set():
                    _push_log("info", f"  {interval}秒後にリトライ...")
                    await asyncio.sleep(interval)

            # ─── 完了処理 ───
            total = time.perf_counter() - t_total
            if success:
                state["status"]  = "success"
                state["message"] = f"予約完了！（合計 {total:.1f}秒）"
            elif stop_ev.is_set():
                state["status"]  = "cancelled"
                state["message"] = "キャンセルされました"
            else:
                state["status"]  = "failed"
                state["message"] = f"{max_att}回試行しましたが予約できませんでした"
                _push_log("error", state["message"])

            _push_log("info", f"⏱  合計処理時間: {total:.2f}秒")
            _push_state()

        finally:
            await browser.close()


# ─────────────────────────────────────────
# 起動
# ─────────────────────────────────────────

if __name__ == "__main__":
    import webbrowser, os
    print("=" * 50)
    print("  TJK 成田ビューゴルフコース 自動予約 Web アプリ")
    print("  http://localhost:5050 をブラウザで開いてください")
    print("  終了: Ctrl+C")
    print("=" * 50)
    # 少し待ってからブラウザを開く
    threading.Timer(1.5, lambda: webbrowser.open("http://localhost:5050")).start()
    app.run(host="0.0.0.0", port=5050, debug=False, threaded=True)
