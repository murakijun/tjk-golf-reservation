#!/usr/bin/env python3
"""
TJK 成田ビューゴルフコース 自動予約 Web アプリ
起動: python3 app.py
ブラウザ: http://localhost:5050
"""

import asyncio
import json
import queue
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, send_from_directory
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ─── TJK ゴルフ予約専用システム（www5サーバー）───
GOLF_BASE  = "https://www5.tjk.gr.jp/TJK"
LOGIN_URL  = f"{GOLF_BASE}/login_wing.php"
SCREENSHOTS = Path(__file__).parent / "screenshots"
SCREENSHOTS.mkdir(exist_ok=True)

# ─────────────────────────────────────────
# グローバル状態
# ─────────────────────────────────────────

log_queue: queue.Queue = queue.Queue()

state = {
    "status":       "idle",   # idle/waiting/running/waiting_confirm/success/failed/cancelled
    "message":      "待機中",
    "started_at":   None,
    "attempt":      0,
    "max_attempts": 10,
    "screenshots":  [],
    "booked_time":  None,     # 予約できた時間帯
    "debug_mode":   False,
}

_stop_event    = threading.Event()
_confirm_event = threading.Event()   # テスト時の確認待ちフラグ
_runner_thread = None

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


@app.route("/stream")
def stream():
    def generate():
        while True:
            try:
                msg = log_queue.get(timeout=20)
                yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
            except queue.Empty:
                yield f"data: {json.dumps({'type':'ping'})}\n\n"
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/state")
def get_state():
    return jsonify(state)


@app.route("/api/start", methods=["POST"])
def start():
    global _runner_thread, _stop_event, _confirm_event

    if state["status"] in ("waiting", "running", "waiting_confirm"):
        return jsonify({"ok": False, "error": "既に実行中です"}), 400

    data = request.json or {}

    # 時間帯リストを受け取る（優先順位順）
    time_slots = data.get("time_slots", [])
    if not time_slots:
        return jsonify({"ok": False, "error": "時間帯を1つ以上選択してください"}), 400

    cfg = {
        "login": {
            "username": data.get("username", ""),
            "password": data.get("password", ""),
        },
        "reservation_start": {
            "date":         data.get("start_date", ""),
            "time":         data.get("start_time", "10:00") + ":00",
            "lead_seconds": int(data.get("lead_seconds", 3)),
        },
        "reservation": {
            "play_date":   data.get("play_date", ""),
            "time_slots":  time_slots,        # 優先順位順の時間リスト
            "num_players": int(data.get("num_players", 4)),
        },
        "browser": {
            "headless": data.get("headless", False),
            "slow_mo":  50,
            "timeout":  30000,
        },
        "retry": {
            "max_attempts":    int(data.get("max_attempts", 10)),
            "interval_seconds": 0.5,
        },
    }

    debug_mode = bool(data.get("debug_mode", False))

    _stop_event    = threading.Event()
    _confirm_event = threading.Event()

    state.update({
        "status":       "waiting",
        "message":      "テスト実行中..." if debug_mode else "予約開始時刻まで待機中...",
        "started_at":   datetime.now().isoformat(),
        "attempt":      0,
        "max_attempts": cfg["retry"]["max_attempts"],
        "screenshots":  [],
        "booked_time":  None,
        "debug_mode":   debug_mode,
    })
    _push_log("info", "🔍 テスト実行（確認画面で一時停止）" if debug_mode else "🚀 予約システムを起動しました")
    _push_log("info", f"  希望時間（優先順）: {' → '.join(time_slots)}")

    def runner():
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(
                _reservation_main(cfg, _stop_event, _confirm_event, debug_mode)
            )
        except Exception as e:
            tb = traceback.format_exc()
            _push_log("error", f"❌ 致命的エラー: {e}")
            for line in tb.splitlines():
                _push_log("error", f"  {line}")
            state["status"]  = "failed"
            state["message"] = f"エラー: {e}"
            _push_state()
        finally:
            loop.close()

    _runner_thread = threading.Thread(target=runner, daemon=True)
    _runner_thread.start()

    return jsonify({"ok": True})


