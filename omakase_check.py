"""OMAKASE キャンセル拾い監視

Cloudflare対策のため requests ではなく Playwright（実ブラウザ）でページを取得し、
「ご予約可能な枠がありません」表示が消えたら Discord に通知する。
"""
import os
import json
import time
from datetime import datetime, timedelta, timezone

import requests
from playwright.sync_api import sync_playwright

DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

JST = timezone(timedelta(hours=9))

# 監視する店舗リスト
SHOPS = [
    {"name": "nacol（浅草・イタリアン）", "url": "https://omakase.in/r/ur658194"},
]

NO_SLOT_TEXT = "ご予約可能な枠がありません"
# 未ログインで空き枠が存在するときに表示されるボタン文言
SLOT_EXISTS_TEXTS = ["ログインして空き枠を確認", "このお店を予約する"]
# 予約受付開始前などの「空きではない」状態
NOT_OPEN_TEXTS = ["しばらくお待ち下さい", "ご予約開始までお待ちください"]
NOTIFY_COOLDOWN_MIN = 60 * 24   # 空き検出通知の再送スキップ時間（分）
BLOCK_COOLDOWN_MIN = 60 * 24    # Cloudflareブロック通知の再送スキップ時間（分）
STATE_FILE = "omakase_state.json"


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def is_in_cooldown(state, key, minutes):
    last = state.get(key)
    if not last:
        return False
    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
    return elapsed < minutes * 60


def new_context(browser):
    return browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        locale="ja-JP",
        viewport={"width": 1280, "height": 900},
    )


def fetch_page_text(page, url):
    """ページを開いて本文テキストを返す。Cloudflareチャレンジ通過を待つ。"""
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    # Cloudflareのチャレンジ（Just a moment...）が挟まる場合があるので少し待って再取得
    for _ in range(6):
        text = page.inner_text("body")
        title = page.title()
        if "Just a moment" in title or "Attention Required" in title or "challenge" in page.url:
            page.wait_for_timeout(5_000)
            continue
        if NO_SLOT_TEXT in text or "予約" in text:
            return text, False
        page.wait_for_timeout(5_000)
    # 最後まで本文が確認できなければブロックとみなす
    return page.inner_text("body"), True


def fetch_with_retry(browser, url, retries=2):
    """コンテキストを作り直しながら最大 retries+1 回試行する。
    連続アクセスによるCloudflareブロック（Attention Required!）対策。"""
    for attempt in range(retries + 1):
        context = new_context(browser)
        page = context.new_page()
        try:
            text, blocked = fetch_page_text(page, url)
            if not blocked:
                return text, False
        finally:
            context.close()
        if attempt < retries:
            time.sleep(20 * (attempt + 1))  # 間隔を空けてブロック解除を待つ
    return text, True


def notify_discord(message):
    res = requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=10)
    res.raise_for_status()


def main():
    state = load_state()
    blocked = False

    with sync_playwright() as p:
        browser = p.chromium.launch(
            args=["--disable-blink-features=AutomationControlled"]
        )

        for i, shop in enumerate(SHOPS):
            if i > 0:
                time.sleep(10)  # 連続アクセスによるブロックを避ける
            try:
                text, maybe_blocked = fetch_with_retry(browser, shop["url"])
            except Exception as e:
                print(f"{shop['name']}: エラー — {e}")
                continue

            if maybe_blocked:
                print(f"{shop['name']}: ページ本文を確認できず（Cloudflareブロックの可能性）")
                blocked = True
                continue

            if NO_SLOT_TEXT in text:
                print(f"{shop['name']}: 空き枠なし")
                continue

            if any(t in text for t in NOT_OPEN_TEXTS):
                print(f"{shop['name']}: 予約受付開始前")
                continue

            if any(t in text for t in SLOT_EXISTS_TEXTS):
                # 未ログインでは枠の日時までは見えないが、空きの存在は確定
                key = f"notify:{shop['url']}"
                if is_in_cooldown(state, key, NOTIFY_COOLDOWN_MIN):
                    print(f"{shop['name']}: 空きあり（クールダウン中のため通知スキップ）")
                    continue
                notify_discord(
                    f"@everyone 🍣 **OMAKASE 空き枠が出ました**\n"
                    f"**{shop['name']}**\n"
                    f"ログインして枠を確認・予約してください\n→ {shop['url']}"
                )
                state[key] = datetime.now(timezone.utc).isoformat()
                print(f"{shop['name']}: 空きを検出、通知しました")
                continue

            # 既知のどの状態にも該当しない＝ページ構造が変わった可能性
            print(f"{shop['name']}: 未知のページ状態（要確認）")
            blocked = True

        browser.close()

    if blocked and not is_in_cooldown(state, "_blocked", BLOCK_COOLDOWN_MIN):
        try:
            notify_discord(
                "⚠️ OMAKASE監視: 空き状況を確認できていない可能性があります"
                "（Cloudflareブロックまたはページ構造の変更）。"
            )
            state["_blocked"] = datetime.now(timezone.utc).isoformat()
        except Exception as e:
            print(f"エラー通知の送信に失敗: {e}")

    save_state(state)


if __name__ == "__main__":
    main()