@app.route("/api/confirm", methods=["POST"])
def confirm():
    """テスト時の確認画面で「予約を確定する」ボタンが押されたとき"""
    _confirm_event.set()
    state["message"] = "予約を確定中..."
    _push_state()
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def stop():
    _stop_event.set()
    _confirm_event.set()   # 待機中の confirm も解除
    state["status"]  = "cancelled"
    state["message"] = "キャンセルしました"
    _push_log("warn", "⛔ 予約をキャンセルしました")
    _push_state()
    return jsonify({"ok": True})


# ─────────────────────────────────────────
# ログ / スクリーンショット送信ヘルパー
# ─────────────────────────────────────────

def _push_log(level: str, message: str):
    log_queue.put({
        "type":    "log",
        "level":   level,
        "message": message,
        "time":    datetime.now().strftime("%H:%M:%S.%f")[:-3],
    })


def _push_screenshot(filename: str, label: str = ""):
    state["screenshots"].append(filename)
    log_queue.put({
        "type":     "screenshot",
        "filename": filename,
        "label":    label,
        "time":     datetime.now().strftime("%H:%M:%S"),
    })


def _push_state():
    log_queue.put({"type": "state", "state": dict(state)})


async def _snap(page, name: str, label: str = "") -> str:
    ts    = datetime.now().strftime("%H%M%S")
    fname = f"{ts}_{name}.png"
    await page.screenshot(path=str(SCREENSHOTS / fname))
    _push_screenshot(fname, label)
    return fname


# ─────────────────────────────────────────
# メイン予約処理
# ─────────────────────────────────────────

async def _reservation_main(cfg, stop_ev, confirm_ev, debug_mode: bool):
    t_total = time.perf_counter()

    # ─── 予約受付開始時刻まで待機（テストは即時）───
    rs       = cfg["reservation_start"]
    start_dt = datetime.fromisoformat(f"{rs['date']}T{rs['time']}")
    wake_dt  = start_dt - timedelta(seconds=int(rs.get("lead_seconds", 3)))

    if not debug_mode and datetime.now() < wake_dt:
        remaining = (wake_dt - datetime.now()).total_seconds()
        _push_log("info", f"⏰ 予約受付開始: {start_dt.strftime('%m/%d %H:%M:%S')} "
                          f"（残り約{int(remaining//60)}分{int(remaining%60)}秒）")
        state["status"]  = "waiting"
        state["message"] = f"待機中（{start_dt.strftime('%H:%M:%S')}まで）"
        _push_state()
        while datetime.now() < wake_dt:
            if stop_ev.is_set():
                return
            remaining = (wake_dt - datetime.now()).total_seconds()
            if remaining > 60:
                await asyncio.sleep(30)
                _push_log("info", f"  残り {int((wake_dt-datetime.now()).total_seconds()//60)}分...")
            else:
                await asyncio.sleep(0.05)

    if stop_ev.is_set():
        return

    state["status"]  = "running"
    state["message"] = "ブラウザ起動中..."
    _push_state()
    _push_log("info", "🌐 ブラウザを起動します")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=cfg["browser"].get("headless", False),
            slow_mo=cfg["browser"].get("slow_mo", 50),
        )
        context = await browser.new_context(
            locale="ja-JP", timezone_id="Asia/Tokyo",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        try:
            # ══════════════════════════════
            # STEP 1: ログイン
            # ══════════════════════════════
            _push_log("info", "━━━ STEP 1: ログイン ━━━")
            state["message"] = "ログイン中..."
            _push_state()

            t0 = time.perf_counter()
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
            _push_log("info", f"  ページ読み込み: {time.perf_counter()-t0:.2f}s")
            await _snap(page, "01_login", "ログインページ")

            # ユーザーID分割・ゼロ埋め
            uid = cfg["login"]["username"].replace(" ", "")
            id1, id2 = (uid.split("-", 1) if "-" in uid else (uid[:4], uid[4:]))
            id1, id2 = id1.zfill(4), id2.zfill(6)
            _push_log("info", f"  ユーザーID: {id1}-{id2}")

            await page.fill('input[name="login_id1"]', id1)
            await page.fill('input[name="login_id2"]', id2)
            await page.fill('input[name="pass"]',      cfg["login"]["password"])

            try:
                async with page.expect_navigation(timeout=15000):
                    await page.evaluate("check()")
            except Exception as e:
                _push_log("warn", f"  ナビゲーション待機: {e}（続行）")
                await asyncio.sleep(1)

            await asyncio.sleep(0.5)
            body = await page.inner_text("body")
            await _snap(page, "02_after_login", "ログイン後")
            _push_log("info", f"  ログイン後URL: {page.url}")

            error_kw = ["パスワードが違", "ＩＤが違", "IDが違", "ログインできません", "認証エラー", "入力してください"]
            if any(k in body for k in error_kw):
                _push_log("error", "❌ ログイン失敗：ユーザーIDまたはパスワードを確認してください")
                state.update({"status": "failed", "message": "ログイン失敗"})
                _push_state()
                return

            _push_log("info", f"✅ ログイン成功（{time.perf_counter()-t_total:.2f}s）")

            # ══════════════════════════════
            # STEP 2: 予約受付開始まで待機（ログイン状態のまま）
            # ══════════════════════════════
            if not debug_mode and datetime.now() < start_dt:
                remain = (start_dt - datetime.now()).total_seconds()
                _push_log("info", f"━━━ STEP 2: 予約開始まで {remain:.1f}秒 待機 ━━━")
                state["message"] = f"ログイン済み・予約開始待ち（{start_dt.strftime('%H:%M:%S')}）"
                _push_state()
                while datetime.now() < start_dt:
                    if stop_ev.is_set():
                        return
                    await asyncio.sleep(0.02)
                _push_log("info", "⏰ 予約受付開始！")

            # ══════════════════════════════
            # STEP 3: 時間帯ごとに予約を試みる
            # ══════════════════════════════
            _push_log("info", "━━━ STEP 3: 空き確認・予約 ━━━")
            state["message"] = "空き状況を確認中..."
            _push_state()

            time_slots   = cfg["reservation"]["time_slots"]
            max_att      = cfg["retry"]["max_attempts"]
            interval     = cfg["retry"]["interval_seconds"]
            booked_time  = None
            success      = False

            for attempt in range(1, max_att + 1):
                if stop_ev.is_set():
                    break

                state["attempt"] = attempt
                _push_state()

                # ゴルフ予約ページへ遷移（毎回）
                _push_log("info", f"  試行 {attempt}/{max_att} — 予約ページへ移動")
                golf_links = [
                    'a:has-text("ゴルフ")', 'a:has-text("予約")', 'a:has-text("成田")',
                    'a[href*="golf"]', 'a[href*="yoyaku"]',
                ]
                for sel in golf_links:
                    try:
                        el = await page.query_selector(sel)
                        if el and await el.is_visible():
                            await el.click()
                            await page.wait_for_load_state("domcontentloaded", timeout=15000)
                            break
                    except Exception:
                        continue

                await asyncio.sleep(0.5)
                await _snap(page, f"03_menu_{attempt}", f"試行{attempt}:メニュー")
                _push_log("info", f"  現在URL: {page.url}")

                # 日付選択
                res = cfg["reservation"]
                for sel in ['input[name*="date"]', 'input[type="date"]', 'select[name*="date"]',
                            '#yyyymmdd', 'input[name="yyyymmdd"]']:
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

                # 人数選択
                for sel in ['select[name*="player"]', 'select[name*="num"]',
                            'select[name*="count"]', 'select[name*="nin"]']:
                    try:
                        el = await page.query_selector(sel)
                        if el and await el.is_visible():
                            await page.select_option(sel, str(res["num_players"]))
                            _push_log("info", f"  人数: {res['num_players']}名")
                            break
                    except Exception:
                        continue

                # 検索実行
                for sel in ['button:has-text("検索")', 'input[value="検索"]',
                            'input[value="照会"]', 'input[type="submit"]',
                            'button[type="submit"]']:
                    try:
                        el = await page.query_selector(sel)
                        if el and await el.is_visible():
                            _push_log("info", "  検索ボタンをクリック")
                            await el.click()
                            await page.wait_for_load_state("domcontentloaded", timeout=15000)
                            break
                    except Exception:
                        continue

                await asyncio.sleep(0.5)
                await _snap(page, f"04_slots_{attempt}", f"試行{attempt}:空き一覧")

                # ── 希望時間帯を順番に試す ──
                slot_booked = False
                body_text   = await page.inner_text("body")

                for slot_time in time_slots:
                    if stop_ev.is_set():
                        break

                    _push_log("info", f"  🕐 {slot_time} の空きを確認...")

                    # 「満」「×」「不可」が含まれていないリンクを探す
                    slot_selectors = [
                        f'a:has-text("{slot_time}")',
                        f'td:has-text("{slot_time}") a',
                        f'input[value*="{slot_time}"]',
                        f'button:has-text("{slot_time}")',
                    ]

                    slot_el = None
                    for sel in slot_selectors:
                        try:
                            el = await page.query_selector(sel)
                            if el and await el.is_visible():
                                # 親要素に「満」「×」があれば埋まっている
                                parent_text = await el.evaluate(
                                    "e => e.closest('tr,td,div,li') ? "
                                    "e.closest('tr,td,div,li').textContent : e.textContent"
                                )
                                full_kw = ["満", "×", "✗", "不可", "締切", "FULL", "予約不可"]
                                if any(k in parent_text for k in full_kw):
                                    _push_log("warn", f"    → {slot_time} は満席")
                                    continue
                                slot_el = el
                                break
                        except Exception:
                            continue

                    if slot_el is None:
                        _push_log("warn", f"    → {slot_time} の空き枠が見つかりません")
                        continue

                    # 空き枠をクリック
                    _push_log("info", f"  ✅ {slot_time} に空きあり → 選択します")
                    await slot_el.click()
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                    await asyncio.sleep(0.5)
                    await _snap(page, f"05_selected_{attempt}", f"{slot_time}を選択")

                    # 確認画面まで進む
                    confirm_pg_kw = ["確認", "confirm", "内容", "申込内容", "予約内容"]
                    submit_selectors = [
                        'button:has-text("次へ")', 'input[value="次へ"]',
                        'button:has-text("確認")', 'input[value="確認"]',
                        'button:has-text("予約する")', 'input[value="予約する"]',
                        'button[type="submit"]', 'input[type="submit"]',
                    ]
                    for sel in submit_selectors:
                        try:
                            el = await page.query_selector(sel)
                            if el and await el.is_visible():
                                _push_log("info", f"    次へ進む: {sel}")
                                await el.click()
                                await page.wait_for_load_state("domcontentloaded", timeout=15000)
                                await asyncio.sleep(0.5)
                                break
                        except Exception:
                            continue

                    await _snap(page, f"06_confirm_{attempt}", f"{slot_time}:確認画面")
                    booked_time = slot_time
                    slot_booked = True
                    break   # 空き枠を見つけたのでスロットループを抜ける

                if not slot_booked:
                    full_msg = "全ての希望時間帯が満席です"
                    _push_log("warn", f"  ⚠️  {full_msg} → リトライします")
                    state["message"] = full_msg
                    _push_state()
                    if attempt < max_att:
                        await asyncio.sleep(interval)
                    continue

                # ══════════════════════════════
                # STEP 4: テスト → 確認待ち / 本番 → そのまま確定
                # ══════════════════════════════
                if debug_mode:
                    _push_log("info", "━━━ STEP 4: 確認画面で一時停止（テストモード）━━━")
                    _push_log("info", f"  {booked_time} の予約確認画面に到達しました")
                    _push_log("info", "  「予約を確定する」ボタンを押すと本当に予約されます")

                    state["status"]     = "waiting_confirm"
                    state["message"]    = f"{booked_time} の確認画面で待機中"
                    state["booked_time"] = booked_time
                    _push_state()

                    # ユーザーがボタンを押すまで待機
                    while not confirm_ev.is_set() and not stop_ev.is_set():
                        await asyncio.sleep(0.1)

                    if stop_ev.is_set():
                        _push_log("warn", "キャンセルされました")
                        break

                    _push_log("info", "  ▶ 確定ボタンが押されました → 予約を送信します")
                    state["status"]  = "running"
                    state["message"] = "予約を送信中..."
                    _push_state()

                # 最終確定ボタンをクリック
                final_selectors = [
                    'button:has-text("予約する")', 'input[value="予約する"]',
                    'button:has-text("申込む")',   'input[value="申込む"]',
                    'button:has-text("確定")',     'input[value="確定"]',
                    'button:has-text("OK")',
                    'button[type="submit"]',      'input[type="submit"]',
                ]
                for sel in final_selectors:
                    try:
                        el = await page.query_selector(sel)
                        if el and await el.is_visible():
                            _push_log("info", f"  確定ボタンをクリック: {sel}")
                            await el.click()
                            await page.wait_for_load_state("domcontentloaded", timeout=15000)
                            break
                    except Exception:
                        continue

                await asyncio.sleep(0.5)
                await _snap(page, "07_result", "予約結果")

                # 結果判定
                body2    = await page.inner_text("body")
                ok_kw    = ["完了", "受付", "予約番号", "申込番号", "ありがとう", "受付番号"]
                fail_kw  = ["エラー", "失敗", "できません", "満席"]

                if any(k in body2 for k in ok_kw):
                    success = True
                    # 予約番号があれば抽出
                    for line in body2.split("\n"):
                        if any(k in line for k in ["予約番号", "申込番号", "受付番号"]):
                            _push_log("success", f"  {line.strip()}")
                    _push_log("success", f"🎉 {booked_time} で予約完了！（{time.perf_counter()-t_total:.2f}s）")
                    break
                elif any(k in body2 for k in fail_kw):
                    _push_log("warn", f"  送信後にエラー → リトライ")
                    booked_time = None
                    await asyncio.sleep(interval)
                else:
                    _push_log("warn", f"  結果不明 → スクリーンショットを確認してください")
                    success = True   # 不明な場合は成功扱いで終了
                    break

            # ── 最終結果 ──
            elapsed = time.perf_counter() - t_total
            if success and booked_time:
                state.update({
                    "status":      "success",
                    "message":     f"✅ {booked_time} で予約完了！",
                    "booked_time": booked_time,
                })
                _push_log("success", f"⏱ 合計時間: {elapsed:.2f}秒")
            elif stop_ev.is_set():
                state.update({"status": "cancelled", "message": "キャンセルされました"})
            else:
                state.update({
                    "status":  "failed",
                    "message": "全ての希望時間が満席でした。別の日程をお試しください。",
                })
                _push_log("error", "❌ 予約できませんでした")
            _push_state()

        finally:
            await browser.close()


# ─────────────────────────────────────────
# 起動
# ─────────────────────────────────────────

if __name__ == "__main__":
    import webbrowser
    print("=" * 50)
    print("  TJK 成田ビューゴルフコース 自動予約 Web アプリ")
    print("  http://localhost:5050 をブラウザで開いてください")
    print("  終了: Ctrl+C")
    print("=" * 50)
    threading.Timer(1.5, lambda: webbrowser.open("http://localhost:5050")).start()
    app.run(host="0.0.0.0", port=5050, debug=False, threaded=True)
